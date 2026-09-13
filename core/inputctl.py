# -*- coding: utf-8 -*-
"""控制层：把鼠标/键盘事件翻译为设备端 input / am 指令。

设计要点
--------
1. 只依赖 core.adb，不关心画面来源 —— 因此无论使用内置投屏引擎还是 scrcpy，
   控制逻辑完全一致（scrcpy 模式下本模块仍然可用，作为"备用通道"）。
2. 文本输入分两条通道：
   - 通道 A（免安装）：`input text`，仅支持 ASCII 可打印字符；
   - 通道 B（Unicode/中文）：依赖设备已安装并启用 ADBKeyboard 输入法，
     通过广播 `ADB_INPUT_B64` 传入 Base64 文本。未安装时明确告知用户。
   本工具不会自动静默安装输入法，需用户主动操作。
3. 唤醒只做"点亮屏幕"，不做任何绕过锁屏凭据的事情；若设备设有密码/指纹锁，
   界面会提示用户通过投屏画面自行输入。
"""

from __future__ import annotations

import base64
import queue
import re
import threading

from core.adb import AdbClient, AdbError, input_text_escape, shell_quote

# 常用按键（KeyEvent 常量）
KEY_EVENTS: dict[str, int] = {
    "返回": 4,        # KEYCODE_BACK
    "Home": 3,       # KEYCODE_HOME
    "多任务": 187,    # KEYCODE_APP_SWITCH
    "电源": 26,       # KEYCODE_POWER
    "唤醒": 224,      # KEYCODE_WAKEUP
    "休眠": 223,      # KEYCODE_SLEEP
    "音量+": 24,
    "音量-": 25,
    "静音": 164,      # KEYCODE_MUTE
    "菜单": 82,       # KEYCODE_MENU
    "通知栏": 1,      # 非标准键，见 open_notifications()
    "回车": 66,
    "删除": 67,
    "Tab": 61,
    "空格": 62,
    "上": 19,
    "下": 20,
    "左": 21,
    "右": 22,
    "确认(DPAD)": 23,
    "截图(系统)": 120,
    "亮度+": 221,
    "亮度-": 220,
    "播放/暂停": 85,
    "上一首": 88,
    "下一首": 87,
    "长按电源菜单": 276,  # KEYCODE_POWER + long press，部分 ROM 有效
    # 编辑键（Android 12+ 可由 shell 注入；旧系统部分 ROM 忽略）
    "复制": 278,
    "粘贴": 279,
    "剪切": 277,
    "撤销": 271,
    "重做": 281,
    "光标居首": 122,
    "光标居尾": 123,
    "上翻页": 92,
    "下翻页": 93,
}

ASCII_ONLY = re.compile(r"^[\x20-\x7E]*$")


