# -*- coding: utf-8 -*-
"""模拟 adb：让核心层可以在没有真机的环境下完成端到端冒烟测试。

v2.0 扩展：内置一套**纯内存**的"模拟 root 文件系统"，支持
  - `su -c ...` 家族命令（id / ls / file_exists / shell）
  - `exec-out su -c 'tar ...'` 流式打包（真实 tar.gz，内存构建）
  - `exec-out su -c 'cat ...'` 二进制读取（含真实 SQLite 数据库字节）

注意：本脚本可能在受控（沙箱）环境下以子进程方式运行，被禁止创建任何
目录/文件，因此全部数据存放在内存中（sqlite3 serialize + tarfile BytesIO），
不触碰文件系统。
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
import tarfile

try:
    from PIL import Image
except ImportError:
    Image = None

BATCH = (
    "getprop;echo __S1__;wm size;wm density;echo __S2__;dumpsys battery;"
    "echo __S3__;df /sdcard /storage/emulated 2>/dev/null|tail -n 2;"
    "echo __S4__;cat /proc/meminfo;echo __S5__;dumpsys power;echo __S6__;id"
)

BATCH_OUT = (
    "[ro.product.brand]: [Google]\n"
    "[ro.product.model]: [Pixel 7]\n"
    "[ro.product.vendor.model]: [Pixel 7]\n"
    "[ro.product.device]: [lynx]\n"
    "[ro.build.version.release]: [14]\n"
    "[ro.build.version.sdk]: [34]\n"
    "__S1__\n"
    "Physical size: 1080x2400\n"
    "Physical density: 420\n"
    "__S2__\n"
    "  AC powered: false\n"
    "  USB powered: true\n"
    "  status: 2\n"
    "  health: 2\n"
    "  level: 62\n"
    "  scale: 100\n"
    "  temperature: 285\n"
    "  technology: Li-ion\n"
    "__S3__\n"
    "Filesystem 1K-blocks Used Available Use% Mounted on\n"
    "/data/media 117440512 58720256 58720256 50% /storage/emulated\n"
    "__S4__\n"
    "MemTotal:        7800000 kB\n"
    "MemAvailable:    3200000 kB\n"
    "__S5__\n"
    "  mWakefulness=Awake\n"
    "__S6__\n"
    "uid=2000 gid=2000 groups=2000,1007 context=u:r:shell:s0\n"
)

ROOT_BATCH = (
    "command -v su;echo __R0__;su -c id 2>&1;echo __R1__;magisk -v 2>&1;"
    "echo __R2__;getenforce 2>&1;echo __R3__;getprop ro.debuggable;echo __R4__;id"
)

ROOT_BATCH_OUT = (
    "/system/bin/su\n"
    "__R0__\n"
    "uid=0(root) gid=0(root) groups=0,1000 context=u:r:magisk:s0\n"
    "__R1__\n"
    "28100\n"
    "__R2__\n"
    "Enforcing\n"
    "__R3__\n"
    "0\n"
    "__R4__\n"
    "uid=2000 gid=2000 groups=2000,1007 context=u:r:shell:s0\n"
)

SHELL = {
    BATCH: BATCH_OUT,
    ROOT_BATCH: ROOT_BATCH_OUT,
    "getprop ro.product.brand": "Google\n",
    "getprop ro.product.marketname": "\n",
    "getprop ro.product.model": "Pixel 7\n",
    "getprop ro.product.vendor.model": "Pixel 7\n",
    "getprop ro.product.device": "lynx\n",
    "getprop ro.build.version.release": "14\n",
    "getprop ro.build.version.sdk": "34\n",
    "id": "uid=2000 gid=2000 groups=2000,1007 context=u:r:shell:s0\n",
    "su -c id": "uid=0(root) gid=0(root) groups=0,1000 context=u:r:magisk:s0\n",
    "magisk -v 2>&1; echo __M__; magisk --path 2>&1": "28100\n__M__\n/sbin/.magisk\n",
    "getenforce 2>/dev/null || echo unknown": "Enforcing\n",
    "getprop ro.debuggable": "0\n",
    "wm size": "Physical size: 1080x2400\n",
    "wm density": "Physical density: 420\n",
    "dumpsys power": "  mWakefulness=Awake\n  mScreenOnEarly=true\n",
    "dumpsys battery": (
        "Current Battery Service state:\n"
        "  AC powered: false\n"
        "  USB powered: true\n"
        "  Wireless powered: false\n"
        "  status: 2\n"
        "  health: 2\n"
        "  present: true\n"
        "  level: 62\n"
        "  scale: 100\n"
        "  voltage: 3900\n"
        "  temperature: 285\n"
        "  technology: Li-ion\n"),
    "cat /proc/meminfo": (
        "MemTotal:        7800000 kB\n"
        "MemFree:          320000 kB\n"
        "MemAvailable:    3200000 kB\n"),
    "cat /proc/uptime": "123456.78 987654.32\n",
    "df /sdcard /storage/emulated 2>/dev/null | tail -n 2": (
        "Filesystem 1K-blocks Used Available Use% Mounted on\n"
        "/data/media 117440512 58720256 58720256 50% /storage/emulated\n"),
    "df -h /sdcard /data /storage/emulated 2>/dev/null": (
        "Filesystem      Size Used Avail Use% Mounted on\n"
        "/data/media      112G  56G   56G  50% /storage/emulated\n"),
    "ls -la '/sdcard' 2>&1": (
        "total 64\n"
        "drwxrwx--x 12 root sdcard_rw 4096 2026-08-28 10:00 .\n"
        "drwxrwx--x 12 root sdcard_rw 4096 2026-08-28 10:00 ..\n"
        "drwxrwx--x  2 root sdcard_rw 4096 2026-08-20 09:30 DCIM\n"
        "drwxrwx--x  2 root sdcard_rw 4096 2026-08-21 11:02 Download\n"
        "-rw-rw----  1 root sdcard_rw 20480 2026-08-25 18:44 notes.txt\n"
        "-rw-rw----  1 root sdcard_rw 1048576 2026-08-26 08:10 我的 照片.jpg\n"),
    "ls -la '/sdcard/DCIM' 2>&1": (
        "total 32\n"
        "drwxrwx--x 2 root sdcard_rw 4096 2026-08-20 09:30 .\n"
        "drwxrwx--x 2 root sdcard_rw 4096 2026-08-20 09:30 ..\n"
        "-rw-rw---- 1 root sdcard_rw 3145728 2026-08-20 09:31 IMG_0001.jpg\n"
        "-rw-rw---- 1 root sdcard_rw 4194304 2026-08-20 09:32 VID_0002.mp4\n"),
    # 私有目录：普通 shell 无权限 → 触发 root 回退
    "ls -la '/data' 2>&1": "ls: /data: Permission denied\n",
    "ls -la '/data/data' 2>&1": "ls: /data/data: Permission denied\n",
    "pm list packages -f -3": "package:/data/app/~~a1==/com.demo.app-1/base.apk=com.demo.app\n",
    "pm list packages -f -s": "package:/system/app/Browser/Browser.apk=com.android.browser\n",
    "pm list packages -s": "package:com.android.browser\npackage:com.android.settings\npackage:com.topjohnwu.magisk\n",
    "dumpsys package 'com.demo.app'": "   versionName=2.1.0\n   application-label:'Demo'\n",
    "pm path 'com.demo.app'": "package:/data/app/~~a1==/com.demo.app-1/base.apk\n",
    "content query --uri content://com.android.contacts/data/phones "
    "--projection display_name:data1:data2 2>&1": (
        "Row: 0 display_name=张三, data1=13800000000, data2=2\n"
        "Row: 1 display_name=Bob, data1=13900000000, data2=1\n"),
    "content query --uri content://sms --projection address:date:body:type:read 2>&1": (
        "Row: 0 address=10086, date=1755000000000, body=您的余额为50元, type=1, read=1\n"
        "Row: 1 address=13800000000, date=1755003600000, body=晚上一起吃饭, type=2, read=0\n"),
    "content query --uri content://call_log/calls --projection number:date:duration:type:name 2>&1": (
        "Row: 0 number=10010, date=1755000000000, duration=65, type=1, name=null\n"),
}

# --------------------------------------------------------------------------- #
# 内存模拟 root 文件系统
# --------------------------------------------------------------------------- #


class FakeRootFS:
    """纯内存的模拟 root 文件系统：files: 路径->bytes；dirs: 路径集合。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self._seed()

    # ---------------- 种子数据 ---------------- #

    @staticmethod
    def _db_sms() -> bytes:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE sms (_id INTEGER PRIMARY KEY, address TEXT, date INTEGER,"
                    " body TEXT, type INTEGER, read INTEGER)")
        con.execute("INSERT INTO sms (address,date,body,type,read) VALUES"
                    " ('10086',1755000000000,'root测试短信',1,1)")
        con.commit()
        data = con.serialize()
        con.close()
        return data

    @staticmethod
    def _db_contacts() -> bytes:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE mimetypes (_id INTEGER PRIMARY KEY, mimetype TEXT)")
        con.execute("CREATE TABLE data (_id INTEGER PRIMARY KEY, data1 TEXT, display_name TEXT,"
                    " data2 TEXT, mimetype_id INTEGER)")
        con.execute("INSERT INTO mimetypes VALUES (1,'vnd.android.cursor.item/phone_v2')")
        con.execute("INSERT INTO data (data1,display_name,data2,mimetype_id)"
                    " VALUES ('13800000000','张三','2',1)")
        con.commit()
        data = con.serialize()
        con.close()
        return data

    @staticmethod
    def _db_calllog() -> bytes:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE calls (number TEXT, date INTEGER, duration INTEGER,"
                    " type INTEGER, name TEXT)")
        con.execute("INSERT INTO calls VALUES ('10010',1755000000000,65,1,'客服')")
        con.commit()
        data = con.serialize()
        con.close()
        return data

    def _seed(self) -> None:
        sms_dir = "/data/user_de/0/com.android.providers.telephony/databases"
        con_dir = "/data/user_de/0/com.android.providers.contacts/databases"
        app_dir = "/data/data/com.demo.app"
        apk_dir = "/data/app/~~a1==/com.demo.app-1"

        self.files = {
            f"{apk_dir}/base.apk": b"PK\x03\x04FAKE_APK_BYTES",
            f"{app_dir}/files/secret.txt": "demo secret data\n".encode("utf-8"),
            f"{sms_dir}/mmssms.db": self._db_sms(),
            f"{con_dir}/contacts2.db": self._db_contacts(),
            f"{con_dir}/calllog.db": self._db_calllog(),
        }
        self.dirs = set()
        for path in list(self.files) + [sms_dir, con_dir, app_dir, apk_dir, "/data",
                                        "/data/app", "/data/app/~~a1==", "/data/data",
                                        "/data/user_de/0",
                                        "/data/user_de/0/com.android.providers.telephony",
                                        "/data/user_de/0/com.android.providers.contacts",
                                        f"{app_dir}/files"]:
            # 补全所有祖先目录（不含文件自身）
            parts = path.split("/")
            cur = ""
            for part in parts[1:]:
                cur += "/" + part
                self.dirs.add(cur)
        # 文件路径不能同时是目录
        self.dirs.difference_update(self.files.keys())

    # ---------------- 查询 ---------------- #

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def is_dir(self, path: str) -> bool:
        return path in self.dirs

    def children(self, path: str) -> list[str]:
        """返回目录下直属子项（含路径）。"""
        prefix = path.rstrip("/") + "/"
        out: set[str] = set()
        for p in list(self.files) + list(self.dirs):
            if p.startswith(prefix):
                rest = p[len(prefix):]
                if "/" in rest:
                    out.add(prefix + rest.split("/", 1)[0])
                else:
                    out.add(p)
        return sorted(out)

    def ls(self, path: str) -> str:
        if not self.is_dir(path):
            return f"ls: cannot access '{path}': No such file or directory\n"
        lines = ["total 8"]
        for child in self.children(path):
            name = child.rsplit("/", 1)[-1]
            if self.is_dir(child):
                lines.append(f"drwxr-xr-x 1 root root 4096 2026-08-29 12:00 {name}")
            else:
                size = len(self.files.get(child, b""))
                lines.append(f"-rw-r--r-- 1 root root {size} 2026-08-29 12:00 {name}")
        return "\n".join(lines) + "\n"

    def stat(self, path: str) -> str:
        if self.is_dir(path):
            return f"drwxr-xr-x 1 root root 4096 2026-08-29 12:00 {path}\n"
        if path in self.files:
            return f"-rw-r--r-- 1 root root {len(self.files[path])} 2026-08-29 12:00 {path}\n"
        return ""

    def tar_gz(self, parent: str, name: str) -> bytes:
        """把 parent/name 目录打包为内存 tar.gz。"""
        root = parent.rstrip("/") + "/" + name
        if not self.is_dir(root):
            return b""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            def add_dir(rel_dir: str, arc_dir: str) -> None:
                ti = tarfile.TarInfo(arc_dir + "/")
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tf.addfile(ti)
                for child in self.children(rel_dir):
                    cname = child.rsplit("/", 1)[-1]
                    carc = arc_dir + "/" + cname
                    if self.is_dir(child):
                        add_dir(child, carc)
                    else:
                        data = self.files.get(child, b"")
                        ti = tarfile.TarInfo(carc)
                        ti.size = len(data)
                        ti.mode = 0o644
                        tf.addfile(ti, io.BytesIO(data))
            add_dir(root, name)
        return buf.getvalue()


