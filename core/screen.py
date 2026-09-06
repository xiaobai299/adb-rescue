# -*- coding: utf-8 -*-
"""投屏层：内置帧采集引擎 + 可选 scrcpy 外部引擎。

内置引擎（默认，零额外依赖）
----------------------------
通过 `adb exec-out screencap -p` 循环取帧，Pillow 解码后按设置做旋转/缩放，
以 PIL.Image 形式回调给界面。优点：兼容所有 Android 4.4+ 设备、无需 root、
无需安装任何东西；缺点：帧率受限于单次截图耗时（通常 3~15 FPS）。
对于"屏幕碎裂无法看清但需要操作"的救援场景，稳定性优先于流畅度。

scrcpy 引擎（可选）
--------------------
若本机存在 scrcpy，可一键启动独立窗口获得 30~60 FPS 与更低延迟；
此时建议勾选"仅镜像不控制"，由本工具的鼠标继续走 input 通道，避免双通道冲突。
"""

from __future__ import annotations

import io
import os
import queue
import subprocess
import threading
import time

from core.adb import IS_WINDOWS, AdbClient, AdbError, locate_scrcpy

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


# 画质档位：名称 -> (缩放系数, 重采样算法)
QUALITY_PRESETS: dict[str, tuple[float, int]] = {
    "流畅（省带宽）": (0.5, 0),   # 0 = NEAREST
    "标准": (0.75, 2),            # 2 = BILINEAR
    "高清": (1.0, 1),             # 1 = LANCZOS
}
if Image is not None:
    QUALITY_PRESETS = {
        "流畅（省带宽）": (0.5, Image.NEAREST),
        "标准": (0.75, Image.BILINEAR),
        "高清": (1.0, Image.LANCZOS),
    }

RESAMPLE_NAMES = {0: "NEAREST", 2: "BILINEAR", 1: "LANCZOS"}


