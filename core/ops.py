# -*- coding: utf-8 -*-
"""运维层：APK 管理、截图录屏、logcat、shell、电源与系统信息。

风险分级
--------
本模块把操作分为 safe / warn / danger 三档，界面层据此决定是否弹二次确认：
    safe   —— 只读或可逆
    warn   —— 会改变设备状态（安装、卸载、启动应用等）
    danger —— 不可逆或影响数据（卸载应用数据、重启、恢复出厂等）
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time

from core.adb import IS_WINDOWS, AdbClient, AdbError, shell_quote

RISK_SAFE = "safe"
RISK_WARN = "warn"
RISK_DANGER = "danger"


# --------------------------------------------------------------------------- #
# 应用管理
# --------------------------------------------------------------------------- #

class AppManager:
    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial

    def _shell(self, cmd: str, timeout: int = 60) -> str:
        res = self.adb.shell(self.serial, cmd, timeout=timeout)
        return res.stdout or res.stderr or ""

    def list_packages(self, third_party: bool = True) -> list[tuple[str, str]]:
        """返回 [(包名, apk 路径)]。"""
        flag = "-3" if third_party else "-s"
        out = self._shell(f"pm list packages -f {flag}", timeout=60)
        result: list[tuple[str, str]] = []
        for line in out.splitlines():
            m = re.match(r"package:(.+)=(.+)", line.strip())
            if m:
                result.append((m.group(2).strip(), m.group(1).strip()))
        result.sort(key=lambda x: x[0])
        return result

    def package_label(self, package: str) -> str:
        """尝试获取应用中文名（通过 dumpsys package 的 application-label，部分版本可用）。"""
        out = self._shell(f"dumpsys package {shell_quote(package)}", timeout=30)
        for pattern in (r"application-label-zh-CN:'([^']+)'", r"application-label:'([^']+)'"):
            m = re.search(pattern, out)
            if m:
                return m.group(1)
        return ""

    def install(self, apk_path: str, *, reinstall: bool = True, allow_test: bool = True,
                allow_downgrade: bool = False, grant_permissions: bool = True,
                timeout: int = 300) -> str:
        args = ["-s", self.serial, "install", "-r" if reinstall else "",
                "-t" if allow_test else "", "-d" if allow_downgrade else "",
                "-g" if grant_permissions else "", os.path.abspath(apk_path)]
        args = [a for a in args if a]
        res = self.adb.raw(args, timeout=timeout)
        text = res.stdout or res.stderr or ""
        if "Success" in text:
            return "安装成功"
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in text:
            raise AdbError("安装失败：签名冲突",
                           "设备上已存在同名但签名不同的应用。请先卸载旧版本再安装（卸载会清除数据）。", text)
        if "INSTALL_FAILED_INSUFFICIENT_STORAGE" in text:
            raise AdbError("安装失败：存储空间不足", "请清理设备存储后重试。", text)
        if "INSTALL_FAILED_VERSION_DOWNGRADE" in text:
            raise AdbError("安装失败：版本低于已安装版本", "请勾选『允许降级安装（-d）』后重试。", text)
        if "INSTALL_FAILED_USER_RESTRICTED" in text:
            raise AdbError("安装被设备策略阻止",
                           "请检查『设置-开发者选项』中是否开启了『USB 调试（安全设置）』或"
                           "'通过 USB 验证应用'，部分 ROM 需要开启『允许通过 USB 安装应用』。", text)
        raise AdbError("安装失败", self.adb.explain(text), text)

    def uninstall(self, package: str, keep_data: bool = False) -> str:
        flag = "-k" if keep_data else ""
        res = self.adb.raw(["-s", self.serial, "uninstall", flag, package], timeout=180)
        text = res.stdout or res.stderr or ""
        if "Success" in text:
            return "卸载成功"
        raise AdbError("卸载失败", self.adb.explain(text) or
                       "系统应用无法直接卸载，可改用『禁用（隐藏）系统应用』。", text)

    def disable(self, package: str, enable: bool = False) -> str:
        action = "enable" if enable else "disable-user"
        out = self._shell(f"pm {action} --user 0 {shell_quote(package)}", timeout=60)
        if "Error" in out or "error" in out:
            raise AdbError("操作失败", self.adb.explain(out), out)
        return "已启用" if enable else "已禁用"

    def clear_data(self, package: str) -> str:
        """清空应用数据 —— 危险操作。"""
        out = self._shell(f"pm clear {shell_quote(package)}", timeout=120)
        if "Success" in out:
            return "数据已清除"
        raise AdbError("清除数据失败", self.adb.explain(out), out)

    def backup_apk(self, package: str, dest_dir: str) -> list[str]:
        """把已安装应用的 APK 导出到本地（免 root）。"""
        out = self._shell(f"pm path {shell_quote(package)}", timeout=30)
        paths = [line.split(":", 1)[1].strip()
                 for line in out.splitlines() if line.startswith("package:")]
        if not paths:
            raise AdbError("未找到该应用的 APK 路径", "应用可能已被卸载或包名不正确。", out)
        os.makedirs(dest_dir, exist_ok=True)
        saved: list[str] = []
        version = self._shell(f"dumpsys package {shell_quote(package)} | grep versionName | head -1", timeout=30)
        m = re.search(r"versionName=([^\s]+)", version)
        suffix = f"_{m.group(1)}" if m else ""
        for idx, remote in enumerate(paths):
            name = f"{package}{suffix}" + (f"_split{idx}" if len(paths) > 1 else "") + ".apk"
            self.adb.raw(["-s", self.serial, "pull", remote,
                          os.path.join(dest_dir, name)], timeout=600)
            saved.append(os.path.join(dest_dir, name))
        return saved

    def launch(self, package: str, activity: str = "") -> str:
        if activity:
            out = self._shell(f"am start -n {package}/{activity}", timeout=30)
        else:
            out = self._shell(
                f"cmd package resolve-activity --brief {shell_quote(package)}", timeout=30)
            last = out.strip().splitlines()[-1].strip() if out.strip() else ""
            if "/" in last:
                out = self._shell(f"am start -n {last}", timeout=30)
            else:
                out = self._shell(
                    f"monkey -p {shell_quote(package)} -c android.intent.category.LAUNCHER 1",
                    timeout=30)
        if "Error" in out or "error type" in out.lower():
            raise AdbError("启动失败", self.adb.explain(out) or "该应用可能没有可启动的界面。", out)
        return out.strip().splitlines()[-1] if out.strip() else "已发送启动指令"

    def force_stop(self, package: str) -> str:
        self._shell(f"am force-stop {shell_quote(package)}", timeout=30)
        return "已停止"

    # ---------------- adb backup（兼容老设备，需手机端确认） ---------------- #

    def adb_backup(self, package: str, dest_file: str, include_apk: bool = True,
                   timeout: int = 900) -> str:
        args = ["-s", self.serial, "backup"]
        args += ["-apk"] if include_apk else ["-noapk"]
        args += ["-nosystem", "-f", dest_file, package]
        res = self.adb.raw(args, timeout=timeout)
        text = res.stdout or res.stderr or ""
        if os.path.isfile(dest_file) and os.path.getsize(dest_file) > 0:
            return f"备份完成：{dest_file}"
        raise AdbError(
            "备份未生成文件",
            "adb backup 需要手机端点击『备份我的数据』确认；若屏幕损坏无法点按，"
            "请在投屏画面中先唤醒设备再重试，或改用『导出应用外置数据』。", text)


# --------------------------------------------------------------------------- #
# 截图 / 录屏
# --------------------------------------------------------------------------- #

class ScreenCapture:
    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial
        self._recording = False
        self._remote_path = "/sdcard/screenrecord.mp4"

    def screenshot(self, local_path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        data = self.adb.exec_out(self.serial, "screencap -p", timeout=30)
        if not data.startswith(b"\x89PNG"):
            raise AdbError("截图数据异常", "设备返回了非 PNG 数据，可能正处于息屏或 DRM 保护界面。",
                           data[:64].decode("latin-1", errors="replace"))
        with open(local_path, "wb") as fp:
            fp.write(data)
        return local_path

    def start_record(self, *, time_limit: int = 180, size: str = "",
                     bit_rate: str = "", remote_path: str = "/sdcard/screenrecord.mp4") -> None:
        """后台启动录屏。time_limit 为设备端自动停止上限。"""
        self._remote_path = remote_path
        cmd = f"screenrecord --time-limit {int(time_limit)}"
        if size:
            cmd += f" --size {size}"
        if bit_rate:
            cmd += f" --bit-rate {bit_rate}"
        cmd += f" {remote_path}"
        out = self.adb.shell(self.serial, f"nohup {cmd} >/dev/null 2>&1 & echo started", timeout=20).output
        if "started" not in out.lower():
            # 某些设备的 shell 不接受 nohup + &，退化为前台阻塞调用（交由调用方控制停止）
            raise AdbError("录屏启动失败", self.adb.explain(out), out)
        self._recording = True

    def stop_record(self, local_path: str) -> str:
        """停止录屏并拉取到本地。"""
        self._recording = False
        for cmd in ("pkill -2 screenrecord", "pkill -l 2 screenrecord",
                    "kill -2 $(pidof screenrecord)"):
            self.adb.shell(self.serial, cmd, timeout=15)
            time.sleep(0.8)
        time.sleep(0.8)
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        res = self.adb.raw(["-s", self.serial, "pull", self._remote_path, local_path], timeout=600)
        text = res.stdout or res.stderr or ""
        if not os.path.isfile(local_path):
            raise AdbError("录制文件未生成", self.adb.explain(text) or
                           "设备可能不支持 screenrecord 或时间过短。", text)
        return local_path

    @property
    def recording(self) -> bool:
        return self._recording


# --------------------------------------------------------------------------- #
# logcat
# --------------------------------------------------------------------------- #

LOG_LEVELS = ["V", "D", "I", "W", "E", "F", "S"]


class LogcatReader:
    """后台读取 logcat，按行推入队列。"""

    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.lines: queue.Queue = queue.Queue(maxsize=5000)
        self.running = False
        self.on_line = None
        self.on_exit = None

    def start(self, *, filter_spec: str = "", keyword: str = "", pid: str = "",
              clear_first: bool = True, since_boot: bool = False) -> None:
        if self.running:
            return
        if clear_first:
            self.adb.raw(["-s", self.serial, "logcat", "-c"], timeout=20)
        args = ["-s", self.serial, "logcat", "-v", "threadtime"]
        if since_boot:
            args.append("-b")
            args.append("all")
        if pid:
            args += ["--pid", pid]
        if filter_spec:
            args += filter_spec.split()
        self.proc = subprocess.Popen(
            [self.adb.adb_path] + args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, args=(keyword,), daemon=True)
        self.thread.start()

    def _read_loop(self, keyword: str) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            if keyword and keyword.lower() not in line.lower():
                continue
            try:
                if self.lines.full():
                    self.lines.get_nowait()
                self.lines.put_nowait(line)
            except queue.Full:
                pass
            if self.on_line:
                try:
                    self.on_line(line)
                except Exception:
                    pass
        self.running = False
        if self.on_exit:
            try:
                self.on_exit()
            except Exception:
                pass

    def stop(self) -> None:
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def dump(self, local_path: str, timeout: int = 30) -> str:
        res = self.adb.raw(["-s", self.serial, "logcat", "-d"], timeout=timeout)
        with open(local_path, "w", encoding="utf-8", errors="replace") as fp:
            fp.write(res.stdout or res.stderr or "")
        return local_path


# --------------------------------------------------------------------------- #
# 电源 / 系统
# --------------------------------------------------------------------------- #

class PowerOps:
    """重启类操作——全部为高风险，界面层必须二次确认。"""

    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial

    def _shell(self, cmd: str, timeout: int = 30) -> str:
        return self.adb.shell(self.serial, cmd, timeout=timeout).output

    def reboot(self) -> str:
        self.adb.raw(["-s", self.serial, "reboot"], timeout=60)
        return "已发送重启指令"

    def reboot_recovery(self) -> str:
        self.adb.raw(["-s", self.serial, "reboot", "recovery"], timeout=60)
        return "已重启至 Recovery 模式"

    def reboot_bootloader(self) -> str:
        self.adb.raw(["-s", self.serial, "reboot", "bootloader"], timeout=60)
        return "已重启至 Fastboot/Bootloader 模式"

    def shutdown(self) -> str:
        out = self._shell("reboot -p")
        if "permission denied" in out.lower():
            raise AdbError("关机失败", "部分设备需要 root 权限才能远程关机，请长按电源键关机。", out)
        return "已发送关机指令"

    def battery_info(self) -> dict[str, str]:
        out = self._shell("dumpsys battery")
        info: dict[str, str] = {}
        mapping = {
            "level": "电量", "scale": "满电刻度", "status": "状态", "health": "健康度",
            "present": "电池存在", "technology": "电池类型", "temperature": "温度",
            "voltage": "电压", "current now": "瞬时电流", "power save": "省电模式",
        }
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k in mapping:
                if k == "temperature":
                    try:
                        v = f"{int(v) / 10:.1f} ℃"
                    except ValueError:
                        pass
                if k == "status":
                    v = {"2": "充电中", "3": "放电中", "4": "未充电", "5": "已充满"}.get(v, v)
                if k == "health":
                    v = {"1": "未知", "2": "良好", "3": "过热", "4": "损坏", "5": "过压",
                         "6": "未知故障", "7": "过冷"}.get(v, v)
                if k == "power save":
                    v = "开启" if v == "true" else "关闭"
                info[mapping[k]] = v
        return info

    def storage_info(self) -> dict[str, str]:
        info: dict[str, str] = {}
        out = self._shell("df -h /sdcard /data /storage/emulated 2>/dev/null")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 5:
                info[parts[-1]] = f"已用 {parts[2]} / 共 {parts[1]}，可用 {parts[3]}（使用率 {parts[4]}）"
        if not info:
            info["提示"] = "设备未返回存储信息，请使用『文件导出』页查看具体目录。"
        return info

    def memory_info(self) -> dict[str, str]:
        out = self._shell("cat /proc/meminfo")
        info: dict[str, str] = {}
        for key, label in (("MemTotal", "总内存"), ("MemAvailable", "可用内存"),
                           ("SwapTotal", "交换分区总量")):
            m = re.search(rf"{key}:\s*(\d+) kB", out)
            if m:
                info[label] = f"{int(m.group(1)) / 1024 / 1024:.2f} GB"
        return info

    def device_report(self) -> dict[str, str]:
        """一键体检：汇总关键状态，便于判断是否适合继续救援操作。"""
        report: dict[str, str] = {}
        report["屏幕状态"] = "点亮" if "Awake" in self._shell("dumpsys power") else "熄灭"
        report["电池"] = self.battery_info().get("电量", "?") + "%"
        report["存储"] = next(iter(self.storage_info().values()), "未知")
        report["内存"] = self.memory_info().get("可用内存", "未知") + " 可用"
        uptime = self._shell("cat /proc/uptime").split()
        if uptime:
            try:
                secs = float(uptime[0])
                report["已开机"] = f"{int(secs // 3600)} 小时 {int(secs % 3600 // 60)} 分钟"
            except ValueError:
                pass
        return report