FAKE_FS: FakeRootFS | None = None


def _rootfs() -> FakeRootFS:
    global FAKE_FS
    if FAKE_FS is None:
        FAKE_FS = FakeRootFS()
    return FAKE_FS


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("'\\''", "'")
    return text


def _decode_su_payload(payload: str) -> str | None:
    """把 `su -c '<内层命令>'` 解码为内层命令（处理嵌套转义引号）。"""
    if payload.startswith("su -c '") and payload.endswith("'"):
        return payload[7:-1].replace("'\\''", "'")
    return None


def _handle_root_shell(inner: str) -> str | None:
    """处理 su -c '...' 内部命令。返回 None 表示无特殊处理。"""
    fs = _rootfs()
    inner = inner.strip()
    if inner == "id":
        return "uid=0(root) gid=0(root) groups=0,1000 context=u:r:magisk:s0\n"
    # 目录类命令优先于静态 SHELL 表（避免命中"Permission denied"等占位键）
    if inner.startswith("ls -la "):
        m = re.match(r"ls -la '(.*)' 2>&1", inner)
        if m:
            return fs.ls(_unquote(m.group(1)))
    if inner.startswith("ls -d ") and "__E__$?" in inner:
        m = re.match(r"ls -d '?([^';]+)'? 2>[^;]+; echo __E__\$?", inner)
        if m:
            path = _unquote(m.group(1))
            return "__E__0\n" if fs.exists(path) else "__E__1\n"
    if inner.startswith("ls -ld "):
        m = re.match(r"ls -ld '(.*)' 2>&1", inner)
        if m:
            return fs.stat(_unquote(m.group(1)))
    if inner in SHELL:
        return SHELL[inner]
    if inner.startswith("pm path "):
        m = re.match(r"pm path '(.*)'", inner)
        if m:
            return f"package:/data/app/~~a1==/{m.group(1)}-1/base.apk\n"
    if inner.startswith("ps -A "):
        return ("PID USER NAME\n"
                "1234 root su\n"
                "2345 shell sh\n")
    if inner.startswith("getenforce"):
        return "Enforcing\n"
    if inner.startswith("magisk -v"):
        return "28100\n"
    if inner.startswith("pm uninstall "):
        return "Success\n"
    if inner.startswith("pm install-existing "):
        return "Success\n"
    if inner.startswith("settings put ") or inner.startswith("svc power ") or \
       inner.startswith("setenforce ") or inner.startswith("pm grant ") or \
       inner.startswith("pm disable-user ") or inner.startswith("pm enable ") or \
       inner.startswith("kill ") or inner.startswith("rm -rf ") or inner.startswith("mkdir -p ") or \
       inner.startswith("cp -r ") or inner.startswith("am start ") or inner.startswith("monkey "):
        return "OK\n"
    return None