class ScreenStreamer:
    """后台线程持续取帧，通过回调把 PIL.Image 交给界面。

    回调在**取帧线程**中执行，界面层需自行做线程安全处理（本项目使用
    `root.after(0, ...)` 或轮询队列）。为简化，这里同时提供线程安全的
    `latest` 属性与 `frames` 队列两种消费方式。
    """

    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial
        self.running = False
        self.thread: threading.Thread | None = None

        # 可调参数
        self.fps = 6                # 目标帧率（1-15）
        self.rotation = 0           # 0 / 90 / 180 / 270，顺时针
        self.quality_name = "标准"
        self.max_width = 0          # 0 = 不限制（按画质档位缩放）
        self.auto_rotate = False    # 依据设备方向自动旋转（需 Android 支持 content 查询）

        # 运行状态
        self.latest: Image.Image | None = None
        self.frames: queue.Queue = queue.Queue(maxsize=2)
        self.frame_count = 0
        self.error_count = 0
        self.last_error = ""
        self.actual_fps = 0.0
        self.capture_ms = 0
        self.device_width = 0
        self.device_height = 0

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.on_status = None  # 可选回调：on_status(text: str, level: str)

    # ---------------- 生命周期 ---------------- #

    def start(self) -> None:
        if self.running:
            return
        if Image is None:
            raise AdbError("缺少 Pillow 依赖", "请执行命令安装：pip install Pillow", "ImportError")
        self.running = True
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="screen-stream")
        self.thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.thread = None

    def _emit(self, text: str, level: str = "info") -> None:
        if self.on_status:
            try:
                self.on_status(text, level)
            except Exception:
                pass

    # ---------------- 取帧循环 ---------------- #

    def _loop(self) -> None:
        interval = 1.0 / max(1, self.fps)
        tick_times: list[float] = []
        while not self._stop.is_set():
            t0 = time.time()
            try:
                raw = self.adb.exec_out(self.serial, "screencap -p", timeout=15)
                img = Image.open(io.BytesIO(raw))
                img.load()
                if img.mode != "RGB":
                    img = img.convert("RGB")
                self.device_width, self.device_height = img.size
                frame = self._transform(img)
                with self._lock:
                    self.latest = frame
                try:
                    if self.frames.full():
                        self.frames.get_nowait()
                    self.frames.put_nowait(frame)
                except queue.Full:
                    pass
                self.frame_count += 1
                self.capture_ms = int((time.time() - t0) * 1000)
                self.error_count = 0
            except AdbError as exc:
                self.error_count += 1
                self.last_error = f"{exc.message} {exc.hint}".strip()
                if self.error_count in (1, 5, 20):
                    self._emit(f"取帧失败：{self.last_error}", "error")
                if self.error_count >= 30:
                    self._emit("连续取帧失败，已停止投屏。请检查设备连接后重新开始。", "error")
                    self.running = False
                    break
                time.sleep(0.5)
            except Exception as exc:  # 解码异常等
                self.error_count += 1
                self.last_error = f"图像解码失败：{exc}"
                time.sleep(0.3)

            tick_times.append(time.time())
            if len(tick_times) > 10:
                tick_times.pop(0)
            if len(tick_times) >= 3:
                span = tick_times[-1] - tick_times[0]
                self.actual_fps = round((len(tick_times) - 1) / span, 1) if span > 0 else 0.0

            wait = interval - (time.time() - t0)
            if wait > 0:
                self._stop.wait(wait)
        self.running = False

    # ---------------- 图像变换 ---------------- #

    @property
    def quality(self) -> tuple[float, int]:
        return QUALITY_PRESETS.get(self.quality_name, (0.75, 2))

    def _transform(self, img: Image.Image) -> Image.Image:
        if self.rotation:
            img = img.rotate(-self.rotation, expand=True)  # PIL 正角度为逆时针
        scale, resample = self.quality
        w, h = img.size
        if self.max_width and w > self.max_width:
            scale = min(scale, self.max_width / w)
        if scale < 0.999:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             resample if resample else Image.NEAREST)
        return img

    def display_size(self) -> tuple[int, int]:
        """当前设置下（旋转+缩放后）的帧尺寸。"""
        w, h = self.device_width or 1080, self.device_height or 2400
        if self.rotation in (90, 270):
            w, h = h, w
        scale, _ = self.quality
        if self.max_width and w > self.max_width:
            scale = min(scale, self.max_width / w)
        return max(1, int(w * scale)), max(1, int(h * scale))

    def device_size(self) -> tuple[int, int]:
        return self.device_width or 1080, self.device_height or 2400

    # ---------------- 坐标映射 ---------------- #

    def map_to_device(self, canvas_x: float, canvas_y: float,
                      offset_x: float, offset_y: float, disp_scale: float) -> tuple[int, int]:
        """把画布坐标反算为设备坐标。

        参数说明：
            offset_x/offset_y —— 画布中图像的左上角（居中留边产生的偏移）
            disp_scale        —— 图像到画布的缩放比
        变换顺序与 _transform 严格互逆：先缩放 → 再旋转。
        """
        ix = (canvas_x - offset_x) / disp_scale
        iy = (canvas_y - offset_y) / disp_scale
        w, h = self.device_size()
        r = self.rotation % 360
        if r == 0:
            dx, dy = ix, iy
        elif r == 90:    # 顺时针 90°：显示 (ix, iy) = (h-1-y, x)
            dx, dy = iy, (h - 1) - ix
        elif r == 180:
            dx, dy = (w - 1) - ix, (h - 1) - iy
        else:            # 270（等同逆时针 90°）：显示 (ix, iy) = (y, w-1-x)
            dx, dy = (w - 1) - iy, ix
        return int(max(0, min(w - 1, dx))), int(max(0, min(h - 1, dy)))


# --------------------------------------------------------------------------- #
# scrcpy 外部引擎
# --------------------------------------------------------------------------- #

