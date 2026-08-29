# -*- coding: utf-8 -*-
"""Root 高级层：su 检测、root shell、root 文件流式导出、应用全量备份、系统级操作。

设计要点
--------
1. 本层只依赖 core.adb 的底层执行原语（raw / shell / exec_out），不直接调用 adb。
2. 所有 root 命令统一走 RootManager.shell()，内部按候选顺序尝试
   （`su -c` → `su 0 -c` → 绝对路径 su），第一次成功即记住方式，避免重复探测。
3. 大目录导出使用 **tar 流式传输**：`adb exec-out su -c 'tar -czf - -C <父目录> <子目录>'`
   单条连接流式写入本地 .tar.gz，比"复制到 /sdcard 再 pull"更快、不占设备存储，
   也比逐文件 `su cat` 少几百次 adb 往返。
4. 短信/通话/联系人强制导出：root 直接流式取回 providers 的 SQLite 数据库目录，
   在电脑本地用 Python sqlite3 解析 —— 不再受 Android 4.4+ / 厂商 ContentProvider
   限制，即使屏幕完全不可用也能完整导出。
5. 合规边界：本层只做"以设备所有者的 root 权限访问数据"，不提供任何绕过
   锁屏密码 / 指纹 / 图案的功能；破坏性操作仍由界面层二次确认。
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass

from core.adb import IS_WINDOWS, AdbClient, AdbError, shell_quote

#: 短信 / 通话 / 联系人数据库的常见路径（按优先级，覆盖各 Android 版本与厂商）
SMS_DB_CANDIDATES = [
    "/data/user_de/0/com.android.providers.telephony/databases",
    "/data/data/com.android.providers.telephony/databases",
]
CONTACTS_DB_CANDIDATES = [
    "/data/user_de/0/com.android.providers.contacts/databases",
    "/data/data/com.android.providers.contacts/databases",
]
CALLLOG_DB_CANDIDATES = [
    "/data/user_de/0/com.android.providers.contacts/databases",
    "/data/user_de/0/com.android.providers.telephony/databases",
    "/data/data/com.android.providers.contacts/databases",
]

#: 常用聊天应用（一键导出数据目录）
CHAT_APPS: list[tuple[str, str]] = [
    ("微信 WeChat", "com.tencent.mm"),
    ("QQ", "com.tencent.mobileqq"),
    ("WhatsApp", "com.whatsapp"),
    ("Telegram", "org.telegram.messenger"),
    ("钉钉 DingTalk", "com.alibaba.android.rimet"),
    ("企业微信", "com.tencent.wework"),
    ("支付宝", "com.eg.android.AlipayGphone"),
]

#: 默认禁止动画的三项设置
ANIMATION_KEYS = (
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
)


@dataclass
class RootStatus:
    """设备 root 状态汇总（界面状态卡用）。"""

    available: bool = False
    method: str = "none"        # magisk / supersu / adb-root / none
    magisk_version: str = ""
    adb_root_ok: bool = False   # ro.debuggable=1，可 adb root
    selinux: str = "unknown"    # Enforcing / Permissive / unknown
    data_readable: bool = False  # /data/data 是否可读
    adbd_as_root: bool = False  # 当前 adbd 是否已以 root 运行
    detail: str = ""

    @property
    def method_text(self) -> str:
        return {
            "magisk": "Magisk",
            "supersu": "SuperSU",
            "adb-root": "ADB Root（userdebug）",
            "none": "未检测到 root",
        }.get(self.method, self.method)


class RootManager:
    """以 root 身份操作设备。未 root 时所有方法抛出带中文提示的 AdbError。"""

    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial
        self._su_cmd: str | None = None     # 探测成功的 su 前缀，如 "su -c"
        self._status: RootStatus | None = None
        self._status_checked_at: float = 0.0

    # ------------------------------------------------------------------ #
    # 基础执行
    # ------------------------------------------------------------------ #

    def _raw_shell(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        """不经过 su 的普通 shell（探测阶段使用）。"""
        return subprocess.run(
            [self.adb.adb_path, "-s", self.serial, "shell", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )

    def _probe_su(self) -> str | None:
        """探测可用的 su 调用前缀。返回 None 表示无 root。"""
        candidates = (
            "su -c",                      # Magisk / 常见 SuperSU
            "su 0 -c",                    # 部分 ROM 的 su 语法
            "/system/xbin/su -c",
            "/system/bin/su -c",
        )
        for prefix in candidates:
            try:
                res = self._raw_shell(f"{prefix} id", timeout=15)
            except Exception:
                continue
            text = (res.stdout or b"").decode("utf-8", errors="replace")
            if "uid=0" in text:
                return prefix
        return None

    def su_available(self, refresh: bool = False) -> bool:
        """检查 su 是否可用（结果缓存 30 秒）。"""
        now = time.time()
        if refresh or self._su_cmd is None or now - self._status_checked_at > 30:
            self._su_cmd = self._probe_su()
            self._status_checked_at = now
        return self._su_cmd is not None

    def shell(self, cmd: str, timeout: int = 60, check: bool = True) -> str:
        """以 root 执行 shell 命令，返回合并输出。check=True 时失败抛 AdbError。"""
        if not self.su_available():
            raise AdbError(
                "设备未获取 root 权限",
                "本功能需要手机已 root（Magisk / SuperSU 等）。请确认：① 已安装 Magisk 并授予 "
                "Shell 应用 root 权限（Magisk 设置里开启『允许 Shell 超级用户』）；"
                "② 或使用 userdebug 固件并执行『adb root』。",
            )
        res = self.adb.shell(self.serial, f"{self._su_cmd} {shell_quote(cmd)}",
                             timeout=timeout)
        out = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
        if check and not res.ok and "uid=0" not in out and "permission denied" in out.lower():
            raise AdbError("root 命令被拒绝", "请检查 Magisk 超级用户授权记录是否拒绝了 shell。", out)
        return out

    def read_binary(self, remote_path: str, timeout: int = 120) -> bytes:
        """以 root 读取整个文件（适合配置文件、数据库等中小文件）。"""
        if not self.su_available():
            raise AdbError("设备未获取 root 权限", "该功能需要 root。", "")
        res = self.adb.raw(
            ["-s", self.serial, "exec-out",
             f"{self._su_cmd} {shell_quote('cat ' + remote_path)}"],
            timeout=timeout, binary=True)
        if not res.data:
            raise AdbError(f"root 读取失败：{remote_path}",
                           "文件可能不存在，或路径需要加前缀 /data/user_de/0/。", res.stderr)
        return res.data

    def list_dir(self, path: str) -> list[str]:
        """以 root 列出目录（每行一个条目，含权限与名称）。"""
        out = self.shell(f"ls -la {shell_quote(path)} 2>&1", timeout=30)
        if "No such file" in out:
            raise AdbError(f"目录不存在：{path}", "请检查路径拼写。", out)
        return [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("total ")]

    def file_exists(self, path: str) -> bool:
        try:
            out = self.shell(f"ls -d {shell_quote(path)} 2>/dev/null; echo __E__$?", timeout=15)
            return "__E__0" in out
        except AdbError:
            return False

    def stat(self, path: str) -> str:
        return self.shell(f"ls -ld {shell_quote(path)} 2>&1", timeout=15).strip()

    # ------------------------------------------------------------------ #
    # root 状态检测
    # ------------------------------------------------------------------ #

    def get_status(self, refresh: bool = False) -> RootStatus:
        """汇总 root 状态。结果缓存 15 秒，避免频繁探测拖慢界面。"""
        now = time.time()
        if self._status is not None and not refresh and now - self._status_checked_at < 15:
            return self._status

        st = RootStatus()
        # 1) su 探测
        if self.su_available(refresh=refresh):
            st.available = True
            st.method = "supersu"
            out = self.shell("magisk -v 2>&1; echo __M__; magisk --path 2>&1", timeout=20)
            if "magisk" in out.lower() or "__M__" in out:
                m = re.search(r"^([\d.]+[^ ]*)(.*)$", out, re.M)
                st.method = "magisk"
                st.magisk_version = m.group(1).strip() if m and m.group(1).strip() else out.splitlines()[0].strip()
            # 区分 adbd 是否已 root（adb root 后 su 可省略）
            try:
                id_out = self.adb.shell(self.serial, "id", timeout=10).stdout
                st.adbd_as_root = "uid=0" in id_out
            except Exception:
                pass
        else:
            st.available = False

        # 2) ro.debuggable（可 adb root 的 userdebug 固件）
        try:
            dbg = self.adb.shell(self.serial, "getprop ro.debuggable", timeout=10).stdout.strip()
            st.adb_root_ok = dbg == "1"
        except Exception:
            pass

        # 3) SELinux 状态
        try:
            se = self.adb.shell(self.serial, "getenforce 2>/dev/null || echo unknown", timeout=10).stdout.strip()
            st.selinux = se or "unknown"
        except Exception:
            pass

        # 4) /data/data 可读性
        if st.available:
            out = self.shell("ls -d /data/data 2>&1; echo __E__$?", timeout=15)
            st.data_readable = "__E__0" in out
            if st.data_readable:
                st.detail += "可读 /data/data；"
            if st.selinux == "Enforcing":
                st.detail += "SELinux 强制模式（读取 /data/data 一般不受影响）；"
            elif st.selinux == "Permissive":
                st.detail += "SELinux 宽容模式；"
        else:
            st.data_readable = False

        if st.adbd_as_root:
            st.detail = "adbd 已以 root 运行（userdebug）。" + st.detail
        elif st.adb_root_ok and not st.available:
            st.detail = "检测到 userdebug 固件，可执行『adb root』重启 adbd 获得 root。"
        self._status = st
        self._status_checked_at = now
        return st

    def adb_root(self) -> str:
        """重启 adbd 为 root（仅 userdebug/eng 固件有效）。"""
        res = self.adb.raw(["-s", self.serial, "root"], timeout=60)
        text = (res.stdout or res.stderr or "").strip()
        if "restarting adbd as root" in text.lower() or res.ok:
            return "adbd 已重启为 root，请稍候数秒后点击『刷新设备』重新连接。"
        if "cannot run as root" in text.lower():
            raise AdbError("此固件不支持 adb root",
                           "生产固件（ro.debuggable=0）无法通过 adb root 提权。"
                           "请改用 Magisk/SuperSU 方式。", text)
        raise AdbError("adb root 失败", self.adb.explain(text), text)

    def open_magisk(self) -> str:
        out = self.shell(
            "am start -n com.topjohnwu.magisk/.ui.MainActivity 2>&1 || "
            "monkey -p com.topjohnwu.magisk -c android.intent.category.LAUNCHER 1 2>&1",
            timeout=30)
        return "已尝试打开 Magisk"

    # ------------------------------------------------------------------ #
    # 流式导出（tar）
    # ------------------------------------------------------------------ #

    def stream_tar(self, remote_dir: str, local_file: str, *,
                   on_progress=None, should_stop=None, compress: bool = True) -> dict:
        """以 root 把整个目录打包成 tar(.gz) 流式传输到本地。

        命令：`tar -czf - -C <父目录> <目录名>`；exec-out 保证二进制安全。
        返回 {bytes, ok, error}。
        """
        if not self.su_available():
            raise AdbError("设备未获取 root 权限", "该功能需要 root。", "")
        parent = os.path.dirname(remote_dir.rstrip("/")) or "/"
        name = os.path.basename(remote_dir.rstrip("/"))
        flags = "-czf" if compress else "-cf"
        # 内层命令整体再套一层 shell 引号：su -c '<tar ...>'
        inner = f"tar {flags} - -C {shell_quote(parent)} {shell_quote(name)} 2>/dev/null"
        cmd = [self.adb.adb_path, "-s", self.serial, "exec-out",
               f"{self._su_cmd} {shell_quote(inner)}"]
        os.makedirs(os.path.dirname(os.path.abspath(local_file)), exist_ok=True)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0)
        total = 0
        ok = True
        try:
            with open(local_file, "wb") as fp:
                assert proc.stdout is not None
                while True:
                    if should_stop and should_stop():
                        proc.terminate()
                        ok = False
                        break
                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        break
                    fp.write(chunk)
                    total += len(chunk)
                    if on_progress:
                        on_progress(total)
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if ok and (total == 0 or proc.returncode not in (0, None)):
            # tar 无输出通常意味着目录为空或不存在；空 tar 也有 10KB 头，0 字节视为失败
            if total == 0:
                ok = False
        return {"bytes": total, "ok": ok, "file": local_file}

    def pull_file_root(self, remote: str, local: str, timeout: int = 120) -> str:
        """以 root 拉取单个文件（流式，避免 /sdcard 中转）。"""
        data = self.read_binary(remote, timeout=timeout)
        os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
        with open(local, "wb") as fp:
            fp.write(data)
        return local

    def stream_file(self, remote: str, local: str, *,
                    on_progress=None, should_stop=None) -> dict:
        """以 root 流式下载任意大小的单个文件（chunk 写入，不占内存）。"""
        if not self.su_available():
            raise AdbError("设备未获取 root 权限", "该功能需要 root。", "")
        cmd = [self.adb.adb_path, "-s", self.serial, "exec-out",
               f"{self._su_cmd} {shell_quote('cat ' + remote)}"]
        os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0)
        total = 0
        ok = True
        try:
            with open(local, "wb") as fp:
                assert proc.stdout is not None
                while True:
                    if should_stop and should_stop():
                        proc.terminate()
                        ok = False
                        break
                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        break
                    fp.write(chunk)
                    total += len(chunk)
                    if on_progress:
                        on_progress(total)
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if ok and total == 0:
            ok = False
        return {"bytes": total, "ok": ok, "file": local}

    # ------------------------------------------------------------------ #
    # 应用全量备份（APK + 内部数据 + 设备保护数据 + 外置数据）
    # ------------------------------------------------------------------ #

    def app_apk_paths(self, package: str) -> list[str]:
        out = self.adb.shell(self.serial, f"pm path {shell_quote(package)}", timeout=30).stdout
        paths = [ln.split(":", 1)[1].strip()
                 for ln in out.splitlines() if ln.startswith("package:")]
        if not paths:
            raise AdbError(f"未找到 {package} 的 APK", "包名可能不存在或应用已卸载。", out)
        return paths

    def _stream_apk(self, remote_apk: str, local_path: str) -> None:
        if not self.su_available():
            raise AdbError("设备未获取 root 权限", "该功能需要 root。", "")
        res = self.adb.raw(
            ["-s", self.serial, "exec-out",
             f"{self._su_cmd} {shell_quote('cat ' + remote_apk)}"],
            timeout=600, binary=True)
        if not res.data:
            raise AdbError(f"APK 读取失败：{remote_apk}", res.stderr, "")
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as fp:
            fp.write(res.data)

    def backup_app_full(self, package: str, dest_dir: str, *,
                        include_apk: bool = True, include_data: bool = True,
                        include_external: bool = True,
                        on_progress=None, should_stop=None) -> dict:
        """应用全量备份到本地。

        结构：dest/<包名>/
              ├── apk/…（base.apk 与 split APK，root 直读 /data/app）
              ├── data-data.tar.gz（/data/data/<pkg> 全部内部数据、数据库）
              ├── data-userde.tar.gz（/data/user_de/0/<pkg> 设备保护数据）
              └── external.tar.gz（/sdcard/Android/data|media|obb/<pkg>）
        """
        if not self.su_available():
            raise AdbError("设备未获取 root 权限",
                           "完整备份（含应用内部数据）需要 root。未 root 设备请改用『导出应用外置数据』。", "")
        target = os.path.join(dest_dir, package)
        os.makedirs(target, exist_ok=True)
        result: dict = {"apks": [], "archives": [], "errors": []}

        def step(text: str):
            if on_progress:
                on_progress(text)

        # 1) APK
        if include_apk:
            try:
                paths = self.app_apk_paths(package)
                apk_dir = os.path.join(target, "apk")
                os.makedirs(apk_dir, exist_ok=True)
                for i, p in enumerate(paths):
                    if should_stop and should_stop():
                        break
                    base = os.path.basename(p)
                    step(f"备份 APK {i + 1}/{len(paths)}：{base}")
                    local = os.path.join(apk_dir, base)
                    self._stream_apk(p, local)
                    result["apks"].append(local)
            except AdbError as exc:
                result["errors"].append(f"APK：{exc.message}")

        # 2) /data/data/<pkg>
        if include_data:
            for remote, tag in (
                (f"/data/data/{package}", "data-data"),
                (f"/data/user_de/0/{package}", "data-userde"),
            ):
                if should_stop and should_stop():
                    break
                if not self.file_exists(remote):
                    continue
                step(f"打包 {remote} …")
                local = os.path.join(target, f"{tag}.tar.gz")
                st = self.stream_tar(remote, local, on_progress=None, should_stop=should_stop)
                if st["ok"] and st["bytes"] > 0:
                    result["archives"].append(local)
                else:
                    result["errors"].append(f"{tag}：目录为空或打包失败")

        # 3) 外置数据（root 可绕过 Android 11+ 的 Android/data 限制）
        if include_external:
            ext = os.path.join("/sdcard/Android", "data", package)
            media = os.path.join("/sdcard/Android", "media", package)
            obb = os.path.join("/sdcard/Android", "obb", package)
            found = [p for p in (ext, media, obb) if self.file_exists(p)]
            if found:
                step("打包外置数据 …")
                local = os.path.join(target, "external.tar.gz")
                # 需要把三个目录打包为一个：先在设备端临时汇总再流式取回
                tmpdir = f"/data/local/tmp/rescue_{package}"
                cmds = [
                    f"rm -rf {shell_quote(tmpdir)}",
                    f"mkdir -p {shell_quote(tmpdir)}",
                ]
                for p in found:
                    cmds.append(f"cp -r {shell_quote(p)} {shell_quote(tmpdir)}/ 2>/dev/null")
                try:
                    self.shell("; ".join(cmds), timeout=60)
                    st = self.stream_tar(tmpdir, local, should_stop=should_stop)
                    if st["ok"] and st["bytes"] > 0:
                        result["archives"].append(local)
                    self.shell(f"rm -rf {shell_quote(tmpdir)}", timeout=30)
                except AdbError as exc:
                    result["errors"].append(f"外置数据：{exc.message}")

        if not result["apks"] and not result["archives"]:
            raise AdbError("备份未生成任何文件",
                           "请确认包名正确，且应用确实存在数据。", "; ".join(result["errors"]))
        return result

    def export_chat_data(self, package: str, dest_dir: str, *,
                         label: str = "", on_progress=None, should_stop=None) -> str:
        """一键导出聊天应用数据目录（root）。"""
        target = os.path.join(dest_dir, package)
        os.makedirs(target, exist_ok=True)
        saved: list[str] = []
        for remote, tag in (
            (f"/data/data/{package}", "internal"),
            (f"/data/user_de/0/{package}", "userde"),
        ):
            if should_stop and should_stop():
                break
            if not self.file_exists(remote):
                continue
            local = os.path.join(target, f"{tag}.tar.gz")
            if on_progress:
                on_progress(f"打包 {remote} …")
            st = self.stream_tar(remote, local, should_stop=should_stop)
            if st["ok"] and st["bytes"] > 0:
                saved.append(local)
        if not saved:
            raise AdbError("未找到该应用的数据目录",
                           "包名可能不存在，或应用从未运行过。", "")
        return target

    # ------------------------------------------------------------------ #
    # 短信 / 通话 / 联系人强制导出（root 直读数据库）
    # ------------------------------------------------------------------ #

    def _fetch_db_dir(self, candidates: list[str], dest_dir: str, tag: str) -> str:
        """把 providers 数据库目录流式取回本地并解压。返回解压目录。"""
        remote = None
        for cand in candidates:
            base = cand.rstrip("/") + "/"
            if self.file_exists(base + "mmssms.db") or \
               self.file_exists(base + "contacts2.db") or \
               self.file_exists(base + "calllog.db"):
                remote = cand
                break
        if remote is None:
            raise AdbError("未找到对应的系统数据库目录",
                           "该厂商可能修改了 providers 路径；可尝试在 Root 文件浏览器中手动定位。", "")
        local_tar = os.path.join(dest_dir, f".{tag}.tar.gz")
        st = self.stream_tar(remote, local_tar)
        if not st["ok"] or st["bytes"] == 0:
            raise AdbError("数据库目录打包失败", self.adb.explain(""), st.get("error", ""))
        extract_dir = os.path.join(dest_dir, f".{tag}_db")
        os.makedirs(extract_dir, exist_ok=True)
        import tarfile
        try:
            with tarfile.open(local_tar, "r:gz") as tf:
                tf.extractall(extract_dir)
        finally:
            try:
                os.remove(local_tar)
            except OSError:
                pass
        return extract_dir

    @staticmethod
    def _open_db(extract_dir: str, *names: str) -> str | None:
        for root, _dirs, files in os.walk(extract_dir):
            for name in names:
                if name in files:
                    return os.path.join(root, name)
        return None

    def export_sms_root(self, dest_dir: str) -> str:
        """直接读取短信数据库导出 CSV（root，无需系统授权）。"""
        os.makedirs(dest_dir, exist_ok=True)
        extract = self._fetch_db_dir(SMS_DB_CANDIDATES, dest_dir, "sms")
        db = self._open_db(extract, "mmssms.db")
        if not db:
            raise AdbError("短信数据库未找到", "请确认设备短信应用为系统默认。", "")
        path = os.path.join(dest_dir, f"sms_root_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        rows = []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute("SELECT address, date, body, type, read FROM sms ORDER BY date")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            con.close()
        except sqlite3.Error as exc:
            raise AdbError("短信数据库解析失败", f"{exc}", db)
        import csv as _csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            writer = _csv.writer(fp)
            writer.writerow(["时间", "类型", "对方号码", "内容", "已读"])
            for row in rows:
                try:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row[cols.index("date")]) / 1000))
                except (ValueError, TypeError, OSError):
                    ts = str(row[cols.index("date")] if cols.index("date") < len(row) else "")
                writer.writerow([
                    ts,
                    {"1": "接收", "2": "已发送", "3": "草稿", "4": "发件箱"}.get(str(row[cols.index("type")]), "其他"),
                    row[cols.index("address")] or "",
                    str(row[cols.index("body")] or "").replace("\r", " ").replace("\n", " "),
                    "是" if str(row[cols.index("read")]) == "1" else "否",
                ])
        return path

    def export_contacts_root(self, dest_dir: str) -> str:
        """直接读取联系人数据库导出 CSV（root）。"""
        os.makedirs(dest_dir, exist_ok=True)
        extract = self._fetch_db_dir(CONTACTS_DB_CANDIDATES, dest_dir, "contacts")
        db = self._open_db(extract, "contacts2.db")
        if not db:
            raise AdbError("联系人数据库未找到", "请确认设备联系人应用为系统默认。", "")
        path = os.path.join(dest_dir, f"contacts_root_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        rows = []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = con.cursor()
            cur.execute(
                "SELECT data1, display_name, data2 FROM data "
                "WHERE mimetype_id IN (SELECT _id FROM mimetypes WHERE mimetype='vnd.android.cursor.item/phone_v2')"
            )
            rows = cur.fetchall()
            con.close()
        except sqlite3.Error as exc:
            raise AdbError("联系人数据库解析失败", f"{exc}", db)
        import csv as _csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            writer = _csv.writer(fp)
            writer.writerow(["姓名", "号码", "号码类型"])
            for number, name, ptype in rows:
                writer.writerow([name or "", number or "", ptype or ""])
        return path

    def export_calllog_root(self, dest_dir: str) -> str:
        """直接读取通话记录数据库导出 CSV（root）。"""
        os.makedirs(dest_dir, exist_ok=True)
        extract = self._fetch_db_dir(CALLLOG_DB_CANDIDATES, dest_dir, "calllog")
        db = self._open_db(extract, "calllog.db") or self._open_db(extract, "contacts2.db")
        if not db:
            raise AdbError("通话记录数据库未找到", "该厂商可能未使用系统默认路径。", "")
        path = os.path.join(dest_dir, f"calllog_root_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        rows = []
        table = "calls"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cur = con.cursor()
            try:
                cur.execute("SELECT number, date, duration, type, name FROM calls ORDER BY date")
            except sqlite3.Error:
                cur.execute("SELECT number, date, duration, type, cached_name FROM calls ORDER BY date")
            rows = cur.fetchall()
            con.close()
        except sqlite3.Error as exc:
            raise AdbError("通话记录数据库解析失败", f"{exc}", db)
        import csv as _csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            writer = _csv.writer(fp)
            writer.writerow(["时间", "类型", "号码", "联系人", "通话时长(秒)"])
            for row in rows:
                try:
                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(row[1]) / 1000))
                except (ValueError, TypeError, OSError):
                    ts = str(row[1])
                writer.writerow([
                    ts,
                    {"1": "来电", "2": "去电", "3": "未接", "4": "语音信箱"}.get(str(row[3]), "其他"),
                    row[0] or "", row[4] or "", row[2] or "",
                ])
        return path

    # ------------------------------------------------------------------ #
    # 系统级操作
    # ------------------------------------------------------------------ #

    def set_animations(self, scale: int | float = 0) -> str:
        """关闭/设置系统动画（scale=0 关闭，显著提升操控流畅度）。"""
        for key in ANIMATION_KEYS:
            self.shell(f"settings put global {key} {scale}", timeout=15)
        return "动画已关闭" if scale == 0 else f"动画缩放已设为 {scale}"

    def force_gpu(self, enable: bool = True) -> str:
        self.shell(f"settings put global force_gpu_rendering {'1' if enable else '0'}", timeout=15)
        return "已开启强制 GPU 渲染" if enable else "已关闭强制 GPU 渲染"

    def set_selinux(self, mode: str) -> str:
        """mode: Enforcing / Permissive。重启后恢复，属临时调整。"""
        out = self.shell(f"setenforce {mode} 2>&1; getenforce", timeout=15)
        return out.strip()

    def keep_screen_on_root(self) -> str:
        self.shell("svc power stayon true", timeout=15)
        return "已设置屏幕常亮（root）"

    def cancel_keep_screen_on(self) -> str:
        self.shell("svc power stayon false", timeout=15)
        return "已取消屏幕常亮"

    def grant_permission(self, package: str, permission: str) -> str:
        out = self.shell(f"pm grant {shell_quote(package)} {shell_quote(permission)} 2>&1",
                         timeout=30)
        if "error" in out.lower() and "not granted" not in out.lower():
            raise AdbError("授权失败", self.adb.explain(out), out)
        return "已授予权限"

    def uninstall_for_user(self, package: str) -> str:
        """卸载系统应用（仅对当前用户，可随时用 pm install-existing 恢复）。"""
        out = self.shell(f"pm uninstall -k --user 0 {shell_quote(package)} 2>&1", timeout=60)
        if "Success" in out or "success" in out:
            return "已卸载（仅当前用户，可通过『恢复系统应用』还原）"
        raise AdbError("卸载失败", self.adb.explain(out) or out, out)

    def restore_system_app(self, package: str) -> str:
        out = self.shell(f"pm install-existing --user 0 {shell_quote(package)} 2>&1", timeout=60)
        if "Success" in out or "success" in out:
            return "已恢复系统应用"
        raise AdbError("恢复失败", self.adb.explain(out) or out, out)

    def freeze(self, package: str, freeze: bool = True) -> str:
        action = "disable-user" if freeze else "enable"
        out = self.shell(f"pm {action} --user 0 {shell_quote(package)} 2>&1", timeout=60)
        if "Error" in out or "error" in out:
            raise AdbError("操作失败", self.adb.explain(out), out)
        return "已冻结" if freeze else "已解冻"

    def list_system_packages(self) -> list[str]:
        out = self.adb.shell(self.serial, "pm list packages -s", timeout=60).stdout
        return sorted(ln.replace("package:", "").strip()
                      for ln in out.splitlines() if ln.startswith("package:"))

    def process_list_root(self) -> list[tuple[str, str, str]]:
        """返回 [(pid, 用户, 进程名)]。"""
        out = self.shell("ps -A -o PID,USER,NAME 2>/dev/null || ps -A", timeout=30)
        rows = []
        for ln in out.splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 3:
                rows.append((parts[0], parts[1], " ".join(parts[2:])))
        return rows

    def kill_process(self, pid: str) -> str:
        self.shell(f"kill -9 {pid} 2>&1", timeout=15)
        return f"已发送 kill -9 到 PID {pid}"

    def system_info_root(self) -> dict[str, str]:
        """root 专属系统信息。"""
        info: dict[str, str] = {}
        try:
            info["SELinux"] = self.shell("getenforce", timeout=10).strip()
        except AdbError:
            info["SELinux"] = "未知"
        try:
            out = self.shell("cat /proc/version | head -1", timeout=10)
            m = re.search(r"Linux version (\S+)", out)
            info["内核"] = m.group(1) if m else out.strip()[:80]
        except AdbError:
            pass
        try:
            info["序列号"] = self.adb.shell(self.serial, "getprop ro.serialno", timeout=10).stdout.strip()
        except AdbError:
            pass
        try:
            info["/data 剩余"] = self.shell("df -h /data | tail -1", timeout=10).split()[-4] + " 可用"
        except AdbError:
            pass
        return info