def _extract_clip_text(output: str) -> str:
    """从剪贴板读取结果中尽力提取文本（支持 Base64 / data="..." 两种回包）。"""
    out = (output or "").strip()
    if not out:
        return ""
    m = re.search(r'data="([^"]+)"', out) or re.search(r'base64=([A-Za-z0-9+/=]+)', out)
    if m:
        out = m.group(1)
    try:
        return base64.b64decode(out, validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001 - 不是 Base64 就当纯文本用
        return out


class InputController:
    """反向控制器。所有方法均要求设备已授权 USB 调试。"""

    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial

    def _shell(self, command: str, timeout: int = 15) -> str:
        res = self.adb.shell(self.serial, command, timeout=timeout)
        if not res.ok and ("not found" in (res.stderr or "").lower()):
            raise AdbError("设备拒绝了输入指令", self.adb.explain(res.output), command)
        return res.output

    # ---------------- 触摸 ---------------- #

    def tap(self, x: int, y: int) -> None:
        self._shell(f"input tap {int(x)} {int(y)}")

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        # 起止点相同 + 持续时间 = 长按
        self._shell(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def swipe_direction(self, direction: str, w: int, h: int, ratio: float = 0.6,
                        duration_ms: int = 250) -> None:
        """按方向执行整屏滑动。direction: 上/下/左/右"""
        cx, cy = w // 2, h // 2
        dx = int(w * ratio / 2)
        dy = int(h * ratio / 2)
        if direction == "上":   # 手指由下往上 → 内容向上滚动
            self.swipe(cx, cy + dy, cx, cy - dy, duration_ms)
        elif direction == "下":
            self.swipe(cx, cy - dy, cx, cy + dy, duration_ms)
        elif direction == "左":
            self.swipe(cx + dx, cy, cx - dx, cy, duration_ms)
        elif direction == "右":
            self.swipe(cx - dx, cy, cx + dx, cy, duration_ms)
        else:
            raise ValueError(direction)

    def drag(self, points: list[tuple[int, int]], duration_ms: int = 800) -> None:
        """多点连续滑动（简易手势）。Android 的 input swipe 只支持两点，
        因此按段依次发送；段数多时自动缩短每段时长。"""
        if len(points) < 2:
            return
        seg = max(80, int(duration_ms / (len(points) - 1)))
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            self.swipe(x1, y1, x2, y2, seg)

    def gesture(self, points: list[tuple[int, int]], max_points: int = 32) -> None:
        """以**单次连续手势**绘制完整轨迹（图案解锁、滑动轨迹等）。

        与 drag() 不同：drag 把轨迹拆成多段 `input swipe`（每段都是独立的
        按下-抬起），图案锁视图会把它当成多次新尝试，画不出图案。
        gesture 使用 `input motionevent DOWN/MOVE.../UP`（Android 9+），
        全程只有一次按下、一次抬起，图案视图能识别为一条连续轨迹。
        老设备不支持 motionevent 时自动回退为 drag()。
        """
        if not points or len(points) < 2:
            return
        # 控制点数量，减少 adb 往返
        pts = points
        if len(pts) > max_points:
            step = (len(pts) - 1) / (max_points - 1)
            pts = [pts[int(i * step)] for i in range(max_points)]
        first, last = pts[0], pts[-1]
        try:
            self._shell(f"input motionevent DOWN {int(first[0])} {int(first[1])}")
            for x, y in pts[1:-1]:
                self._shell(f"input motionevent MOVE {int(x)} {int(y)}")
            self._shell(f"input motionevent UP {int(last[0])} {int(last[1])}")
        except AdbError:
            # 旧设备无 motionevent 子命令：退化为分段 swipe（图案可能不完整）
            self.drag(points, 1000)

    def pinch(self, cx: int, cy: int, start_gap: int = 120, end_gap: int = 400,
              duration_ms: int = 500) -> None:
        """双指缩放示意。ADB 无原生多点接口，这里用两条快速滑动近似（部分 ROM 可识别）。"""
        self.swipe(cx - start_gap, cy, cx - end_gap, cy, duration_ms)
        self.swipe(cx + start_gap, cy, cx + end_gap, cy, duration_ms)

    # ---------------- 文本 ---------------- #

    def input_text(self, text: str) -> str:
        """输入文本，自动选择通道。返回通道说明，便于界面提示。"""
        if not text:
            return "空文本"
        if ASCII_ONLY.match(text):
            self._shell(f"input text {input_text_escape(text)}")
            return "ASCII 通道（input text）"
        # Unicode：尝试 ADBKeyboard 广播
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        res = self.adb.shell(
            self.serial,
            f"am broadcast -a ADB_INPUT_B64 --es msg {shell_quote(b64)}",
            timeout=15,
        )
        out = res.output
        if "result=0" in out or "Broadcast completed" in out or res.ok:
            return "Unicode 通道（ADBKeyboard 广播）"
        raise AdbError(
            "中文/Unicode 输入失败",
            "原生 `input text` 仅支持英文与数字。请先安装并启用 ADBKeyboard 输入法："
            "在『应用管理』页安装 APK → 手机端『设置-系统-语言和输入法』中启用 ADBKeyboard，"
            "回到本工具重试；或改用『剪贴板粘贴』方式（长按投屏画面粘贴）。",
            out,
        )

    def input_text_ascii(self, text: str) -> None:
        self._shell(f"input text {input_text_escape(text)}")

    def clear_field(self, times: int = 60) -> None:
        """清空当前输入框（连续删除键）。"""
        for _ in range(times):
            self.key_event(67)

    def adbkeyboard_available(self) -> bool:
        res = self.adb.shell(self.serial, "pm list packages com.android.adbkeyboard", timeout=10)
        return "com.android.adbkeyboard" in res.stdout

    def clipboard_set(self, text: str) -> bool:
        """把文本写入手机剪贴板，依次尝试三条通道（全部失败返回 False）：
        1) Clipper 广播（需安装 Clipper 应用，最可靠）
        2) Android 12+ 的 cmd clipboard set-primary-clip（部分 ROM 支持）
        3) root 下重试 cmd clipboard"""
        res = self.adb.shell(
            self.serial,
            f"am broadcast -a clipper.set -e text {shell_quote(text)}",
            timeout=10,
        )
        if "Broadcast completed" in res.output and "Error" not in res.output:
            return True
        # cmd clipboard 写入（Android 12+，部分 ROM/厂商禁用）
        res2 = self.adb.shell(
            self.serial,
            f"cmd clipboard set-primary-clip --user 0 {shell_quote(text)}",
            timeout=10)
        if res2.ok and "exception" not in (res2.output or "").lower() \
                and "denied" not in (res2.output or "").lower():
            return True
        res3 = self.adb.shell(
            self.serial,
            f"su -c {shell_quote('cmd clipboard set-primary-clip --user 0 ' + text)}",
            timeout=10)
        return res3.ok and "exception" not in (res3.output or "").lower() \
            and "denied" not in (res3.output or "").lower() and "not found" not in (res3.output or "").lower()

    def clipboard_get(self) -> tuple[bool, str, str]:
        """读取手机剪贴板（尽力而为）。返回 (成功?, 文本, 通道/原因)。

        通道顺序：cmd clipboard get-primary-clip → root 重试 →
        ADBKeyboard 的 ADB_GET_TEXT 广播（结果里带 Base64）。
        Android 对剪贴板读取有前台/权限限制，部分系统全部失败——此时
        界面会提示改用『发送文本』通道，不会静默失败。
        """
        for label, cmd in (
            ("cmd clipboard", "cmd clipboard get-primary-clip --user 0"),
            ("root cmd clipboard",
             "su -c 'cmd clipboard get-primary-clip --user 0'"),
        ):
            res = self.adb.shell(self.serial, cmd, timeout=10)
            if res.ok:
                text = _extract_clip_text(res.output)
                if text and "exception" not in text.lower() and "denied" not in text.lower():
                    return True, text, label
        if self.adbkeyboard_available():
            res = self.adb.shell(self.serial, "am broadcast -a ADB_GET_TEXT", timeout=10)
            text = _extract_clip_text(res.output)
            if text:
                return True, text, "ADBKeyboard"
        return False, "", "系统限制：剪贴板读取需要前台应用权限（部分 ROM 禁用）。" \
                           "可改用『发送到手机』直接输入文本。"

    def paste(self) -> None:
        """模拟 Ctrl+V 粘贴（部分输入框有效）。"""
        self._shell("input keyevent 279")  # KEYCODE_PASTE

    # ---------------- 编辑键（复制/粘贴/剪切/全选/撤销/重做） ---------------- #

    def copy(self) -> None:
        self._shell("input keyevent 278")  # KEYCODE_COPY

    def cut(self) -> None:
        self._shell("input keyevent 277")  # KEYCODE_CUT

    def undo(self) -> None:
        self._shell("input keyevent 271")  # KEYCODE_UNDO

    def redo(self) -> None:
        self._shell("input keyevent 281")  # KEYCODE_REDO

    def select_all(self) -> bool:
        """Ctrl+A 全选：Android 11+ 用 keycombination，失败返回 False。"""
        res = self.adb.shell(self.serial, "input keycombination 29 4096", timeout=10)
        if not res.ok or "error" in (res.output or "").lower():
            return False
        return True

    # ---------------- 按键 ---------------- #

    def key_event(self, code: int) -> None:
        self._shell(f"input keyevent {int(code)}")

    def key_by_name(self, name: str) -> None:
        if name == "通知栏":
            self.open_notifications()
            return
        if name not in KEY_EVENTS:
            raise KeyError(name)
        self.key_event(KEY_EVENTS[name])

    def long_press_power(self) -> None:
        """长按电源键（调出关机菜单）。使用 swipe 模拟长按 Power 不被支持，
        故采用发送 KEYCODE_POWER 长按的替代实现：连续长按 keyevent。"""
        self._shell("input keyevent --longpress 26")

    # ---------------- 屏幕 / 系统 ---------------- #

    def open_notifications(self) -> None:
        self._shell("cmd statusbar expand-notifications")

    def collapse_notifications(self) -> None:
        self._shell("cmd statusbar collapse")

    def open_quick_settings(self) -> None:
        self._shell("cmd statusbar expand-settings")

    def lock_screen(self) -> None:
        """锁屏（熄灭屏幕）。"""
        self._shell("input keyevent 26")

    def set_brightness(self, value: int) -> None:
        """0-255 亮度。"""
        value = max(0, min(255, int(value)))
        self._shell(f"settings put system screen_brightness {value}")

    def get_brightness(self) -> int | None:
        res = self.adb.shell(self.serial, "settings get system screen_brightness", timeout=10)
        try:
            return int(res.stdout.strip())
        except ValueError:
            return None

    def start_activity(self, package: str, activity: str = "") -> None:
        if activity:
            self._shell(f"am start -n {package}/{activity}")
        else:
            self._shell(f"monkey -p {shell_quote(package)} -c android.intent.category.LAUNCHER 1")

    # ---------------- 唤醒与保活 ---------------- #

    def screen_state(self) -> str:
        """返回屏幕状态：Awake / Asleep / Dozing / Unknown"""
        res = self.adb.shell(self.serial, "dumpsys power", timeout=15)
        m = re.search(r"mWakefulness=(\w+)", res.stdout)
        return m.group(1) if m else "Unknown"

    def screen_on(self) -> bool | None:
        state = self.screen_state()
        if state == "Unknown":
            return None
        return state == "Awake"

    def is_locked(self) -> bool | None:
        """是否停在锁屏界面（True=锁屏，需用户自行输入凭据）。"""
        res = self.adb.shell(self.serial, "dumpsys window", timeout=15)
        if not res.stdout:
            return None
        if re.search(r"isKeyguardShowing=true|mShowingLockscreen=true", res.stdout):
            return True
        if re.search(r"isKeyguardShowing=false", res.stdout):
            return False
        return None

    def unlock_with_password(self, password: str) -> dict:
        """一键解锁：确保亮屏 → 唤出密码面板 → 输入密码 → 回车提交 → 验证。

        仅限设备所有者输入自己已知的密码；不含任何绕过/猜测逻辑。
        兼容两类 ROM：密码面板常驻的（上滑无副作用）与停在时钟页的
        （先上滑才有输入焦点）。首次失败时按"免上滑直接输入"重试一次。
        """
        import time as _t
        if not password:
            raise AdbError("未填写密码",
                           "请先在『键盘输入』框中输入锁屏密码，再点『一键解锁』。")
        if not ASCII_ONLY.match(password):
            raise AdbError("锁屏密码格式不支持",
                           "锁屏 PIN/密码需为英文、数字与常见符号；"
                           "锁屏界面无法使用中文输入通道。")
        report: dict = {"steps": [], "locked_before": None, "locked_after": None}
        if self.screen_state() != "Awake":
            self.wake(extend_timeout=True)
            report["steps"].append("已唤醒屏幕")
            _t.sleep(0.8)
        report["locked_before"] = self.is_locked()
        if report["locked_before"] is False:
            report["steps"].append("设备未在锁屏，无需解锁")
            report["locked_after"] = False
            return report
        try:
            res = self.adb.shell(self.serial, "wm size", timeout=10)
            m = re.search(r"(\d+)x(\d+)", res.stdout or "")
            w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        except Exception:  # noqa: BLE001
            w, h = 1080, 2400
        self.swipe(w // 2, int(h * 0.80), w // 2, int(h * 0.35), 300)
        _t.sleep(0.9)
        report["steps"].append("已唤出密码面板（上滑）")
        self.input_text(password)
        self.key_event(66)
        report["steps"].append("已输入密码并回车提交")
        _t.sleep(1.5)
        report["locked_after"] = self.is_locked()
        if report["locked_after"] is not False:
            # 部分 ROM 无需上滑（面板常驻），补一次直接输入
            self.input_text(password)
            self.key_event(66)
            _t.sleep(1.5)
            report["steps"].append("已按免上滑方式重试一次")
            report["locked_after"] = self.is_locked()
        return report

    def get_screen_timeout(self) -> int | None:
        res = self.adb.shell(self.serial, "settings get system screen_off_timeout", timeout=10)
        try:
            return int(res.stdout.strip())
        except (ValueError, AttributeError):
            return None

    def set_screen_timeout(self, ms: int) -> None:
        """设置自动熄屏时间（毫秒）。救援期间建议调到 10 分钟以上。"""
        self._shell(f"settings put system screen_off_timeout {int(ms)}")

    def keep_awake_on_usb(self) -> bool:
        """接入 USB 时保持不休眠（免 root；部分 ROM 可能忽略此设置）。"""
        res = self.adb.shell(self.serial, "svc power stayon usb", timeout=10)
        return res.ok

    def wake(self, *, extend_timeout: bool = True, timeout_ms: int = 600000,
             dismiss_keyguard: bool = True, brighten: bool = False,
             brightness: int = 0) -> dict:
        """点亮屏幕并返回详细状态。

        执行顺序：记录息屏前状态 → POWER/WAKEUP 唤醒 → 延长自动熄屏时间
                  → 设置 USB 常亮 → （可选）调高亮度 → 复查状态。

        合规说明：仅发送点亮屏幕的系统按键事件；若设备设置了密码/图案/指纹锁，
        本工具不会也无法绕过，需要用户在投屏画面中自行输入凭据。
        """
        report = {
            "before": self.screen_state(),
            "after": "Unknown",
            "ok": False,
            "locked": None,
            "timeout_ms": None,
            "brightness": None,
            "steps": [],
        }
        # 1) 唤醒：先发 POWER（部分 ROM 对 WAKEUP 无响应），再发 WAKEUP
        self._shell("input keyevent 26")
        self._shell("input keyevent 224")
        report["steps"].append("已发送电源键 + 唤醒事件")

        # 2) 延长自动熄屏时间，避免救援过程中反复息屏
        if extend_timeout:
            try:
                self.set_screen_timeout(int(timeout_ms))
                report["timeout_ms"] = int(timeout_ms)
                report["steps"].append(f"自动熄屏时间已延长至 {int(timeout_ms) // 1000} 秒")
            except Exception:
                pass

        # 3) USB 连接时保持不休眠
        try:
            if self.keep_awake_on_usb():
                report["steps"].append("已设置 USB 连接时保持常亮")
        except Exception:
            pass

        # 4) 滑动锁（仅对无密码锁屏有效）
        if dismiss_keyguard:
            self._shell("input keyevent 82")
            res = self.adb.shell(self.serial, "wm dismiss-keyguard", timeout=10)
            if res.ok and "error" not in (res.stdout or "").lower():
                report["steps"].append("已尝试关闭滑动锁")

        # 5) 调高亮度，便于投屏/外接显示观察
        if brighten:
            value = int(brightness) if brightness else max(self.get_brightness() or 0, 200)
            self.set_brightness(value)
            report["brightness"] = value
            report["steps"].append(f"亮度已设为 {value}")

        # 6) 复查
        import time as _t
        _t.sleep(0.6)
        report["after"] = self.screen_state()
        report["locked"] = self.is_locked()
        report["ok"] = report["after"] == "Awake"
        return report


class KeepAwake:
    """后台保活：定期检查屏幕状态，一旦息屏立即重新点亮。

    救援场景中手机屏幕往往无法触控，一旦自动熄屏就完全失去画面，
    因此需要一个持续的守护线程而不是"点一次亮一次"。
    """

    def __init__(self, controller: "InputController", interval: float = 15.0):
        self.ctl = controller
        self.interval = interval
        self.running = False
        self.wake_count = 0
        self.last_state = "Unknown"
        self.on_event = None          # 可选回调：on_event(text, level)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="keep-awake")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def _emit(self, text: str, level: str = "info") -> None:
        if self.on_event:
            try:
                self.on_event(text, level)
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.ctl.screen_state()
                self.last_state = state
                if state != "Awake":
                    self.ctl.wake(extend_timeout=True)
                    self.wake_count += 1
                    self._emit(f"检测到屏幕熄灭（{state}），已自动重新点亮"
                               f"（第 {self.wake_count} 次）", "warn")
            except Exception as exc:  # noqa: BLE001
                self._emit(f"保活检测异常：{exc}", "error")
            self._stop.wait(self.interval)


class InputQueue:
    """输入命令串行队列：后台线程按顺序执行 input 命令，主线程永不阻塞。

    背景：v1.x 在 Tk 主线程里同步执行 `input tap/swipe/keyevent`，每次都要
    启动一个 adb 子进程（Windows 上约 50~300ms），期间界面完全冻结，
    这是"控制很卡"的主要来源之一。本类把命令投递到后台线程按序执行，
    界面始终可响应；快速连续点击也会按顺序送达设备，不会乱序。

    用法：
        q = InputQueue(controller, on_error=...)
        q.submit(ctl.tap, x, y)
        q.submit(ctl.swipe, x1, y1, x2, y2, 300)
    """

    def __init__(self, controller: "InputController", on_error=None):
        self.ctl = controller
        self.on_error = on_error
        self.sent = 0
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="input-queue")
        self._thread.start()

    def submit(self, fn, *args, **kwargs) -> None:
        """投递一个输入操作（fn 为 InputController 的方法，如 ctl.tap）。"""
        self._q.put((fn, args, kwargs))

    def _run(self) -> None:
        while True:
            fn, args, kwargs = self._q.get()
            try:
                fn(*args, **kwargs)
                self.sent += 1
            except Exception as exc:  # noqa: BLE001 - 错误回传给界面层
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:  # noqa: BLE001
                        pass
