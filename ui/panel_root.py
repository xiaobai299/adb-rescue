# -*- coding: utf-8 -*-
"""Root 高级页：root 状态卡、一键提速、应用完整备份、聊天/系统数据导出、
Root 文件浏览、系统应用管理。全部功能依赖设备已 root（Magisk/SuperSU）。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.adb import AdbError
from core.ops import AppManager
from core.root import CHAT_APPS, RootManager
from ui.widgets import COLOR, FONT, ProgressPanel, run_async, Tooltip


class RootPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.root: RootManager | None = None
        self.packages: list[str] = []
        self.root_entries: list[str] = []
        self._root_current = "/data"

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=6)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=6)

        self._build_status(left)
        self._build_speed(left)
        self._build_backup(left)
        self._build_export(right)
        self._build_sysapps(right)
        self._build_browser(self)

    # ------------------------------------------------------------------ #
    # Root 状态卡
    # ------------------------------------------------------------------ #

    def _build_status(self, master) -> None:
        box = ttk.LabelFrame(master, text="  Root 状态  ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        self.status_text = tk.Text(box, height=5, font=(FONT, 9), relief="solid",
                                   bd=1, wrap="word")
        self.status_text.pack(fill="x")
        self.status_text.insert("1.0", "尚未检测。连接设备后点击『刷新状态』。")
        self.status_text.configure(state="disabled")

        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="刷新状态", style="Accent.TButton",
                   command=self.refresh_status).pack(side="left")
        ttk.Button(bar, text="adb root（userdebug）", command=self.adb_root).pack(side="left", padx=6)
        ttk.Button(bar, text="打开 Magisk", command=self.open_magisk).pack(side="left")

    def _set_status(self, text: str) -> None:
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.insert("1.0", text)
        self.status_text.configure(state="disabled")

    def refresh_status(self) -> None:
        if self.root is None:
            self.app.log("请先连接并选择一台已授权的设备。", "warn")
            return

        def work():
            st = self.root.get_status(refresh=True)
            info = self.root.system_info_root() if st.available else {}
            return st, info

        def done(data):
            st, info = data
            lines = [f"Root：{'✔ 可用（' + st.method_text + '）' if st.available else '✘ 未获取'}"]
            if st.magisk_version:
                lines.append(f"Magisk 版本：{st.magisk_version}")
            if st.adbd_as_root:
                lines.append("adbd：已以 root 运行")
            elif st.adb_root_ok:
                lines.append("固件：userdebug（可执行『adb root』）")
            lines.append(f"SELinux：{st.selinux}")
            lines.append(f"/data/data 访问：{'可读' if st.data_readable else '不可读'}")
            if info:
                for k, v in info.items():
                    if k not in ("SELinux",):
                        lines.append(f"{k}：{v}")
            self._set_status("\n".join(lines))
            self.app.log(f"Root 状态已刷新：{'可用' if st.available else '不可用'}", "ok" if st.available else "warn")
            if not st.available:
                self.app.log("提示：root 功能需 Magisk/SuperSU 已安装且已授予 Shell 超级用户权限。", "warn")

        run_async(self.app, work, on_done=done, busy_text="检测 Root…")

    def adb_root(self) -> None:
        if self.root is None:
            self.app.log("请先连接设备。", "warn")
            return

        def work():
            return self.root.adb_root()

        def done(msg):
            self.app.log(msg, "ok")
            messagebox.showinfo("adb root", msg + "\n\n随后请点击顶部『刷新设备』重新连接。")

        run_async(self.app, work, on_done=done, busy_text="重启 adbd…")

    def open_magisk(self) -> None:
        if self.root is None:
            self.app.log("请先连接设备。", "warn")
            return
        run_async(self.app, self.root.open_magisk,
                  on_done=lambda m: self.app.log(m, "ok"), busy_text="打开 Magisk…")

    # ------------------------------------------------------------------ #
    # 一键提速
    # ------------------------------------------------------------------ #

    def _build_speed(self, master) -> None:
        box = ttk.LabelFrame(master, text="  操控提速（root 系统级优化）  ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Button(bar, text="一键提速", style="Accent.TButton",
                   command=lambda: self._speed("all")).pack(side="left")
        ttk.Button(bar, text="关闭动画", command=lambda: self._speed("anim_off")).pack(side="left", padx=4)
        ttk.Button(bar, text="恢复动画", command=lambda: self._speed("anim_on")).pack(side="left")
        ttk.Button(bar, text="强制 GPU 渲染", command=lambda: self._speed("gpu")).pack(side="left", padx=4)
        ttk.Button(bar, text="屏幕常亮", command=lambda: self._speed("stayon")).pack(side="left")
        ttk.Button(bar, text="取消常亮", command=lambda: self._speed("stayoff")).pack(side="left", padx=4)

        bar2 = ttk.Frame(box)
        bar2.pack(fill="x", pady=(6, 0))
        ttk.Button(bar2, text="SELinux 宽容（临时）", command=lambda: self._speed("se_permissive")).pack(side="left")
        ttk.Button(bar2, text="SELinux 强制（恢复）", command=lambda: self._speed("se_enforcing")).pack(side="left", padx=4)
        tip = ttk.Label(box, text="一键提速 = 关闭动画 + 强制 GPU 渲染；SELinux 宽容为临时调整，重启手机后自动恢复。",
                        style="Muted.TLabel", wraplength=560)
        tip.pack(anchor="w", pady=(6, 0))

    def _speed(self, mode: str) -> None:
        if self.root is None:
            self.app.log("请先连接设备。", "warn")
            return

        def work():
            msgs = []
            if mode in ("all", "anim_off"):
                msgs.append(self.root.set_animations(0))
            if mode == "anim_on":
                msgs.append(self.root.set_animations(1))
            if mode in ("all", "gpu"):
                msgs.append(self.root.force_gpu(True))
            if mode == "stayon":
                msgs.append(self.root.keep_screen_on_root())
            if mode == "stayoff":
                msgs.append(self.root.cancel_keep_screen_on())
            if mode == "se_permissive":
                out = self.root.set_selinux("Permissive")
                msgs.append("SELinux：" + out)
            if mode == "se_enforcing":
                out = self.root.set_selinux("Enforcing")
                msgs.append("SELinux：" + out)
            return "；".join(msgs)

        run_async(self.app, work,
                  on_done=lambda m: (self.app.log(f"✓ {m}", "ok"),
                                     self.refresh_status()),
                  busy_text="执行系统优化…")

    # ------------------------------------------------------------------ #
    # 应用完整备份
    # ------------------------------------------------------------------ #

    def _build_backup(self, master) -> None:
        box = ttk.LabelFrame(master, text="  应用完整备份（APK + 内部数据 + 外置数据，root）  ", padding=8)
        box.pack(fill="both", expand=True, pady=(0, 8))

        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Label(bar, text="应用").pack(side="left")
        self.pkg_var = tk.StringVar()
        self.pkg_combo = ttk.Combobox(bar, textvariable=self.pkg_var, width=28)
        self.pkg_combo.pack(side="left", padx=(2, 6))
        ttk.Button(bar, text="加载列表", command=self.load_packages).pack(side="left")
        ttk.Button(bar, text="自定义包名", command=self.manual_pkg).pack(side="left", padx=4)
        Tooltip(self.pkg_combo, "选择已安装的第三方应用，或输入任意包名（例如 com.tencent.mm）。")

        bar2 = ttk.Frame(box)
        bar2.pack(fill="x", pady=(6, 0))
        self.opt_apk = tk.BooleanVar(value=True)
        self.opt_data = tk.BooleanVar(value=True)
        self.opt_ext = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar2, text="包含 APK", variable=self.opt_apk).pack(side="left")
        ttk.Checkbutton(bar2, text="包含内部数据 (/data/data)", variable=self.opt_data).pack(side="left", padx=8)
        ttk.Checkbutton(bar2, text="包含外置数据", variable=self.opt_ext).pack(side="left")

        bar3 = ttk.Frame(box)
        bar3.pack(fill="x", pady=(6, 0))
        ttk.Label(bar3, text="保存到").pack(side="left")
        self.bak_dir_var = tk.StringVar(value=os.path.join(self.app.cfg.get("export_dir"), "full_backup"))
        ttk.Entry(bar3, textvariable=self.bak_dir_var, width=42).pack(side="left", padx=4)
        ttk.Button(bar3, text="浏览", width=6, command=self._pick_bak_dir).pack(side="left")
        ttk.Button(bar3, text="开始完整备份", style="Accent.TButton",
                   command=self.do_backup).pack(side="left", padx=8)

        self.bak_progress = ProgressPanel(box)
        self.bak_progress.pack(fill="x", pady=(6, 0))

    def _pick_bak_dir(self) -> None:
        p = filedialog.askdirectory(title="选择备份保存目录")
        if p:
            self.bak_dir_var.set(p)

    def load_packages(self) -> None:
        if self.app.device is None or not self.app.device.online:
            self.app.log("请先连接设备。", "warn")
            return
        am = AppManager(self.app.adb, self.app.device.serial)

        def work():
            return [p for p, _ in am.list_packages(third_party=True)]

        def done(pkgs):
            self.packages = pkgs
            self.pkg_combo["values"] = pkgs
            self.app.log(f"已加载 {len(pkgs)} 个第三方应用。", "ok")

        run_async(self.app, work, on_done=done, busy_text="加载应用列表…")

    def manual_pkg(self) -> None:
        pkg = self.pkg_var.get().strip()
        if pkg:
            self.pkg_combo.set(pkg)

    def do_backup(self) -> None:
        pkg = self.pkg_var.get().strip()
        if not pkg or self.root is None:
            messagebox.showinfo("提示", "请先选择或输入包名，并确认已连接设备。")
            return
        if not self.app.device.rooted:
            self.app.log("当前设备未检测到 root，完整备份不可用。", "warn")
            return
        dest = self.bak_dir_var.get().strip()
        if not dest:
            dest = os.path.join(self.app.cfg.get("export_dir"), "full_backup")
            self.bak_dir_var.set(dest)
        if not self.app.confirm(
                title="确认完整备份",
                message=f"将备份应用：{pkg}\n包含 APK、/data/data 内部数据与外置数据。\n"
                        "内部数据包含登录态、聊天记录等隐私内容，请妥善保管备份文件。",
                risk="warn", confirm_label="开始备份"):
            return
        self.bak_progress.reset("准备备份…")

        def work():
            return self.root.backup_app_full(
                pkg, dest,
                include_apk=self.opt_apk.get(),
                include_data=self.opt_data.get(),
                include_external=self.opt_ext.get(),
                on_progress=lambda t: self.app.after_ui(lambda: self.bak_progress.set(50, t)),
            )

        def done(result):
            self.bak_progress.set(100, "备份完成")
            apks = len(result["apks"])
            arch = len(result["archives"])
            msg = f"备份完成：APK {apks} 个，数据包 {arch} 个\n保存位置：{os.path.join(dest, pkg)}"
            if result["errors"]:
                msg += "\n\n部分项失败：" + "\n".join(result["errors"])
            self.app.log(msg.replace("\n", " ｜ "), "ok" if not result["errors"] else "warn")
            messagebox.showinfo("备份完成", msg)

        run_async(self.app, work, on_done=done, busy_text="完整备份中…")

    # ------------------------------------------------------------------ #
    # 系统应用管理
    # ------------------------------------------------------------------ #

    def _build_sysapps(self, master) -> None:
        box = ttk.LabelFrame(master, text="  系统应用管理（可恢复）  ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Label(bar, text="包名").pack(side="left")
        self.sys_var = tk.StringVar()
        self.sys_combo = ttk.Combobox(bar, textvariable=self.sys_var, width=26)
        self.sys_combo.pack(side="left", padx=(2, 6))
        ttk.Button(bar, text="列系统应用", command=self.load_sysapps).pack(side="left")

        bar2 = ttk.Frame(box)
        bar2.pack(fill="x", pady=(6, 0))
        ttk.Button(bar2, text="冻结", command=lambda: self._sys_op("freeze")).pack(side="left")
        ttk.Button(bar2, text="解冻", command=lambda: self._sys_op("unfreeze")).pack(side="left", padx=4)
        ttk.Button(bar2, text="卸载（仅当前用户）", style="Danger.TButton",
                   command=lambda: self._sys_op("uninstall")).pack(side="left")
        ttk.Button(bar2, text="恢复系统应用", command=lambda: self._sys_op("restore")).pack(side="left", padx=4)

        tip = ttk.Label(box, text="『卸载（仅当前用户）』可用『恢复系统应用』随时还原，不会删除 APK；"
                                  "冻结则完全可逆。误操作桌面/系统界面组件前请三思。",
                        style="Muted.TLabel", wraplength=560)
        tip.pack(anchor="w", pady=(6, 0))

    def load_sysapps(self) -> None:
        if self.root is None:
            self.app.log("请先连接设备。", "warn")
            return
        run_async(self.app, self.root.list_system_packages,
                  on_done=lambda pkgs: (setattr(self, "sysapps", pkgs),
                                        self.sys_combo.configure(values=pkgs),
                                        self.app.log(f"已加载 {len(pkgs)} 个系统应用。", "ok")),
                  busy_text="加载系统应用…")

    def _sys_op(self, op: str) -> None:
        pkg = self.sys_var.get().strip()
        if not pkg or self.root is None:
            messagebox.showinfo("提示", "请先选择系统应用包名。")
            return
        if op == "uninstall" and not self.app.confirm(
                title="确认卸载（仅当前用户）",
                message=f"将对系统应用 {pkg} 执行：pm uninstall -k --user 0\n"
                        "该操作仅对当前用户隐藏应用并清除其数据，可通过『恢复系统应用』还原。",
                risk="danger", confirm_label="确认卸载"):
            return
        if op == "freeze" and not self.app.confirm(
                title="确认冻结", message=f"将冻结 {pkg}。冻结后应用不再运行、桌面图标消失。",
                risk="warn", confirm_label="确认冻结"):
            return

        def work():
            return {
                "freeze": lambda: self.root.freeze(pkg, True),
                "unfreeze": lambda: self.root.freeze(pkg, False),
                "uninstall": lambda: self.root.uninstall_for_user(pkg),
                "restore": lambda: self.root.restore_system_app(pkg),
            }[op]()

        run_async(self.app, work,
                  on_done=lambda m: self.app.log(f"{op} {pkg} → {m}", "ok"),
                  busy_text=f"{op}中…")

    # ------------------------------------------------------------------ #
    # 聊天/系统数据导出
    # ------------------------------------------------------------------ #

    def _build_export(self, master) -> None:
        box = ttk.LabelFrame(master, text="  聊天与系统数据导出（root 直读数据库）  ", padding=8)
        box.pack(fill="both", expand=True)

        self.chat_progress = ProgressPanel(box)
        self.chat_progress.pack(fill="x", pady=(0, 6))

        ttk.Label(box, text="聊天应用数据（含数据库，tar.gz）").pack(anchor="w")
        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=2)
        for i, (label, pkg) in enumerate(CHAT_APPS):
            b = ttk.Button(bar, text=label, width=12,
                           command=lambda p=pkg: self.export_chat(p))
            b.grid(row=i // 3, column=i % 3, padx=2, pady=2)

        ttk.Separator(box).pack(fill="x", pady=8)
        ttk.Label(box, text="系统数据（直接读取 providers 数据库，绕过授权限制）").pack(anchor="w")
        bar2 = ttk.Frame(box)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="导出短信", command=lambda: self.export_sys("短信")).pack(side="left")
        ttk.Button(bar2, text="导出通话记录", command=lambda: self.export_sys("通话记录")).pack(side="left", padx=4)
        ttk.Button(bar2, text="导出通讯录", command=lambda: self.export_sys("通讯录")).pack(side="left")

        self.export_dir_var = tk.StringVar(value=self.app.cfg.get("export_dir"))
        bar3 = ttk.Frame(box)
        bar3.pack(fill="x", pady=(8, 0))
        ttk.Label(bar3, text="保存到").pack(side="left")
        ttk.Entry(bar3, textvariable=self.export_dir_var, width=36).pack(side="left", padx=4)
        ttk.Button(bar3, text="浏览", width=6, command=self._pick_export_dir).pack(side="left")

        tip = ttk.Label(box, text="提示：导出短信/通话/通讯录之前建议先在『应用管理』中通过常规通道导出（更规范）；"
                                  "本通道用于常规方式被厂商/系统拒绝时的兜底，数据直接来自系统数据库。",
                        style="Muted.TLabel", wraplength=560)
        tip.pack(anchor="w", pady=(6, 0))

    def _pick_export_dir(self) -> None:
        p = filedialog.askdirectory(title="选择导出目录")
        if p:
            self.export_dir_var.set(p)

    def export_chat(self, pkg: str) -> None:
        if self.root is None or not (self.app.device and self.app.device.rooted):
            self.app.log("导出聊天数据需要 root，当前设备未获取。", "warn")
            return
        dest = os.path.join(self.export_dir_var.get().strip(), "chatdata")
        self.chat_progress.reset(f"打包 {pkg} …")

        def work():
            return self.root.export_chat_data(
                pkg, dest,
                on_progress=lambda t: self.app.after_ui(
                    lambda: self.chat_progress.set(50, f"已传输 {t/1048576:.1f} MB")))

        def done(target):
            self.chat_progress.set(100, "导出完成")
            self.app.log(f"已导出 {pkg} 数据到 {target}", "ok")
            messagebox.showinfo("导出完成", f"{pkg} 数据已导出到：\n{target}\n\n"
                                            "内部数据为 tar.gz 压缩包，可直接解压查看（含数据库、文件）。")

        run_async(self.app, work, on_done=done, busy_text="导出聊天数据…")

    def export_sys(self, kind: str) -> None:
        if self.root is None or not (self.app.device and self.app.device.rooted):
            self.app.log("导出系统数据需要 root，当前设备未获取。", "warn")
            return
        dest = self.export_dir_var.get().strip()
        os.makedirs(dest, exist_ok=True)
        self.chat_progress.reset(f"导出{kind}…")

        def work():
            return {
                "短信": lambda: self.root.export_sms_root(dest),
                "通话记录": lambda: self.root.export_calllog_root(dest),
                "通讯录": lambda: self.root.export_contacts_root(dest),
            }[kind]()

        def done(path):
            self.chat_progress.set(100, "导出完成")
            self.app.log(f"{kind}已导出：{path}", "ok")
            messagebox.showinfo("导出完成", f"{kind}已保存为 CSV：\n{path}")

        run_async(self.app, work, on_done=done, busy_text=f"导出{kind}…")

    # ------------------------------------------------------------------ #
    # Root 文件浏览
    # ------------------------------------------------------------------ #

    def _build_browser(self, master) -> None:
        box = ttk.LabelFrame(master, text="  Root 文件浏览器  ", padding=8)
        box.pack(fill="both", expand=True)

        bar = ttk.Frame(box)
        bar.pack(fill="x")
        self.root_path_var = tk.StringVar(value="/data")
        e = ttk.Entry(bar, textvariable=self.root_path_var, width=40)
        e.pack(side="left")
        e.bind("<Return>", lambda _ev: self.root_browse())
        ttk.Button(bar, text="浏览", command=self.root_browse).pack(side="left", padx=4)
        ttk.Button(bar, text="上级", command=self.root_up).pack(side="left")
        ttk.Button(bar, text="导出目录(tar.gz)", style="Accent.TButton",
                   command=self.root_export_dir).pack(side="left", padx=6)
        ttk.Button(bar, text="导出选中文件", command=self.root_export_file).pack(side="left")

        wrap = ttk.Frame(box)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.root_tree = ttk.Treeview(wrap, columns=("perm", "size", "name"),
                                      show="headings", selectmode="extended")
        for c, head, w in (("perm", "权限", 110), ("size", "大小", 90), ("name", "名称", 320)):
            self.root_tree.heading(c, text=head)
            self.root_tree.column(c, width=w, anchor="w")
        self.root_tree.pack(side="left", fill="both", expand=True)
        y = ttk.Scrollbar(wrap, command=self.root_tree.yview)
        y.pack(side="right", fill="y")
        self.root_tree.configure(yscrollcommand=y.set)
        self.root_tree.bind("<Double-1>", self._root_double)

    def root_browse(self) -> None:
        if self.root is None:
            self.app.log("请先连接设备。", "warn")
            return
        path = self.root_path_var.get().strip() or "/data"
        self._root_current = path
        self.root_path_var.set(path)

        def work():
            return self.root.list_dir(path)

        def done(lines):
            self.root_entries = lines
            for item in self.root_tree.get_children():
                self.root_tree.delete(item)
            for ln in lines:
                parts = ln.split(None, 8)
                if len(parts) >= 9:
                    perm, _, _, _, size, _, _, _, name = parts
                    is_dir = perm.startswith("d")
                    self.root_tree.insert("", "end", values=(perm, size, name),
                                          tags=("dir",) if is_dir else ())
            self.root_tree.tag_configure("dir", foreground=COLOR["primary"])
            self.app.log(f"root 浏览 {path}，共 {len(lines)} 项", "ok")

        run_async(self.app, work, on_done=done, busy_text=f"读取 {path} …")

    def root_up(self) -> None:
        parent = os.path.dirname(self._root_current.rstrip("/"))
        self.root_path_var.set(parent or "/")
        self.root_browse()

    def _root_double(self, _ev) -> None:
        sel = self.root_tree.selection()
        if not sel:
            return
        name = self.root_tree.item(sel[0])["values"][2]
        if str(self.root_tree.item(sel[0])["values"][0]).startswith("d"):
            self.root_path_var.set(self._root_current.rstrip("/") + "/" + name)
            self.root_browse()

    def root_export_dir(self) -> None:
        if self.root is None:
            self.app.log("请先连接设备。", "warn")
            return
        dest_dir = filedialog.askdirectory(title="选择保存目录")
        if not dest_dir:
            return
        name = os.path.basename(self._root_current.rstrip("/")) or "root"
        local = os.path.join(dest_dir, f"{name}_{os.path.basename(self._root_current.rstrip('/'))}.tar.gz")
        self.chat_progress.reset("打包目录…")

        def work():
            st = self.root.stream_tar(
                self._root_current, local,
                on_progress=lambda b: self.app.after_ui(
                    lambda: self.chat_progress.set(min(99, b / 1048576 / 50), f"已传输 {b/1048576:.1f} MB")))
            if not st["ok"]:
                raise AdbError("目录打包失败", "目录可能为空或设备 tar 不支持。", "")
            return st["file"]

        def done(f):
            self.chat_progress.set(100, "导出完成")
            self.app.log(f"已导出：{f}", "ok")
            messagebox.showinfo("导出完成", f"已保存到：\n{f}")

        run_async(self.app, work, on_done=done, busy_text="导出目录…")

    def root_export_file(self) -> None:
        if self.root is None:
            self.app.log("请先连接设备。", "warn")
            return
        sel = self.root_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要导出的文件。")
            return
        dest_dir = filedialog.askdirectory(title="选择保存目录")
        if not dest_dir:
            return
        names = [self.root_tree.item(i)["values"][2] for i in sel]
        self.chat_progress.reset("下载文件…")

        def work():
            saved = []
            for i, name in enumerate(names):
                remote = self._root_current.rstrip("/") + "/" + name
                local = os.path.join(dest_dir, name)
                st = self.root.stream_file(remote, local)
                if not st["ok"]:
                    raise AdbError(f"下载失败：{name}", "文件可能不存在或已被移除。", "")
                saved.append(local)
                self.app.after_ui(lambda i=i: self.chat_progress.set((i + 1) / len(names) * 100,
                                                                     f"已下载 {i + 1}/{len(names)}"))
            return saved

        def done(files):
            self.chat_progress.set(100, "导出完成")
            self.app.log(f"已导出 {len(files)} 个文件到 {dest_dir}", "ok")
            messagebox.showinfo("导出完成", "\n".join(files))

        run_async(self.app, work, on_done=done, busy_text="下载文件…")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def on_device(self, device) -> None:
        if device is not None and device.online:
            self.root = RootManager(self.app.adb, device.serial)
            self.after(300, self.refresh_status)
        else:
            self.root = None
            self._set_status("未连接设备。")
