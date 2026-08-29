# -*- coding: utf-8 -*-
"""设备通信层：adb 可执行文件定位、命令执行、设备枚举与信息采集。

本层是唯一直接调用 adb 可执行文件的地方（screen/files/ops 均通过本层）。
所有对外接口统一返回 (ok, data, error) 风格或抛出 AdbError，便于上层集中处理
"未找到 adb / 设备离线 / 权限不足" 三类高频故障。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------- #
# 异常与数据模型
# --------------------------------------------------------------------------- #


class AdbError(Exception):
    """ADB 通信失败。message 字段必须为中文，可直接展示给用户。"""

    def __init__(self, message: str, hint: str = "", detail: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint  # 排查建议
        self.detail = detail  # 原始 stderr / 命令


@dataclass
class Device:
    """一台已连接的设备/模拟器。"""

    serial: str
    state: str = "unknown"          # device / unauthorized / offline / recovery / sideload / unknown
    model: str = ""
    brand: str = ""
    device_code: str = ""
    transport_id: str = ""

    # 运行期补充信息
    android_version: str = ""
    sdk_level: str = ""
    resolution: str = ""            # "1080x2400"
    override_resolution: str = ""   # wm size 被改写时
    density: str = ""
    battery_level: str = ""
    battery_status: str = ""        # 充电中/放电中/已充满/未知
    charging: str = ""              # USB / AC / 无线 / 未充电
    storage_summary: str = ""
    memory_summary: str = ""
    screen_on: bool | None = None
    rooted: bool = False
    # root 详情（v2.0，由 collect_root_info 填充）
    root_method: str = "none"       # magisk / supersu / adb-root / none
    magisk_version: str = ""
    selinux: str = "unknown"        # Enforcing / Permissive
    adb_root_ok: bool = False       # ro.debuggable=1（可 adb root）
    adbd_root: bool = False         # adbd 已以 root 运行

    @property
    def display_name(self) -> str:
        name = self.model or self.device_code or self.serial
        return f"{name}（{self.serial}）"

    @property
    def online(self) -> bool:
        return self.state == "device"

    @property
    def state_text(self) -> str:
        return {
            "device": "已授权 · 可操控",
            "unauthorized": "未授权 USB 调试",
            "offline": "设备离线",
            "recovery": "Recovery 模式",
            "sideload": "Sideload 模式",
            "unknown": "状态未知",
        }.get(self.state, self.state)


@dataclass
class CommandResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    code: int = 0
    data: bytes = b""

    @property
    def output(self) -> str:
        return (self.stdout or "").strip() or (self.stderr or "").strip()


# --------------------------------------------------------------------------- #
# adb 可执行文件定位
# --------------------------------------------------------------------------- #

IS_WINDOWS = os.name == "nt"


def _exe(name: str) -> str:
    return name + (".exe" if IS_WINDOWS else "")


def app_base_dir() -> str:
    """程序根目录：源码运行时为项目根；PyInstaller 打包后为 exe 所在目录
    （onedir 模式）/ 解压临时目录（onefile 模式），tools/ 放在这里随包携带。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            base = meipass.rstrip("\\/")
            # onedir：_MEIPASS = <exe目录>/_internal → 返回 exe 所在目录
            if os.path.basename(base) == "_internal":
                return os.path.dirname(base)
            return base  # onefile：临时解压目录
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def candidate_adb_paths() -> list[str]:
    """返回所有可能的 adb 路径（按优先级从高到低）。"""
    exe = _exe("adb")
    paths: list[str] = []

    # 1. 程序根目录下的 tools/（源码项目或打包后的 exe 同级目录，便于绿色版随包携带）
    here = app_base_dir()
    paths.append(os.path.join(here, "tools", exe))
    paths.append(os.path.join(here, exe))

    # 2. 环境变量
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK_HOME"):
        root = os.environ.get(var)
        if root:
            paths.append(os.path.join(root, "platform-tools", exe))

    # 3. 用户目录下常见 SDK 位置
    home = os.path.expanduser("~")
    paths += [
        os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools", exe),
        os.path.join(home, "Android", "Sdk", "platform-tools", exe),
        os.path.join(home, "AppData", "Local", "Android", "sdk", "platform-tools", exe),
        os.path.join(home, "scrcpy", exe),
    ]

    # 4. 常见解压位置
    for drive in ("C", "D", "E"):
        paths += [
            f"{drive}:\\Android\\platform-tools\\{exe}",
            f"{drive}:\\platform-tools\\{exe}",
            f"{drive}:\\adb\\{exe}",
        ]

    # 5. PATH
    found = shutil.which("adb")
    if found:
        paths.append(found)

    # 去重且保留顺序
    seen, result = set(), []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def locate_adb() -> str | None:
    """在常见位置中查找可用的 adb，返回第一个命中路径。"""
    for path in candidate_adb_paths():
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def candidate_scrcpy_paths() -> list[str]:
    """scrcpy 候选路径，优先使用随包自带的 tools/scrcpy。

    同时扫描各盘符根目录下的 scrcpy* 文件夹（例如用户自行下载的
    D:\\scrcpy-win64-v4.1\\scrcpy.exe），方便直接复用电脑上已有的副本。
    """
    exe = _exe("scrcpy")
    here = app_base_dir()
    home = os.path.expanduser("~")
    paths = [
        os.path.join(here, "tools", "scrcpy", exe),
        os.path.join(here, "tools", exe),
        os.path.join(here, exe),
        os.path.join(home, "scrcpy", exe),
        os.path.join(home, "scoop", "apps", "scrcpy", "current", exe),
        os.path.join(home, "AppData", "Local", "scrcpy", exe),
        os.path.join(home, "AppData", "Local", "Programs", "scrcpy", exe),
        r"C:\Program Files\scrcpy\scrcpy.exe",
        r"C:\Program Files (x86)\scrcpy\scrcpy.exe",
        r"C:\scrcpy\scrcpy.exe",
    ]
    # 各盘符根目录下的 scrcpy* 目录（D:\scrcpy-win64-v4.1 等常见绿色版）
    for drive in ("C", "D", "E"):
        for p in glob.glob(f"{drive}:\\scrcpy*\\{exe}"):
            paths.append(p)
        for p in glob.glob(f"{drive}:\\scrcpy*\\*\\{exe}"):
            paths.append(p)
    found = shutil.which("scrcpy")
    if found:
        paths.append(found)
    seen, result = set(), []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            result.append(p)
    return [p for p in result if os.path.isfile(p)]