def png_bytes() -> bytes:
    if Image is None:
        return b"PNGDATA"
    img = Image.new("RGB", (1080, 2400), (30, 60, 120))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    args = [a for a in sys.argv[1:] if a != ""]
    if os.environ.get("FAKE_ADB_DEBUG"):
        print(f"[DEBUG argv={sys.argv[1:]!r}]", file=sys.stderr)
    if not args:
        print("Android Debug Bridge version 1.0.41")
        return 0
    head = args[0]

    if head == "version":
        print("Android Debug Bridge version 1.0.41")
        print("Version 34.0.5-10900879")
        return 0
    if head in ("start-server", "kill-server"):
        return 0
    if head == "root":
        print("restarting adbd as root...")
        return 0
    if head == "devices":
        print("List of devices attached")
        print("ABC123\tdevice product:lynx model:Pixel_7 device:lynx transport_id:1")
        print("XYZ789\tunauthorized usb:1-1 transport_id:2")
        return 0

    if head == "-s" and len(args) >= 3:
        sub = args[2]
        payload = args[3] if len(args) > 3 else ""
        if sub == "shell":
            # su -c '...' 家族
            m = re.match(r"^(su(?: 0)? -c) '(.*)'$", payload)
            if m:
                inner = m.group(2).replace("'\\''", "'")
                handled = _handle_root_shell(inner)
                if handled is not None:
                    sys.stdout.write(handled)
                    return 0
                sys.stdout.write(f"ROOT: {inner}\n")
                return 0
            if payload == "su -c id":
                sys.stdout.write("uid=0(root) gid=0(root) groups=0,1000 context=u:r:magisk:s0\n")
                return 0
            sys.stdout.write(SHELL.get(payload, f"SIMULATED: {payload}\n"))
            return 0
        if sub == "exec-out":
            if payload == "screencap -p":
                sys.stdout.buffer.write(png_bytes())
                return 0
            inner = _decode_su_payload(payload)
            if inner is not None:
                # exec-out su -c 'tar -czf - -C <parent> <name> 2>/dev/null'
                m = re.match(r"tar -czf - -C (.+?) (.+?) 2>/dev/null$", inner)
                if m:
                    parent = _unquote(m.group(1))
                    name = _unquote(m.group(2))
                    sys.stdout.buffer.write(_rootfs().tar_gz(parent, name))
                    return 0
                # exec-out su -c 'cat <path>'
                m = re.match(r"cat (.*)$", inner)
                if m:
                    remote = _unquote(m.group(1))
                    data = _rootfs().files.get(remote)
                    if data is not None:
                        sys.stdout.buffer.write(data)
                    return 0
            return 0
        if sub == "pull":
            print("[100%] pulled")
            return 0
        if sub == "install":
            print("Success")
            return 0
        if sub == "uninstall":
            print("Success")
            return 0
        if sub == "reboot":
            return 0
        if sub == "logcat":
            return 0
        print(f"UNSUPPORTED: {args}")
        return 0

    print(f"UNSUPPORTED: {args}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
