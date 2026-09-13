# -*- coding: utf-8 -*-
"""自动备份层（v2.1 新增）：连接即备份的调度与增量记录。

设计
----
1. BACKUP_CLASSES 定义可勾选的备份类别（照片/截图/下载/文档/音乐/短信/
   通讯录/通话记录/微信数据/指定应用全量）。每项声明"源目录 + 扩展名过滤"
   或"调用哪个导出函数"，界面只按 key 勾选，执行逻辑集中在这里。
2. 增量备份用目标目录下的 `.manifest.json`（key -> {相对路径: 字节数}）：
   同名同大小 = 已备份过，跳过；大小变化或新文件才拉取。不依赖设备端
   mtime 解析（toybox/busybox 格式不一，不可靠）。
3. 短信/通讯录/通话记录每次全量导出 CSV（文件名带时间戳，绝不覆盖旧数据）。
4. 微信等 root 聊天数据走 RootManager.export_chat_data（tar.gz 流式）。
5. 本层纯逻辑、无界面依赖；UI 在数据导出页与主窗口设备检测处调用。
"""

from __future__ import annotations

import json
import os
import time

from core.adb import AdbClient, AdbError
from core.files import (DOC_EXT, IMAGE_EXT, AUDIO_EXT, VIDEO_EXT,
                        FileManager)

#: 备份类别定义：key -> {label, kind, dirs?, ext?, need_root?}
BACKUP_CLASSES: dict[str, dict] = {
    "photos": {"label": "相册照片视频", "kind": "files",
               "dirs": ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies"],
               "ext": IMAGE_EXT | VIDEO_EXT},
    "screenshots": {"label": "截图", "kind": "files",
                    "dirs": ["/sdcard/Pictures/Screenshots", "/sdcard/DCIM/Screenshots"],
                    "ext": IMAGE_EXT},
    "downloads": {"label": "下载目录", "kind": "files",
                  "dirs": ["/sdcard/Download"], "ext": None},
    "docs": {"label": "文档", "kind": "files",
             "dirs": ["/sdcard/Documents", "/sdcard/Download", "/sdcard/Books"],
             "ext": DOC_EXT},
    "music": {"label": "音乐录音", "kind": "files",
              "dirs": ["/sdcard/Music", "/sdcard/Recordings"],
              "ext": AUDIO_EXT},
    "sms": {"label": "短信", "kind": "db", "export": "sms"},
    "contacts": {"label": "通讯录", "kind": "db", "export": "contacts"},
    "calllog": {"label": "通话记录", "kind": "db", "export": "calllog"},
    "wechat": {"label": "微信数据（root）", "kind": "chat", "package": "com.tencent.mm",
               "need_root": True},
}


