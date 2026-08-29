# -*- coding: utf-8 -*-
"""数据导出页：远程文件浏览、批量导出、通讯录/短信/通话记录导出。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.adb import AdbError
from core.files import FileEntry, FileManager, QUICK_DIRS
from ui.widgets import COLOR, ProgressPanel, run_async, Tooltip

KIND_FILTERS = ["全部", "图片", "视频", "音频", "文档", "文件夹"]


class FilesPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.fm: FileManager | None = None
        self.current = "/sdcard"
        self.entries: list[FileEntry] = []
        self._stop = False

        self._build_top()
        self._build_tree()
        self._build_bottom()

    # ------------------------------------------------------------------ #

    def _build_top(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="快捷目录").pack(side="left")
        self.quick_var = tk.StringVar(value=QUICK_DIRS[0][0])
        q = ttk.Combobox(bar, textvariable=self.quick_var, width=18, state="readonly",
                         values=[n for n, _ in QUICK_DIRS])
        q.pack(side="left", padx=(2, 6))
        q.bind("<<ComboboxSelected>>", self._open_quick)
        ttk.Button(bar, text="打开", command=self._open_quick).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.path_var = tk.StringVar(value=self.current)
        e = ttk.Entry(bar, textvariable=self.path_var, width=46)
        e.pack(side="left", padx=(0, 4))
        e.bind("<Return>", lambda _ev: self.browse(self.path_var.get().strip()))
        ttk.Button(bar, text="转到", command=lambda: self.browse(self.path_var.get().strip())).pack(side="left")
        ttk.Button(bar, text="上级", command=self.go_up).pack(side="left", padx=4)
        ttk.Button(bar, text="刷新", command=lambda: self.browse(self.current)).pack(side="left")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(bar, text="筛选").pack(side="left")
        self.kind_var = tk.StringVar(value="全部")
        k = ttk.Combobox(bar, textvariable=self.kind_var, width=8, state="readonly",
                         values=KIND_FILTERS)
        k.pack(side="left", padx=(2, 4))
        k.bind("<<ComboboxSelected>>", lambda _e: self._fill_tree(self.entries))
        self.search_var = tk.StringVar()
        s = ttk.Entry(bar, textvariable=self.search_var, width=14)
        s.pack(side="left")
        s.bind("<KeyRelease>", lambda _e: self._fill_tree(self.entries))
        Tooltip(s, "按文件名过滤，边输入边筛选。")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.root_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Root 模式（可访问 /data 等私有目录）",
                        variable=self.root_mode, command=self._on_root_mode).pack(side="left", padx=2)
        Tooltip(bar, "启用后自动使用 root 通道浏览与导出 /data、/data/data、"
                     "Android 11+ 的 Android/data 等普通 shell 无权访问的目录。\n"
                     "需设备已 root 并授予 Shell 超级用户权限。")

    def _build_tree(self) -> None:
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8, pady=4)

        cols = ("name", "kind", "size", "mtime", "path")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="extended")
        heads = {"name": "名称", "kind": "类型", "size": "大小", "mtime": "修改时间", "path": "完整路径"}
        widths = {"name": 240, "kind": 70, "size": 100, "mtime": 150, "path": 460}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(wrap, command=self.tree.yview)
        yscroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.tag_configure("dir", foreground=COLOR["primary"])

    def _build_bottom(self) -> None:
        box = ttk.LabelFrame(self, text="  导出  ", padding=6)
        box.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Label(box, text="保存到").pack(side="left")
        self.dest_var = tk.StringVar(value=self.app.cfg.get("export_dir"))
        ttk.Entry(box, textvariable=self.dest_var, width=52).pack(side="left", padx=4)
        ttk.Button(box, text="浏览", width=6,
                   command=lambda: _pick(self.dest_var)).pack(side="left")

        ttk.Separator(box, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(box, text="导出选中项", style="Accent.TButton",
                   command=self.export_selected).pack(side="left")
        ttk.Button(box, text="导出整个目录", command=self.export_current_dir).pack(side="left", padx=6)
        self.btn_stop = ttk.Button(box, text="停止", command=self.stop_export, state="disabled")
        self.btn_stop.pack(side="left")

        ttk.Separator(box, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(box, text="导出通讯录", command=self.export_contacts).pack(side="left")
        ttk.Button(box, text="导出短信", command=self.export_sms).pack(side="left", padx=4)
        ttk.Button(box, text="导出通话记录", command=self.export_calls).pack(side="left")

        self.progress = ProgressPanel(self)
        self.progress.pack(fill="x", padx=10, pady=(0, 6))

    # ------------------------------------------------------------------ #

    def on_device(self, device) -> None:
        if device is not None and device.online:
            from core.root import RootManager
            root = RootManager(self.app.adb, device.serial) if device.rooted else None
            self.fm = FileManager(self.app.adb, device.serial, root=root)
            self.after(100, lambda: self.browse(self.current))
        else:
            self.fm = None
            for item in self.tree.get_children():
                self.tree.delete(item)

    def _on_root_mode(self) -> None:
        """Root 模式开关：重建 FileManager 并刷新当前目录。"""
        if self.app.device is None or not self.app.device.online:
            self.root_mode.set(False)
            self.app.log("请先连接设备。", "warn")
            return
        if self.root_mode.get() and not self.app.device.rooted:
            self.app.log("当前设备未检测到 root，Root 模式不可用。", "warn")
            self.root_mode.set(False)
            return
        from core.root import RootManager
        root = RootManager(self.app.adb, self.app.device.serial) if self.root_mode.get() else None
        self.fm = FileManager(self.app.adb, self.app.device.serial, root=root)
        if root is not None and not root.su_available():
            self.app.log("root 通道不可用（su 未授权）。请检查 Magisk 的 Shell 超级用户授权。", "warn")
        self.browse(self.current)

    def _open_quick(self, _event=None) -> None:
        name = self.quick_var.get()
        path = next((p for n, p in QUICK_DIRS if n == name), "/sdcard")
        self.browse(path)

    def go_up(self) -> None:
        parent = os.path.dirname(self.current.rstrip("/"))
        self.browse(parent or "/")

    def browse(self, path: str) -> None:
        if self.fm is None:
            self.app.log("请先连接并选择一台已授权的设备。", "warn")
            return
        path = path.strip() or "/sdcard"
        self.current = path
        self.path_var.set(path)

        def work():
            return self.fm.list_dir(path)

        def done(entries):
            self.entries = entries
            self._fill_tree(entries)
            self.app.log(f"已列出 {path}，共 {len(entries)} 项", "ok")

        run_async(self.app, work, on_done=done, busy_text=f"读取目录 {path} …")

    def _fill_tree(self, entries) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        kind = self.kind_var.get()
        kw = self.search_var.get().strip().lower()
        for e in entries:
            if kind != "全部" and e.kind != kind:
                continue
            if kw and kw not in e.name.lower():
                continue
            self.tree.insert("", "end", values=(e.name, e.kind, e.size_text, e.mtime, e.path),
                             tags=("dir",) if e.is_dir else ())

    def _on_double(self, _event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0])["values"]
        if values[1] == "文件夹":
            self.browse(values[4])

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #

    def _selected_entries(self) -> list[FileEntry]:
        result = []
        for item in self.tree.selection():
            values = self.tree.item(item)["values"]
            result.append(FileEntry(name=values[0], path=values[4], is_dir=values[1] == "文件夹"))
        return result

    def export_selected(self) -> None:
        items = self._selected_entries()
        if not items:
            messagebox.showinfo("提示", "请先在文件列表中选中要导出的文件或文件夹（可多选：Ctrl / Shift）。")
            return
        self._run_export(items, os.path.dirname(items[0].path.rstrip("/")) or "/sdcard")

    def export_current_dir(self) -> None:
        def work():
            return self.fm.walk(self.current)

        def done(files):
            if not files:
                self.app.log("该目录下没有可导出的文件。", "warn")
                return
            if len(files) > 3000:
                messagebox.showwarning(
                    "文件数量过多",
                    f"该目录共 {len(files)} 个文件，导出可能耗时较长。\n"
                    "建议先进入子目录分批导出。")
            self._run_export(files, os.path.dirname(self.current.rstrip("/")) or "/sdcard")

        if self.fm is None:
            self.app.log("请先连接设备。", "warn")
            return
        run_async(self.app, work, on_done=done, busy_text="扫描目录…")

    def _run_export(self, items, base_dir: str) -> None:
        dest_root = self.dest_var.get().strip() or self.app.cfg.get("export_dir")
        self.app.cfg.set("export_dir", dest_root)
        os.makedirs(dest_root, exist_ok=True)
        self._stop = False
        self.btn_stop.configure(state="normal")
        self.progress.reset("准备导出…")

        need_scan = any(i.is_dir for i in items)
        fm = self.fm

        def work():
            todo = []
            if need_scan:
                self.app.after_ui(lambda: self.progress.set(0, "正在扫描所选文件夹…"))
                for i in items:
                    if self._stop:
                        break
                    if i.is_dir:
                        todo.extend(fm.walk(i.path))
                    else:
                        todo.append(i)
            else:
                todo = list(items)
            if not todo:
                raise AdbError("没有可导出的文件", "所选文件夹为空，或设备不允许访问该路径。")

            def on_progress(done_bytes, total_bytes, done_files, total_files, name):
                pct = (done_bytes / total_bytes * 100) if total_bytes else \
                    (done_files / total_files * 100 if total_files else 100)
                text = (f"{done_files}/{total_files} 个文件｜{done_bytes/1048576:.1f}/"
                        f"{total_bytes/1048576:.1f} MB｜正在导出：{name}")
                self.app.after_ui(lambda: self.progress.set(pct, text))
                self.app.after_ui(lambda: self.app.progress.set(pct, text))

            return fm.export(todo, dest_root, base_dir=base_dir,
                             on_progress=on_progress, should_stop=lambda: self._stop)

        def done(stats):
            self.btn_stop.configure(state="disabled")
            self.app.progress.reset()
            msg = (f"导出完成：成功 {stats['done']}/{stats['total_files']} 个文件，"
                   f"共 {stats['total_bytes']/1048576:.1f} MB\n保存位置：{stats['dest']}")
            if stats["failed"]:
                msg += f"\n失败 {len(stats['failed'])} 个，例如：{stats['failed'][0][1]}"
            self.progress.set(100, "导出完成")
            self.app.log(msg.replace("\n", " ｜ "), "ok")
            messagebox.showinfo("导出完成", msg)

        run_async(self.app, work, on_done=done, busy_text="导出中…")

    def stop_export(self) -> None:
        self._stop = True
        self.progress.set(self.progress.value, "正在取消…")
        self.app.log("已请求停止，当前文件传输完成后即中止。", "warn")

    def export_contacts(self) -> None:
        self._export_simple("通讯录", lambda fm, d: fm.export_contacts(d))

    def export_sms(self) -> None:
        self._export_simple("短信", lambda fm, d: fm.export_sms(d))

    def export_calls(self) -> None:
        self._export_simple("通话记录", lambda fm, d: fm.export_call_logs(d))

    def _export_simple(self, label: str, func) -> None:
        if self.fm is None:
            self.app.log("请先连接设备。", "warn")
            return
        dest = self.dest_var.get().strip() or self.app.cfg.get("export_dir")
        os.makedirs(dest, exist_ok=True)

        def work():
            return func(self.fm, dest)

        def done(path):
            self.app.log(f"{label}已导出：{path}", "ok")
            messagebox.showinfo("导出完成", f"{label}已保存为 CSV：\n{path}")

        run_async(self.app, work, on_done=done, busy_text=f"导出{label}…")


def _pick(var: tk.StringVar) -> None:
    path = filedialog.askdirectory(title="选择导出保存目录")
    if path:
        var.set(path)