def locate_scrcpy() -> str | None:
    paths = candidate_scrcpy_paths()
    return paths[0] if paths else None


# --------------------------------------------------------------------------- #
# ADB 客户端
# --------------------------------------------------------------------------- #


class AdbClient:
    """adb 命令执行器。所有与设备交互的入口。"""

    def __init__(self, adb_path: str | None = None, scrcpy_path: str | None = None):
        self.adb_path = adb_path or locate_adb() or _exe("adb")
        self.scrcpy_path = scrcpy_path or (candidate_scrcpy_paths() or [None])[0]
        self.default_timeout = 20

    # ---------------- 基础执行 ---------------- #

    def _startup_info(self):
        if not IS_WINDOWS:
            return None
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return si

    def raw(
        self,
        args: list[str],
        timeout: int | None = None,
        binary: bool = False,
        stdin: bytes | None = None,
    ) -> CommandResult:
        """执行 adb 命令。args 不含 'adb' 本体。"""
        cmd = [self.adb_path] + list(args)
        try:
            proc = subprocess.run(
                cmd,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.default_timeout,
                startupinfo=self._startup_info(),
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
        except FileNotFoundError:
            raise AdbError(
                f"未找到 adb 可执行文件：{self.adb_path}",
                "请在顶部『设置』中手动指定 adb 路径；或下载 Android SDK platform-tools "
                "后把目录加入 PATH。",
                " ".join(cmd),
            )
        except subprocess.TimeoutExpired:
            raise AdbError(
                "adb 命令执行超时",
                "设备可能无响应或 USB 线接触不良。请重新插拔数据线，"
                "并在手机上确认『允许 USB 调试』弹窗。",
                " ".join(cmd),
            )
        except OSError as exc:
            raise AdbError(f"adb 启动失败：{exc}", "请检查 adb 路径是否正确、文件是否完整。", " ".join(cmd))

        if binary:
            out, err = b"", self._decode(proc.stderr)
            data = proc.stdout
        else:
            data = b""
            out = self._decode(proc.stdout)
            err = self._decode(proc.stderr)

        return CommandResult(ok=proc.returncode == 0, stdout=out, stderr=err, code=proc.returncode, data=data)

    @staticmethod
    def _decode(raw: bytes) -> str:
        """解码文本输出，并统一换行符（Windows 下 adb 可能返回 CRLF）。"""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    # ---------------- 设备级命令 ---------------- #

    def adb(self, args: list[str], timeout: int | None = None, binary: bool = False) -> CommandResult:
        return self.raw(args, timeout=timeout, binary=binary)

    def shell(self, serial: str, command: str, timeout: int | None = None,
              check: bool = False, binary: bool = False) -> CommandResult:
        """在指定设备上执行 shell 命令。

        注意：命令以**单个字符串**传给设备端 shell，因此调用方需自行处理引号。
        """
        res = self.raw(["-s", serial, "shell", command], timeout=timeout, binary=binary)
        if check and not res.ok:
            raise AdbError("命令执行失败", self.explain(res.output), command)
        return res

    def exec_out(self, serial: str, command: str, timeout: int | None = None) -> bytes:
        """以二进制方式取回命令输出（不经过设备端 shell 转义/换行转换）。"""
        res = self.raw(["-s", serial, "exec-out", command], timeout=timeout, binary=True)
        if not res.data:
            raise AdbError("未取到任何数据", "设备可能离线或命令不被支持。", command)
        return res.data

    # ---------------- 服务器 / 设备枚举 ---------------- #

    def start_server(self) -> bool:
        res = self.raw(["start-server"], timeout=60)
        return res.ok

    def kill_server(self) -> bool:
        return self.raw(["kill-server"], timeout=30).ok

    def version(self) -> str:
        try:
            res = self.raw(["version"], timeout=15)
            first = (res.stdout or res.stderr).splitlines()
            return first[0].strip() if first else "未知"
        except AdbError:
            return "不可用"

    def devices(self) -> list[Device]:
        """枚举设备。若 adb 未运行会先尝试启动服务。"""
        res = self.raw(["devices", "-l"], timeout=30)
        text = res.stdout or ""
        if "daemon started" in text or ("List of devices" not in text and "List of devices attached" not in text):
            # 首次启动 daemon 时输出可能不含设备列表，重来一次
            res = self.raw(["devices", "-l"], timeout=30)
            text = res.stdout or ""

        devices: list[Device] = []
        started = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("List of devices"):
                started = True
                continue
            if not started:
                continue
            if line.startswith("*") or "daemon" in line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            dev = Device(serial=serial, state=state)
            for token in parts[2:]:
                if ":" in token:
                    k, v = token.split(":", 1)
                    if k == "model":
                        dev.model = v.replace("_", " ")
                    elif k == "device":
                        dev.device_code = v
                    elif k == "product":
                        dev.device_code = dev.device_code or v
                    elif k == "transport_id":
                        dev.transport_id = v
            devices.append(dev)
        return devices

    # ---------------- 信息采集 ---------------- #

    def get_prop(self, serial: str, key: str) -> str:
        res = self.shell(serial, f"getprop {key}", timeout=10)
        return res.stdout.strip() if res.ok else ""

    #: 一次 shell 调用取回全部关键属性，减少 USB 往返（慢设备/扩展坞下差异明显）
    INFO_BATCH = (
        "getprop;echo __S1__;wm size;wm density;echo __S2__;dumpsys battery;"
        "echo __S3__;df /sdcard /storage/emulated 2>/dev/null|tail -n 2;"
        "echo __S4__;cat /proc/meminfo;echo __S5__;dumpsys power;echo __S6__;id"
    )

    def collect_info(self, serial: str) -> Device:
        """采集一台设备的完整信息（单次批处理，失败时自动退回逐项查询）。"""
        dev = Device(serial=serial, state="device")
        sections = []
        try:
            out = self.shell(serial, self.INFO_BATCH, timeout=30).stdout
            sections = out.split("__S")
        except AdbError:
            sections = []
        if len(sections) >= 7:
            self._parse_batch(dev, sections)
            return dev

        # 退回逐项查询
        dev.brand = self.get_prop(serial, "ro.product.brand")
        dev.model = (
            self.get_prop(serial, "ro.product.marketname")
            or self.get_prop(serial, "ro.product.model")
            or self.get_prop(serial, "ro.product.vendor.model")
        )
        dev.device_code = self.get_prop(serial, "ro.product.device")
        dev.android_version = self.get_prop(serial, "ro.build.version.release")
        dev.sdk_level = self.get_prop(serial, "ro.build.version.sdk")
        res = self.shell(serial, "wm size", timeout=10)
        m_phys = re.search(r"Physical size:\s*(\d+x\d+)", res.stdout)
        m_over = re.search(r"Override size:\s*(\d+x\d+)", res.stdout)
        if m_phys:
            dev.resolution = m_phys.group(1)
        if m_over:
            dev.override_resolution = m_over.group(1)
        res = self.shell(serial, "wm density", timeout=10)
        m_den = re.search(r"Physical density:\s*(\d+)", res.stdout)
        if m_den:
            dev.density = m_den.group(1)
        res = self.shell(serial, "dumpsys battery", timeout=15)
        self._parse_battery(dev, res.stdout)
        res = self.shell(serial, "df /sdcard /storage/emulated 2>/dev/null | tail -n 2", timeout=15)
        self._parse_storage(dev, res.stdout)
        res = self.shell(serial, "cat /proc/meminfo", timeout=10)
        self._parse_memory(dev, res.stdout)
        res = self.shell(serial, "dumpsys power", timeout=15)
        m = re.search(r"mWakefulness=(\w+)", res.stdout)
        if m:
            dev.screen_on = m.group(1) in ("Awake", "Dreaming")
        res = self.shell(serial, "id", timeout=10)
        dev.rooted = "uid=0" in res.stdout
        return dev

    #: root 详情采集（v2.0）。先确认 su 二进制存在再调用，避免误触发 Magisk 授权弹窗
    ROOT_BATCH = (
        "command -v su;echo __R0__;su -c id 2>&1;echo __R1__;magisk -v 2>&1;"
        "echo __R2__;getenforce 2>&1;echo __R3__;getprop ro.debuggable;echo __R4__;id"
    )

    def collect_root_info(self, dev: Device) -> Device:
        """采集 root 详情并写回 dev（失败时保持默认值，不影响主流程）。"""
        try:
            out = self.shell(dev.serial, self.ROOT_BATCH, timeout=25).stdout
        except AdbError:
            return dev
        secs = out.split("__R")
        if len(secs) >= 6:
            self._parse_root_batch(dev, secs)
        return dev

    def _parse_root_batch(self, dev: Device, secs: list[str]) -> None:
        su_path = (secs[0] or "").strip()
        su_out = secs[1] or ""
        magisk = (secs[2] or "").strip()
        selinux = (secs[3] or "").strip()
        debuggable = (secs[4] or "").strip()
        id_out = secs[5] or ""

        dev.rooted = "uid=0" in su_out or "uid=0" in id_out
        if "uid=0" in id_out:
            dev.adbd_root = True
        if su_path or "uid=0" in su_out:
            dev.root_method = "magisk" if magisk else ("supersu" if "uid=0" in su_out else "none")
            dev.magisk_version = magisk
        if dev.adbd_root:
            # adbd 已以 root 运行时优先标注为 adb-root（无需再依赖 su）
            dev.root_method = "adb-root"
        dev.selinux = selinux or "unknown"
        dev.adb_root_ok = debuggable.strip() == "1"
        if not dev.rooted:
            dev.root_method = "none"

    def _parse_batch(self, dev: Device, sections: list[str]) -> None:
        """解析批处理命令的各分段。sections[k] 对应 __S{k}__ 之前的内容。"""
        props = dict(re.findall(r"^\[([^\]]+)\]:\s*\[(.*)\]$", sections[0], re.M))
        dev.brand = props.get("ro.product.brand", "")
        dev.model = (props.get("ro.product.marketname")
                     or props.get("ro.product.model")
                     or props.get("ro.product.vendor.model")
                     or "")
        dev.device_code = props.get("ro.product.device", "")
        dev.android_version = props.get("ro.build.version.release", "")
        dev.sdk_level = props.get("ro.build.version.sdk", "")

        wm = sections[1]
        m_phys = re.search(r"Physical size:\s*(\d+x\d+)", wm)
        m_over = re.search(r"Override size:\s*(\d+x\d+)", wm)
        m_den = re.search(r"Physical density:\s*(\d+)", wm)
        if m_phys:
            dev.resolution = m_phys.group(1)
        if m_over:
            dev.override_resolution = m_over.group(1)
        if m_den:
            dev.density = m_den.group(1)

        self._parse_battery(dev, sections[2])
        self._parse_storage(dev, sections[3])
        self._parse_memory(dev, sections[4])

        m = re.search(r"mWakefulness=(\w+)", sections[5])
        if m:
            dev.screen_on = m.group(1) in ("Awake", "Dreaming")
        dev.rooted = "uid=0" in sections[6]

    @staticmethod
    def _parse_battery(dev: Device, out: str) -> None:
        m_lv = re.search(r"^\s*level:\s*(\d+)", out, re.M)
        m_sc = re.search(r"^\s*scale:\s*(\d+)", out, re.M)
        if m_lv:
            scale = int(m_sc.group(1)) if m_sc else 100
            try:
                dev.battery_level = str(round(int(m_lv.group(1)) * 100 / scale))
            except ValueError:
                dev.battery_level = m_lv.group(1)
        code = ""
        m_st = re.search(r"^\s*status:\s*(\d+)", out, re.M)
        if m_st:
            code = m_st.group(1)
        dev.battery_status = {"2": "充电中", "3": "放电中", "4": "未充电", "5": "已充满"}.get(code, "未知")
        if re.search(r"^\s*AC powered:\s*true", out, re.M):
            dev.charging = "充电器"
        elif re.search(r"^\s*USB powered:\s*true", out, re.M):
            dev.charging = "USB"
        elif re.search(r"^\s*Wireless powered:\s*true", out, re.M):
            dev.charging = "无线"
        else:
            dev.charging = "未充电"

    @staticmethod
    def _parse_storage(dev: Device, out: str) -> None:
        for line in out.splitlines():
            nums = re.findall(r"(\d+(?:\.\d+)?)([KMGT]?)", line)
            if len(nums) >= 3:
                try:
                    total = _to_bytes(nums[0][0], nums[0][1], unit_is_kb=True)
                    avail = _to_bytes(nums[2][0], nums[2][1], unit_is_kb=True)
                    if total > 0:
                        used_pct = round((total - avail) * 100 / total)
                        dev.storage_summary = (f"{_human(avail)} 可用 / 共 {_human(total)}"
                                               f"（已用 {used_pct}%）")
                        break
                except ValueError:
                    continue

    @staticmethod
    def _parse_memory(dev: Device, out: str) -> None:
        m_total = re.search(r"MemTotal:\s*(\d+) kB", out)
        m_avail = re.search(r"MemAvailable:\s*(\d+) kB", out)
        if m_total and m_avail:
            dev.memory_summary = (f"{_human(int(m_avail.group(1)) * 1024)} 可用 / 共 "
                                  f"{_human(int(m_total.group(1)) * 1024)}")

    # ---------------- 错误解释 ---------------- #

    @staticmethod
    def explain(text: str) -> str:
        """把 adb 原始错误翻译成中文排查建议。"""
        t = (text or "").lower()
        if "device offline" in t or "device '(null)' not found" in t:
            return ("设备离线：USB 数据线可能被拔出、接触不良，或手机端 USB 调试被关闭。"
                    "建议更换数据线/USB 口后重新插拔，并在开发者选项中确认 USB 调试处于开启状态。")
        if "unauthorized" in t:
            return ("设备未授权：请在手机屏幕上确认『允许 USB 调试？』弹窗，勾选『一律允许使用这台计算机』后确定。"
                    "若手机屏幕损坏导致无法点按该弹窗，可先用 OTG 转接鼠标点按，或改用已授权过的电脑。")
        if "no devices/emulators found" in t or "device not found" in t or "no device" in t:
            return ("未检测到任何设备：请确认① 数据线已连接且能充电 ② 开发者选项中的 USB 调试已开启 "
                    "③ USB 模式切换为『传输文件/Android Auto』 ④ 已安装并授权手机对应的 USB 驱动。")
        if "more than one device" in t:
            return "连接了多台设备，请在界面顶部下拉框中选择目标设备。"
        if "permission denied" in t:
            return "权限不足：该目录或操作需要 root 权限，普通（未 root）设备无法访问。可改用『应用数据备份』等免 root 方案。"
        if "not found" in t and "adb" in t:
            return "找不到 adb，请在设置中指定正确的 adb 路径。"
        if "closed" in t or "broken pipe" in t or "connection reset" in t:
            return "连接被中断：设备可能已断开或重启。请重新连接后重试。"
        if "timeout" in t:
            return "操作超时：设备响应缓慢，可降低投屏帧率或稍后重试。"
        if t.strip():
            return f"设备返回：{text.strip()[:200]}"
        return "未知错误，请查看日志页了解详情。"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def _to_bytes(value: str, suffix: str, unit_is_kb: bool = False) -> int:
    """把 df 输出中的数值转换为字节。unit_is_kb=True 时无后缀数值单位为 KB。"""
    num = float(value)
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    if suffix:
        return int(num * mult.get(suffix.upper(), 1))
    return int(num * 1024) if unit_is_kb else int(num)


def _human(size: int | float) -> str:
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def shell_quote(text: str) -> str:
    """把字符串包装为设备端 shell 的单引号安全形式。"""
    return "'" + text.replace("'", "'\\''") + "'"


def input_text_escape(text: str) -> str:
    """为 `input text` 转义：空格用 %s，其余按 shell 单引号规则处理。"""
    return shell_quote(text.replace(" ", "%s"))