class AutoBackup:
    """自动备份执行器：run(items, dest) 一次完整备份。"""

    def __init__(self, adb: AdbClient, serial: str, root=None):
        self.adb = adb
        self.serial = serial
        self.fm = FileManager(adb, serial, root=root)
        self.root = root  # RootManager 或 None

    # ---------------- 清单（增量记录） ---------------- #

    @staticmethod
    def _manifest_path(dest: str) -> str:
        return os.path.join(dest, ".manifest.json")

    def _load_manifest(self, dest: str) -> dict:
        try:
            with open(self._manifest_path(dest), "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:  # noqa: BLE001
            return {}

    def _save_manifest(self, dest: str, data: dict) -> None:
        try:
            with open(self._manifest_path(dest), "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False)
        except OSError:
            pass

    # ---------------- 执行 ---------------- #

    def run(self, items: list[str], dest: str, *, chat_packages: list[str] | None = None,
            on_progress=None, should_stop=None) -> dict:
        """执行一次备份。items 为 BACKUP_CLASSES 的 key 列表。

        on_progress(text) 进度提示；should_stop() 可中途取消。
        返回 {"new": n, "skipped": n, "failed": [(path, msg)], "dirs": [...], "files": [...]}
        """
        os.makedirs(dest, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        manifest = self._load_manifest(dest)
        result = {"new": 0, "skipped": 0, "failed": [], "outputs": [],
                  "stopped": False}

        def say(text: str) -> None:
            if on_progress:
                on_progress(text)

        for key in items:
            if should_stop and should_stop():
                result["stopped"] = True
                break
            spec = BACKUP_CLASSES.get(key)
            if not spec:
                continue
            try:
                if spec["kind"] == "files":
                    self._backup_files(key, spec, dest, manifest, result, say, should_stop)
                elif spec["kind"] == "db":
                    self._backup_db(spec, dest, stamp, result, say)
                elif spec["kind"] == "chat":
                    self._backup_chat([spec["package"]], dest, result, say)
            except AdbError as exc:
                result["failed"].append((key, exc.message))
                say(f"✗ {spec['label']}：{exc.message}")

        # 勾选了微信之外的自定义聊天应用（界面传入）
        extra = chat_packages or []
        if extra and self.root is not None:
            try:
                self._backup_chat(extra, dest, result, say)
            except AdbError as exc:
                result["failed"].append(("chat_extra", exc.message))

        self._save_manifest(dest, manifest)
        return result

    # ---------------- 文件类 ---------------- #

    def _backup_files(self, key: str, spec: dict, dest: str, manifest: dict,
                      result: dict, say, should_stop) -> None:
        seen = manifest.setdefault(key, {})
        target = os.path.join(dest, key)
        for base in spec["dirs"]:
            if should_stop and should_stop():
                result["stopped"] = True
                return
            say(f"扫描 {base} …")
            try:
                files = self.fm.walk(base)
            except AdbError:
                continue
            for item in files:
                if item.ext and spec.get("ext") and item.ext not in spec["ext"]:
                    continue
                rel = item.path
                rec = seen.get(rel)
                if rec is not None and item.size and rec == item.size:
                    result["skipped"] += 1
                    continue
                if should_stop and should_stop():
                    result["stopped"] = True
                    self._save_manifest_partial(target, seen, result)
                    return
                local_dir = os.path.join(target, os.path.dirname(
                    rel.replace(base, "").lstrip("/")))
                try:
                    self.fm.pull_file(rel, local_dir)
                    seen[rel] = item.size
                    result["new"] += 1
                    result["outputs"].append(rel)
                    say(f"已备份（新增 {result['new']}，跳过 {result['skipped']}）：{item.name}")
                except AdbError as exc:
                    result["failed"].append((rel, exc.message))

    def _save_manifest_partial(self, target: str, seen: dict, result: dict) -> None:
        # 中途取消时把已完成部分落盘（调用方 run() 结尾统一保存，这里留空占位）
        pass

    # ---------------- 数据库类（短信/通讯录/通话） ---------------- #

    def _backup_db(self, spec: dict, dest: str, stamp: str, result: dict, say) -> None:
        say(f"导出 {spec['label']} …")
        if self.root is not None:
            export = {"sms": self.root.export_sms_root,
                      "contacts": self.root.export_contacts_root,
                      "calllog": self.root.export_calllog_root}[spec["export"]]
            path = export(dest)
        else:
            export = {"sms": self.fm.export_sms,
                      "contacts": self.fm.export_contacts,
                      "calllog": self.fm.export_call_logs}[spec["export"]]
            path = export(dest)
        result["new"] += 1
        result["outputs"].append(path)
        say(f"✓ {spec['label']} → {path}")

    # ---------------- 聊天应用数据（root） ---------------- #

    def _backup_chat(self, packages: list[str], dest: str, result: dict, say) -> None:
        if self.root is None:
            raise AdbError("设备未 root，跳过聊天数据备份",
                           "微信等应用内部数据需要 root 权限直读。", "")
        for pkg in packages:
            say(f"打包 {pkg} 数据 …")
            target = self.root.export_chat_data(pkg, os.path.join(dest, "chatdata"))
            result["new"] += 1
            result["outputs"].append(target)
            say(f"✓ {pkg} → {target}")


def summarize(result: dict) -> str:
    """把 run() 的结果转成一行中文日志。"""
    parts = [f"新增 {result['new']}", f"跳过 {result['skipped']}"]
    if result["failed"]:
        parts.append(f"失败 {len(result['failed'])}（例：{result['failed'][0][1]}）")
    if result.get("stopped"):
        parts.append("已中途取消")
    return " ｜ ".join(parts)
