# -*- coding: utf-8 -*-
"""应用管理页：安装/卸载/备份 APK、启动应用、导出应用数据。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.adb import AdbError
from core.ops import AppManager
from ui.widgets import ProgressPanel, run_async, Tooltip


class AppsPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.am: AppManager | None = None
        self.packages: list[tuple[str, str]] = []

        self._build_top()
        self._build_tree()
        self._build_actions()
        self.progress = ProgressPanel(self)
        self.progress.pack(fill="x", padx=10, pady=(0, 8))

    def _build_top(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="应用列表", style="Title.TLabel").pack(side="left")
        ttk.Button(bar, text="刷新", command=self.refresh).pack(side="left", padx=8)
        self.third_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="仅第三方应用", variable=self.third_var,
                        command=self.refresh).pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.search_var, width=26)
        e.pack(side="left", padx=8)
        e.bind("<KeyRelease>", lambda _ev: self._fill())
        Tooltip(e, "按包名或名称过滤。")

    def _build_tree(self) -> None:
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("label", "package", "apk")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c, head, width in (("label", "应用名称", 220), ("package", "包名", 320), ("apk", "APK 路径", 520)):
            self.tree.heading(c, text=head)
            self.tree.column(c, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        y = ttk.Scrollbar(wrap, command=self.tree.yview)
        y.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=y.set)

    def _build_actions(self) -> None:
        box = ttk.LabelFrame(self, text="  操作  ", padding=6)
        box.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Button(box, text="安装 APK", style="Accent.TButton",
                   command=self.install).pack(side="left")
        ttk.Button(box, text="备份 APK 到本地", command=self.backup_apk).pack(side="left", padx=6)
        ttk.Button(box, text="启动应用", command=lambda: self._act("启动")).pack(side="left")
        ttk.Button(box, text="强制停止", command=lambda: self._act("停止")).pack(side="left", padx=6)
        ttk.Button(box, text="导出应用数据", command=lambda: self._act("导出数据")).pack(side="left")

        ttk.Separator(box, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(box, text="卸载应用", style="Danger.TButton",
                   command=lambda: self._act("卸载")).pack(side="left")
        ttk.Button(box, text="禁用/启用", command=lambda: self._act("禁用")).pack(side="left", padx=6)
        ttk.Button(box, text="清除应用数据", style="Danger.TButton",
                   command=lambda: self._act("清除数据")).pack(side="left")
        ttk.Button(box, text="adb backup 备份", command=lambda: self._act("adb备份")).pack(side="left", padx=6)

        tip = ttk.Label(self, text="提示：卸载与清除数据会永久删除该应用的账号登录状态、聊天记录与本地文件，"
                                   "执行前请确认已完成数据导出。",
                        style="Muted.TLabel")
        tip.pack(anchor="w", padx=12)

    # ------------------------------------------------------------------ #

    def on_device(self, device) -> None:
        self.am = AppManager(self.app.adb, device.serial) if (device and device.online) else None
        for item in self.tree.get_children():
            self.tree.delete(item)
        if self.am:
            self.after(120, self.refresh)

    def refresh(self) -> None:
        if self.am is None:
            self.app.log("请先连接设备。", "warn")
            return

        def work():
            return self.am.list_packages(third_party=self.third_var.get())

        def done(pkgs):
            self.packages = pkgs
            self._fill()
            self.app.log(f"已加载 {len(pkgs)} 个应用", "ok")

        run_async(self.app, work, on_done=done, busy_text="读取应用列表…")

    def _fill(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        kw = self.search_var.get().strip().lower()
        for pkg, apk in self.packages:
            if kw and kw not in pkg.lower():
                continue
            self.tree.insert("", "end", values=("", pkg, apk))

    def _selected(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一个应用。")
            return None
        return self.tree.item(sel[0])["values"][1]

    # ------------------------------------------------------------------ #

    def install(self) -> None:
        if self.am is None:
            self.app.log("请先连接设备。", "warn")
            return
        paths = filedialog.askopenfilenames(
            title="选择要安装的 APK（可多选）",
            filetypes=[("APK 文件", "*.apk"), ("所有文件", "*.*")])
        if not paths:
            return

        def work():
            results = []
            for i, p in enumerate(paths):
                self.app.after_ui(
                    lambda i=i, p=p: self.progress.set(i / len(paths) * 100,
                                                       f"正在安装 {os.path.basename(p)}"))
                results.append((os.path.basename(p), self.am.install(p)))
            return results

        def done(results):
            self.progress.set(100, "安装完成")
            ok = sum(1 for _, r in results if r == "安装成功")
            self.app.log(f"安装完成：成功 {ok}/{len(results)}", "ok" if ok == len(results) else "warn")
            messagebox.showinfo("安装结果", "\n".join(f"{n}：{r}" for n, r in results))

        run_async(self.app, work, on_done=done, busy_text="安装 APK…")

    def backup_apk(self) -> None:
        pkg = self._selected()
        if not pkg or self.am is None:
            return
        dest = filedialog.askdirectory(title="选择 APK 备份目录")
        if not dest:
            return

        def work():
            return self.am.backup_apk(pkg, dest)

        def done(files):
            self.app.log(f"已备份 {len(files)} 个 APK 文件到 {dest}", "ok")
            messagebox.showinfo("备份完成", "\n".join(files))

        run_async(self.app, work, on_done=done, busy_text="备份 APK…")

    def _act(self, action: str) -> None:
        pkg = self._selected()
        if not pkg or self.am is None:
            return
        am = self.am

        risk_details = {
            "卸载": ("卸载应用",
                   f"即将卸载：{pkg}\n卸载后该应用的全部本地数据（聊天记录、登录状态、缓存文件）将被永久删除，且无法恢复。",
                   "danger"),
            "清除数据": ("清除应用数据",
                     f"即将清除：{pkg} 的数据与缓存\n相当于把应用恢复到刚安装的状态，登录信息将被登出。",
                     "danger"),
            "禁用": ("禁用/启用应用",
                   "禁用系统应用会将其从桌面隐藏并停止运行。若误禁用了桌面或系统界面组件，"
                   "可能导致手机无法正常操作，请谨慎选择。",
                   "warn"),
        }

        if action in risk_details:
            title, msg, risk = risk_details[action]
            require = "清除数据" if action == "清除数据" else None
            if not self.app.confirm(title=f"确认{action}", message=msg, risk=risk,
                                    require_text=require,
                                    details=f"包名：{pkg}"):
                self.app.log(f"已取消：{action} {pkg}", "warn")
                return

        def work():
            if action == "启动":
                return am.launch(pkg)
            if action == "停止":
                return am.force_stop(pkg)
            if action == "卸载":
                return am.uninstall(pkg)
            if action == "清除数据":
                return am.clear_data(pkg)
            if action == "禁用":
                out = am.disable(pkg)
                return out
            if action == "导出数据":
                dest = os.path.join(self.app.cfg.get("export_dir"), "appdata")
                os.makedirs(dest, exist_ok=True)
                # 设备已 root：完整备份（APK + /data/data 内部数据 + 外置数据）
                if self.app.device and self.app.device.rooted:
                    from core.root import RootManager
                    rm = RootManager(self.app.adb, self.app.device.serial)

                    def on_step(text):
                        self.app.after_ui(lambda: self.progress.set(50, text))
                    result = rm.backup_app_full(pkg, dest, on_progress=on_step)
                    target = os.path.join(dest, pkg)
                    apks = len(result["apks"]); arch = len(result["archives"])
                    extra = f"\n失败项：{'; '.join(result['errors'])}" if result["errors"] else ""
                    return f"完整备份完成（root）：APK {apks} 个、数据包 {arch} 个 → {target}{extra}"
                from core.files import FileManager
                fm = FileManager(self.app.adb, self.app.device.serial)

                def on_progress(done_b, total_b, done_f, total_f, name):
                    pct = (done_b / total_b * 100) if total_b else 0
                    self.app.after_ui(
                        lambda: self.progress.set(pct, f"{done_f}/{total_f}｜{name}"))
                stats = fm.export_app_data(pkg, dest, on_progress=on_progress)
                return f"导出完成：{stats['done']}/{stats['total_files']} 个文件 → {stats['dest']}"
            if action == "adb备份":
                dest = os.path.join(self.app.cfg.get("export_dir"), "appdata")
                os.makedirs(dest, exist_ok=True)
                return am.adb_backup(pkg, os.path.join(dest, f"{pkg}.ab"))
            raise AdbError("未知操作", action)

        def done(result):
            self.app.log(f"{action} {pkg} → {result}", "ok")
            messagebox.showinfo("操作完成", str(result))

        run_async(self.app, work, on_done=done, busy_text=f"{action}中…")
