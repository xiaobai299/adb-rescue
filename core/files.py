# -*- coding: utf-8 -*-
"""文件层：远程目录浏览、数据导出（照片/视频/文档/联系人/短信/应用数据）。

进度模型
--------
导出前先递归统计文件清单与总字节数，随后逐文件 `adb pull`，
按"已完成字节 / 总字节"计算进度。相比解析 adb 自带进度输出，
该方式对老版本 adb 同样可靠，且能给出"第 n / N 个文件"的可读信息。
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
from dataclasses import dataclass

from core.adb import AdbClient, AdbError, shell_quote

# 常用媒体/文档目录（按优先级）
QUICK_DIRS: list[tuple[str, str]] = [
    ("相册 / DCIM", "/sdcard/DCIM"),
    ("截图", "/sdcard/Pictures/Screenshots"),
    ("图片", "/sdcard/Pictures"),
    ("视频", "/sdcard/Movies"),
    ("下载", "/sdcard/Download"),
    ("文档", "/sdcard/Documents"),
    ("音乐", "/sdcard/Music"),
    ("录音", "/sdcard/Recordings"),
    ("微信", "/sdcard/Android/data/com.tencent.mm/MicroMsg"),
    ("QQ", "/sdcard/Android/data/com.tencent.mobileqq/Tencent"),
    ("存储卡根目录", "/sdcard"),
]

DOC_EXT = {".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
           ".csv", ".json", ".xml", ".zip", ".rar", ".7z", ".epub", ".md",
           ".log", ".html", ".htm", ".apk", ".db"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".3gp", ".flv", ".webm", ".m4v", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".amr"}

MONTHS = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}


@dataclass
class FileEntry:
    name: str
    path: str
    size: int = 0
    mtime: str = ""
    is_dir: bool = False
    perm: str = ""

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def kind(self) -> str:
        if self.is_dir:
            return "文件夹"
        e = self.ext
        if e in IMAGE_EXT:
            return "图片"
        if e in VIDEO_EXT:
            return "视频"
        if e in AUDIO_EXT:
            return "音频"
        if e in DOC_EXT:
            return "文档"
        return "文件"

    @property
    def size_text(self) -> str:
        return _human(self.size) if not self.is_dir else ""


def _human(size: int | float) -> str:
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


class FileManager:
    def __init__(self, adb: AdbClient, serial: str, root=None):
        self.adb = adb
        self.serial = serial
        self.root = root  # 可选 RootManager；启用后自动以 root 访问 /data 等私有目录

    def _shell(self, cmd: str, timeout: int = 30) -> str:
        res = self.adb.shell(self.serial, cmd, timeout=timeout)
        return res.stdout or res.stderr or ""

    # ---------------- 目录浏览 ---------------- #

    def list_dir(self, path: str) -> list[FileEntry]:
        """列出目录内容。兼容 toybox / toolbox 两种 ls 输出格式。

        普通 shell 无权限时，若已启用 root 则自动回退为 root 通道（可浏览
        /data、/data/data、Android 11+ 的 Android/data 等私有目录）。
        """
        out = self._shell(f"ls -la {shell_quote(path)} 2>&1")
        if "No such file or directory" in out:
            if self.root is not None:
                try:
                    return self._list_root(path)
                except AdbError:
                    pass
            raise AdbError(
                f"目录不存在：{path}",
                "部分 Android 版本不允许 shell 访问该目录（例如 Android 11+ 的 "
                "Android/data 需要借助『应用数据备份』功能）。",
                out,
            )
        if "Permission denied" in out:
            if self.root is not None:
                try:
                    return self._list_root(path)
                except AdbError:
                    pass
            raise AdbError(
                f"无权限访问：{path}",
                "该目录需要 root 权限。未 root 设备请改用『应用数据备份』或导出到存储卡的公开目录。",
                out,
            )
        entries: list[FileEntry] = []
        for line in out.splitlines():
            entry = _parse_ls_line(line.rstrip(), path)
            if entry:
                entries.append(entry)
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def _list_root(self, path: str) -> list[FileEntry]:
        """以 root 列出目录（复用 ls -la 解析器）。"""
        out = self.root.list_dir(path)  # type: ignore[union-attr]
        entries: list[FileEntry] = []
        for line in out:
            entry = _parse_ls_line(line.rstrip(), path)
            if entry:
                entries.append(entry)
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def walk(self, root: str, on_scan=None, max_files: int = 20000) -> list[FileEntry]:
        """递归收集文件（不含目录），使用广度优先避免深层递归。"""
        found: list[FileEntry] = []
        queue: list[str] = [root]
        while queue:
            current = queue.pop(0)
            try:
                items = self.list_dir(current)
            except AdbError:
                continue
            for item in items:
                if item.is_dir:
                    queue.append(item.path)
                else:
                    found.append(item)
                    if len(found) >= max_files:
                        return found
            if on_scan:
                on_scan(len(found), current)
        return found

    # ---------------- 导出 ---------------- #

    def pull_file(self, remote: str, local_dir: str, preserve_time: bool = True) -> str:
        os.makedirs(local_dir, exist_ok=True)
        # 私有目录（/data 等）普通 pull 无权限时，启用 root 则走流式通道
        if self.root is not None and not remote.startswith(("/sdcard/", "/storage/emulated/")):
            local = os.path.join(local_dir, os.path.basename(remote.rstrip("/")))
            st = self.root.stream_file(remote, local)
            if not st["ok"]:
                raise AdbError(f"导出失败：{os.path.basename(remote)}", "root 读取失败。", "")
            return st["file"]
        args = ["-s", self.serial, "pull", "-a" if preserve_time else "", remote, local_dir]
        args = [a for a in args if a]
        res = self.adb.raw(args, timeout=3600, binary=True)
        text = res.stdout or res.stderr or ""
        if not res.ok or "error" in text.lower():
            raise AdbError(f"导出失败：{os.path.basename(remote)}", self.adb.explain(text), text)
        return text.strip()

    def export(self, items: list[FileEntry], dest_root: str, base_dir: str = "/sdcard",
               on_progress=None, should_stop=None) -> dict:
        """批量导出。返回统计字典。

        on_progress(done_bytes, total_bytes, done_files, total_files, current_name)
        should_stop() -> bool 用于中途取消
        """
        total_bytes = sum(i.size for i in items)
        total_files = len(items)
        done_bytes = 0
        done_files = 0
        failed: list[tuple[str, str]] = []

        for item in items:
            if should_stop and should_stop():
                break
            rel = os.path.relpath(item.path, base_dir).lstrip("/\\")
            rel = rel.replace("\\", "/")
            local_dir = os.path.join(dest_root, os.path.dirname(rel))
            os.makedirs(local_dir, exist_ok=True)
            try:
                self.pull_file(item.path, local_dir)
                done_files += 1
            except AdbError as exc:
                failed.append((item.path, exc.message))
            done_bytes += item.size
            if on_progress:
                on_progress(done_bytes, total_bytes, done_files, total_files, item.name)

        return {
            "total_files": total_files,
            "done": done_files,
            "failed": failed,
            "total_bytes": total_bytes,
            "dest": dest_root,
        }

    def export_dir(self, remote_dir: str, dest_root: str, on_progress=None,
                   should_stop=None) -> dict:
        """整目录导出（先扫描再逐个拉取）。"""
        return self.export(
            self.walk(remote_dir), dest_root, base_dir=os.path.dirname(remote_dir.rstrip("/")) or "/",
            on_progress=on_progress, should_stop=should_stop,
        )

    # ---------------- 联系人 / 短信 ---------------- #

    def export_contacts(self, dest_dir: str) -> str:
        """导出通讯录为 CSV。依次尝试多个 ContentProvider URI。"""
        os.makedirs(dest_dir, exist_ok=True)
        rows = self._content_query(
            [
                "content://com.android.contacts/data/phones --projection display_name:data1:data2",
                "content://contacts/phones/ --projection display_name:number:type",
                "content://com.android.contacts/contacts --projection display_name:has_phone_number",
            ]
        )
        if not rows:
            raise AdbError(
                "未能读取通讯录",
                "设备的通讯录 ContentProvider 被厂商限制。替代方案：在手机上打开『联系人→设置→"
                "导入/导出→导出到存储卡』生成 .vcf 文件，再到本工具『数据导出』页导出该文件。",
            )
        path = os.path.join(dest_dir, f"contacts_{_stamp()}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.writer(fp)
            writer.writerow(["序号", "姓名", "号码", "号码类型"])
            seen = set()
            idx = 0
            for row in rows:
                name = row.get("display_name") or ""
                number = row.get("data1") or row.get("number") or ""
                if not number and not name:
                    continue
                key = (name, number)
                if key in seen:
                    continue
                seen.add(key)
                idx += 1
                writer.writerow([idx, name, number, _phone_type(row.get("data2") or row.get("type"))])
        return path

    def export_sms(self, dest_dir: str) -> str:
        """导出短信为 CSV。"""
        os.makedirs(dest_dir, exist_ok=True)
        rows = self._content_query([
            "content://sms --projection address:date:body:type:read",
            "content://sms/inbox --projection address:date:body:read",
            "content://mms-sms/conversations --projection snippet:date",
        ])
        if not rows:
            raise AdbError(
                "未能读取短信",
                "Android 4.4 之后仅默认短信应用可读取短信库，部分 ROM 会拒绝 adb 访问。"
                "替代方案：使用厂商自带的『短信备份』功能，或授予默认短信应用后重试。",
            )
        path = os.path.join(dest_dir, f"sms_{_stamp()}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.writer(fp)
            writer.writerow(["时间", "类型", "对方号码", "内容", "已读"])
            for row in rows:
                ts = row.get("date") or ""
                try:
                    time_str = dt.datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError, OSError):
                    time_str = ts
                writer.writerow([
                    time_str,
                    _sms_type(row.get("type")),
                    row.get("address") or "",
                    (row.get("body") or "").replace("\r", " ").replace("\n", " "),
                    "是" if str(row.get("read")) == "1" else "否",
                ])
        return path

    def _content_query(self, candidates: list[str]) -> list[dict]:
        """依次尝试 URI，返回第一个成功且非空的结果。"""
        for uri in candidates:
            out = self._shell(f"content query --uri {uri} 2>&1")
            if "Error" in out or "error" in out or not out.strip():
                continue
            rows = _parse_content_rows(out)
            if rows:
                return rows
        return []

    def export_call_logs(self, dest_dir: str) -> str:
        os.makedirs(dest_dir, exist_ok=True)
        rows = self._content_query([
            "content://call_log/calls --projection number:date:duration:type:name",
        ])
        path = os.path.join(dest_dir, f"calllog_{_stamp()}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.writer(fp)
            writer.writerow(["时间", "类型", "号码", "联系人", "通话时长(秒)"])
            for row in rows:
                try:
                    time_str = dt.datetime.fromtimestamp(int(row.get("date", 0)) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError, OSError):
                    time_str = row.get("date", "")
                writer.writerow([time_str, _call_type(row.get("type")), row.get("number", ""),
                                 row.get("name", ""), row.get("duration", "")])
        return path

    # ---------------- 应用数据 ---------------- #

    def app_data_dirs(self, package: str) -> list[str]:
        """返回该应用可能存在数据的公开目录。"""
        return [
            f"/sdcard/Android/data/{package}",
            f"/sdcard/Android/media/{package}",
            f"/sdcard/Android/obb/{package}",
        ]

    def export_app_data(self, package: str, dest_dir: str, on_progress=None,
                        should_stop=None) -> dict:
        """导出应用外置数据（免 root）。"""
        target = os.path.join(dest_dir, package)
        os.makedirs(target, exist_ok=True)
        collected: list[FileEntry] = []
        for d in self.app_data_dirs(package):
            try:
                items = self.walk(d)
            except AdbError:
                items = []
            collected.extend(items)
        if not collected:
            raise AdbError(
                f"未找到 {package} 的外置数据",
                "该应用可能未在外置存储写入数据，或 Android 11+ 限制了对 Android/data 的访问。"
                "可尝试：① 使用下方『adb backup 备份』；② 对已 root 设备改用 /data/data 导出。",
            )
        return self.export(collected, target, base_dir="/sdcard",
                           on_progress=on_progress, should_stop=should_stop)


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #

def _parse_ls_line(line: str, parent: str) -> FileEntry | None:
    """解析 `ls -la` 的单行输出，兼容 toybox 与旧版 toolbox 格式。"""
    if not line:
        return None
    if line.startswith("total ") or "No such file" in line or "Permission denied" in line:
        return None
    tokens = line.split()
    if len(tokens) < 6:
        return None

    perm = tokens[0]
    # 定位日期起始 token
    date_idx = -1
    date_len = 0
    for i, tok in enumerate(tokens[3:], start=3):
        if re.match(r"^\d{4}-\d{2}-\d{2}$", tok) or re.match(r"^\d{2}-\d{2}-\d{2}$", tok):
            date_idx, date_len = i, 2
            break
        if tok in MONTHS:
            date_idx, date_len = i, 3
            break
    if date_idx < 0:
        return None

    size_tok = ""
    for tok in reversed(tokens[3:date_idx]):
        if re.match(r"^\d+$", tok):
            size_tok = tok
            break
    name = " ".join(tokens[date_idx + date_len:])
    if not name or name in (".", ".."):
        return None
    link = " -> "
    if link in name:
        name = name.split(link)[0].strip()

    try:
        size = int(size_tok) if size_tok else 0
    except ValueError:
        size = 0

    path = parent.rstrip("/") + "/" + name
    mtime = " ".join(tokens[date_idx:date_idx + date_len])
    return FileEntry(name=name, path=path, size=size, mtime=mtime,
                     is_dir=perm.startswith("d"), perm=perm)


def _parse_content_rows(output: str) -> list[dict]:
    """解析 `content query` 输出：Row: 0 a=1, b=张三, c=null"""
    rows: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        body = line.split(":", 1)[1] if ":" in line else ""
        body = body.strip()
        # 去掉行号
        body = re.sub(r"^\d+\s*", "", body)
        row: dict[str, str] = {}
        for part in body.split(", "):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            row[k.strip()] = "" if v == "null" else v
        if row:
            rows.append(row)
    return rows


def _phone_type(code: str | None) -> str:
    return {"1": "住宅", "2": "手机", "3": "工作", "4": "工作传真", "5": "住宅传真",
            "6": "寻呼机", "7": "其他", "12": "总机"}.get(str(code or ""), "未知")


def _sms_type(code: str | None) -> str:
    return {"1": "接收", "2": "已发送", "3": "草稿", "4": "发件箱",
            "5": "发送失败", "6": "待发送"}.get(str(code or ""), f"类型{code or '?'}")


def _call_type(code: str | None) -> str:
    return {"1": "来电", "2": "去电", "3": "未接", "4": "语音信箱",
            "5": "拒接", "6": "拦截"}.get(str(code or ""), "未知")


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")