class ScrcpyLauncher:
    """构建并启动 scrcpy 命令行。scrcpy 以独立窗口运行，本工具只负责参数拼装。"""

    #: 可选帧率档位（0 表示不限，跟随设备最大刷新率）
    FPS_PRESETS: dict[str, int] = {
        "不限（跟随设备）": 0,
        "120 FPS": 120,
        "90 FPS": 90,
        "60 FPS": 60,
        "30 FPS": 30,
    }

    def __init__(self, adb: AdbClient, scrcpy_path: str | None = None):
        self.adb = adb
        self.path = scrcpy_path or adb.scrcpy_path or locate_scrcpy() or ""
        self.proc: subprocess.Popen | None = None
        # 可选回调：scrcpy 进程自行退出（如用户直接关闭窗口）时通知界面层。
        # 在排空线程中回调（非主线程），界面层需自行调度回主线程。
        self.on_exit = None
        self._stopping = False  # 主动 stop() 后置位，避免误触 on_exit

    def available(self) -> bool:
        return bool(self.path) and os.path.isfile(self.path)

    def version(self) -> str:
        if not self.available():
            return "不可用"
        try:
            out = subprocess.run([self.path, "--version"], capture_output=True,
                                 text=True, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0)
            return (out.stdout or out.stderr).splitlines()[0].strip()
        except Exception:
            return "未知"

    def build_args(self, serial: str, *, max_size: int = 0, max_fps: int = 0,
                   bit_rate: str = "", rotation: int | None = None,
                   video_codec: str = "", turn_screen_off: bool = False,
                   no_control: bool = True, stay_awake: bool = True,
                   show_touches: bool = False, fullscreen: bool = False,
                   always_on_top: bool = True, no_audio: bool = True,
                   window_title: str = "", low_latency: bool = False,
                   tcpip: bool = False, audio: bool = False) -> list[str]:
        """拼装 scrcpy 命令行。max_fps=0 表示不限帧率（即可跑到设备的 120Hz）。

        与"直接双击 scrcpy.exe"保持一致的默认行为：USB 直连、8M 码率、
        默认显示缓冲 —— 这是画面最稳的配置（v2.0.2 实测直接运行不卡）。

        low_latency：减到最小显示缓冲（--video-buffer=0）并启用 baseline 编码。
                    注意：0 缓冲会取消抖动补偿，网络/编码稍有波动画面反而
                    会一顿一顿，仅适合追求极限低延迟的用户。
        tcpip：--tcpip 在 scrcpy 4.x 中会把**设备重连到无线（Wi-Fi）TCP/IP**，
               仅适合无线连接场景；USB 直连时请保持关闭（比默认通道慢）。
        audio：转发手机音频到电脑（需要 scrcpy 窗口直接控制模式才有意义）。
        """
        args = [self.path, "-s", serial]
        if max_size:
            args += ["--max-size", str(max_size)]
        # max_fps 为 0 时不传 --max-fps，让 scrcpy 跟随设备原生刷新率
        if max_fps:
            args += ["--max-fps", str(max_fps)]
        if bit_rate:
            args += ["--video-bit-rate", bit_rate]
        if video_codec:
            args += ["--video-codec", video_codec]
        if low_latency:
            args += ["--video-buffer=0"]
            if video_codec == "h264":
                args += ["--video-codec-options=profile=baseline,level=4"]
        if tcpip:
            args.append("--tcpip")
        # rotation 为 0（默认）时不传参，与直接运行行为一致
        if rotation:
            args += ["--rotation", str(rotation)]
        if turn_screen_off:
            args.append("--turn-screen-off")
        if no_control:
            args.append("--no-control")   # 控制统一由本工具的 input 通道负责
        # scrcpy 4.x 明确拒绝「禁用控制 + 保持唤醒」的组合，
        # 因此仅在 scrcpy 具备控制权时才传 --stay-awake，
        # 其它情况改由 adb 的 `svc power stayon usb` 实现屏幕常亮。
        if stay_awake and not no_control:
            args.append("--stay-awake")
        if show_touches:
            args.append("--show-touches")
        if fullscreen:
            args.append("--fullscreen")
        if always_on_top:
            args.append("--always-on-top")
        if no_audio and not audio:
            args.append("--no-audio")
        if window_title:
            args += ["--window-title", window_title]
        return args

    def launch(self, serial: str, **kwargs) -> subprocess.Popen:
        if not self.available():
            raise AdbError(
                "未找到 scrcpy",
                "scrcpy 是可选组件。不安装也能正常使用本工具的『内置投屏引擎』；"
                "如需高帧率，可到 https://github.com/Genymobile/scrcpy 下载后在设置中指定路径。",
            )
        args = self.build_args(serial, **kwargs)
        # 屏幕一旦熄灭，采集帧率会掉到个位数（实测息屏时约 0~10 FPS）。
        # 因此启动前先点亮屏幕并维持常亮：scrcpy 有控制权时由它自己保持，
        # 否则用免 root 的 `svc power stayon usb`。
        if kwargs.get("stay_awake", True):
            try:
                self.adb.shell(serial, "input keyevent 224", timeout=15)  # KEYCODE_WAKEUP
            except Exception:
                pass
            if kwargs.get("no_control", True):
                try:
                    self.adb.shell(serial, "svc power stayon usb", timeout=15)
                except Exception:
                    pass
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        self._stopping = False
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        # 关键：必须排空 scrcpy 的 stdout，否则管道写满后 scrcpy 会阻塞，
        # 导致整个视频管线卡死（症状与"电脑显示卡"一致）。
        self._drain = threading.Thread(target=self._drain_output, daemon=True,
                                       name="scrcpy-drain")
        self._drain.start()
        return self.proc

    def _drain_output(self) -> None:
        """后台持续读取 scrcpy 输出，避免 stdout 管道写满阻塞视频管线。"""
        try:
            if self.proc and self.proc.stdout:
                while True:
                    chunk = self.proc.stdout.read(4096)
                    if not chunk:
                        break
        except Exception:  # noqa: BLE001
            pass
        # 进程自然退出（stop() 会先置 _stopping 并把 proc 置 None）时通知界面层
        if (self.on_exit and not self._stopping
                and self.proc is not None and self.proc.poll() is not None):
            try:
                self.on_exit()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
