# -*- coding: utf-8 -*-
"""主窗口：设备栏 + 功能页签 + 状态栏。

页面（Panel）统一实现 `on_device(device)` 接口，在设备切换时收到通知，
从而各自决定是否启用控件 —— 避免"未连接设备却点了按钮"这类空指针问题。
"""

from __future__ import annotations

import os
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.adb import (AdbClient, AdbError, candidate_adb_paths, Device,
                      locate_adb, locate_scrcpy)
from core.config import Config
from core.inputctl import InputController
from ui.widgets import (COLOR, FONT, LogPanel, ProgressPanel, ask_confirm,
                        run_async, style_root, Tooltip)
from ui.panel_screen import ScreenPanel
from ui.panel_files import FilesPanel
from ui.panel_apps import AppsPanel
from ui.panel_ops import OpsPanel
from ui.panel_root import RootPanel


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = Config()
        # 保存过的 adb 路径若已失效（文件被删除/移动），自动回退到自动定位，
        # 避免用户被一份过期配置卡死。
        saved_adb = self.cfg.get("adb_path") or ""
        if saved_adb and not os.path.isfile(saved_adb):
            saved_adb = ""
        saved_scrcpy = self.cfg.get("scrcpy_path") or ""
        if saved_scrcpy and not os.path.isfile(saved_scrcpy):
            saved_scrcpy = ""
        self.adb = AdbClient(
            adb_path=saved_adb or locate_adb() or "",
            scrcpy_path=saved_scrcpy or locate_scrcpy() or "",
        )
        self.devices: list[Device] = []
        self.device: Device | None = None
        self.input: InputController | None = None
        self.input_q = None          # InputQueue：输入命令后台串行队列（不阻塞界面）
        self.panels: list = []
        self._busy_depth = 0

        self.title("安卓手机救援控制工具（ADB） v2.1")
        # 默认窗口尺寸按 DPI 缩放：高 DPI 屏上字体/控件按真实像素渲染，
        # 固定 1320x860 物理像素会显得过小、行内容放不下
        try:
            dpi_scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            dpi_scale = 1.0
        w, h = int(1320 * dpi_scale), int(860 * dpi_scale)
        w = min(w, self.winfo_screenwidth() - 40)
        h = min(h, self.winfo_screenheight() - 60)
        self.geometry(f"{w}x{h}")
        self.minsize(int(1120 * dpi_scale), int(720 * dpi_scale))
        style_root(self)

        self._build_device_bar()
        self._build_notebook()
        self._build_status_bar()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log("欢迎使用安卓手机救援控制工具。请先用 USB 数据线连接设备，并确认手机已开启『USB 调试』。", "ok")
        self.after(300, self._startup_check)

    # ------------------------------------------------------------------ #
    # 界面骨架
    # ------------------------------------------------------------------ #

    def _build_device_bar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")

        ttk.Label(bar, text="目标设备", style="Title.TLabel").pack(side="left")
        self.dev_var = tk.StringVar()
        self.dev_combo = ttk.Combobox(bar, textvariable=self.dev_var, width=42,
                                      state="readonly", values=[])
        self.dev_combo.pack(side="left", padx=8)
        self.dev_combo.bind("<<ComboboxSelected>>", lambda _e: self.select_device())

        ttk.Button(bar, text="刷新设备", command=self.refresh_devices).pack(side="left")
        ttk.Button(bar, text="重启 ADB 服务", command=self.restart_server).pack(side="left", padx=6)

        self.state_var = tk.StringVar(value="未连接")
        self.state_label = ttk.Label(bar, textvariable=self.state_var, style="Title.TLabel")
        self.state_label.pack(side="left", padx=14)

        ttk.Button(bar, text="设置", command=self.open_settings).pack(side="right")
        ttk.Button(bar, text="MTP 存储", command=self.open_mtp).pack(side="right", padx=6)
        ttk.Button(bar, text="没开USB调试?", command=self.show_no_debug_help).pack(side="right", padx=6)
        ttk.Button(bar, text="连接帮助", command=self.show_guide).pack(side="right", padx=6)
        ttk.Button(bar, text="导出目录", command=self.open_export_dir).pack(side="right", padx=6)

    def _build_notebook(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.notebook = nb

        self.screen_panel = ScreenPanel(nb, self)
        self.files_panel = FilesPanel(nb, self)
        self.apps_panel = AppsPanel(nb, self)
        self.ops_panel = OpsPanel(nb, self)
        self.root_panel = RootPanel(nb, self)

        nb.add(self.screen_panel, text="  投屏与控制  ")
        nb.add(self.files_panel, text="  数据导出  ")
        nb.add(self.apps_panel, text="  应用管理  ")
        nb.add(self.ops_panel, text="  运维工具  ")
        nb.add(self.root_panel, text="  Root 高级  ")

        log_frame = ttk.Frame(nb, padding=6)
        self.log_panel = LogPanel(log_frame, height=30, mono=False)
        self.log_panel.pack(fill="both", expand=True)
        nb.add(log_frame, text="  运行日志  ")

        self.panels = [self.screen_panel, self.files_panel, self.apps_panel,
                       self.ops_panel, self.root_panel]

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(bar, textvariable=self.status_var)
        self.status_label.pack(side="left")
        self.busy_var = tk.StringVar(value="")
        self.busy_label = ttk.Label(bar, textvariable=self.busy_var,
                                    foreground=COLOR["primary"])
        self.busy_label.pack(side="right", padx=10)
        self.progress = ProgressPanel(bar)
        self.progress.pack(side="right", fill="x", expand=False, ipadx=180)

    # ------------------------------------------------------------------ #
    # 通用能力（供各页面调用）
    # ------------------------------------------------------------------ #

    def log(self, message: str, level: str = "info") -> None:
        self.log_panel.write(message, level)

    def set_status(self, text: str, level: str = "info") -> None:
        self.status_var.set(text)
        color = {"info": COLOR["text"], "ok": COLOR["success"],
                 "warn": COLOR["warn"], "error": COLOR["danger"]}.get(level, COLOR["text"])
        self.status_label.configure(foreground=color)

    def set_busy(self, busy: bool, text: str = "执行中…") -> None:
        self._busy_depth = max(0, self._busy_depth + (1 if busy else -1))
        if self._busy_depth > 0:
            self.busy_var.set(text)
            self.configure(cursor="watch")
        else:
            self.busy_var.set("")
            self.configure(cursor="")
        self.update_idletasks()

    def confirm(self, *, title: str, message: str, details: str = "", risk: str = "danger",
                require_text: str | None = None, confirm_label: str = "确认执行") -> bool:
        """破坏性操作统一入口。设置中关闭确认时会跳过（风险自负）。"""
        if not self.cfg.get("confirm_destructive", True) and risk != "danger":
            return True
        return ask_confirm(self, title=title, message=message, details=details,
                           risk=risk, require_text=require_text, confirm_label=confirm_label)

    def ensure_device(self) -> Device:
        """返回当前可用设备，不可用时抛出中文错误。"""
        if self.device is None:
            raise AdbError("尚未选择设备",
                           "请连接手机后点击『刷新设备』，并在下拉框中选择目标设备。")
        if self.device.state == "unauthorized":
            raise AdbError(
                "设备未授权 USB 调试",
                "请在手机屏幕上确认『允许 USB 调试？』弹窗并勾选『一律允许』。"
                "若手机屏幕损坏无法点按：① 用 OTG 转接线接一个鼠标来点击弹窗；"
                "② 或换一台此前已授权过的电脑。确认后点击『刷新设备』。")
        if self.device.state != "device":
            raise AdbError(f"设备当前状态：{self.device.state_text}",
                           self.adb.explain(self.device.state))
        return self.device

    def after_ui(self, func) -> None:
        self.after(0, func)

    def open_export_dir(self) -> None:
        path = self.cfg.get("export_dir")
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)  # noqa: S606 - Windows 打开资源管理器
        except Exception:
            self.log(f"导出目录：{path}", "info")

    # ------------------------------------------------------------------ #
    # 设备管理
    # ------------------------------------------------------------------ #

    def _startup_check(self) -> None:
        if not os.path.isfile(self.adb.adb_path):
            found = locate_adb()
            if found:
                self.adb.adb_path = found
                self.cfg.set("adb_path", found)
                self.log(f"已自动定位 adb：{found}", "ok")
            else:
                self.log("未找到 adb，请在『设置』中手动指定路径。", "warn")
                if messagebox.askyesno(
                        "未找到 adb",
                        "没有检测到 adb 可执行文件。\n\n"
                        "本工具依赖 Android SDK platform-tools。你可以：\n"
                        "  1）下载 platform-tools 解压后在设置中指定 adb.exe；\n"
                        "  2）或现在直接手动选择 adb.exe 文件。\n\n"
                        "是否现在选择？"):
                    self.pick_adb()
        self.log(f"ADB 版本：{self.adb.version()}", "info")
        self.refresh_devices()

    def pick_adb(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 adb 可执行文件",
            filetypes=[("adb", "adb.exe;adb"), ("所有文件", "*.*")])
        if path:
            self.adb.adb_path = path
            self.cfg.set("adb_path", path)
            self.log(f"已设置 adb 路径：{path}", "ok")
            self.refresh_devices()

    def restart_server(self) -> None:
        def work():
            self.adb.kill_server()
            self.adb.start_server()
            return self.adb.version()

        def done(ver):
            self.log(f"ADB 服务已重启，版本 {ver}", "ok")
            self.refresh_devices()

        run_async(self, work, on_done=done, busy_text="重启 ADB 服务…")

    def refresh_devices(self) -> None:
        def work():
            self.adb.start_server()
            devices = self.adb.devices()
            for dev in devices:
                if dev.state == "device":
                    try:
                        info = self.adb.collect_info(dev.serial)
                        dev.__dict__.update(info.__dict__)
                    except AdbError:
                        pass
                    try:
                        self.adb.collect_root_info(dev)
                    except AdbError:
                        pass
                elif dev.state == "unauthorized":
                    dev.model = dev.model or "未知型号（未授权）"
            return devices

        def done(devices):
            self.devices = devices
            values = [d.display_name for d in devices]
            self.dev_combo["values"] = values
            if not devices:
                self.dev_var.set("")
                self.device = None
                self.input = None
                self.input_q = None
                self.state_var.set("未检测到设备")
                self.state_label.configure(foreground=COLOR["danger"])
                self.log("未检测到设备：请检查数据线、USB 调试开关与驱动程序。", "warn")
                self.log("提示：若手机从未开启『USB 调试』，工具无法接管；"
                         "可点顶部『没开USB调试?』了解 MTP 文件传输等替代方案。", "info")
                self._notify_panels(None)
            else:
                preferred = self.cfg.get("last_serial")
                target = next((d for d in devices if d.serial == preferred), None) or \
                    next((d for d in devices if d.state == "device"), None) or devices[0]
                self.dev_var.set(target.display_name)
                self.log(f"检测到 {len(devices)} 台设备：" +
                         "，".join(f"{d.display_name}[{d.state_text}]" for d in devices), "ok")
                self.select_device()  # 内部会通知各页面
                self._maybe_auto_backup(target)
                if not any(d.online for d in devices):
                    self.after(300, self.show_unauthorized_help)

        run_async(self, work, on_done=done, busy_text="检测设备中…")

    def select_device(self) -> None:
        name = self.dev_var.get()
        dev = next((d for d in self.devices if d.display_name == name), None)
        self.device = dev
        if dev is None:
            self.input = None
            self.input_q = None
            return
        self.cfg.set("last_serial", dev.serial)
        self.input = InputController(self.adb, dev.serial) if dev.online else None
        # 输入命令走后台串行队列：界面永不因 adb 子进程阻塞（显著降低操控卡顿感）
        if self.input is not None:
            from core.inputctl import InputQueue
            self.input_q = InputQueue(
                self.input,
                on_error=lambda exc: self.after_ui(lambda: self._input_error(exc)))
        else:
            self.input_q = None

        color = {"device": COLOR["success"], "unauthorized": COLOR["danger"],
                 "offline": COLOR["warn"]}.get(dev.state, COLOR["muted"])
        self.state_label.configure(foreground=color)
        root_badge = ""
        if dev.rooted and dev.root_method in ("magisk", "supersu", "adb-root"):
            root_badge = f" ｜ ROOT({dev.root_method})"
        self.state_var.set(f"● {dev.state_text}{root_badge}")
        self.set_status(f"当前设备：{dev.display_name}", "ok" if dev.online else "warn")
        self._notify_panels(dev)

    def _input_error(self, exc: Exception) -> None:
        """输入队列中的 adb 错误统一回显到日志（主线程）。"""
        from core.adb import AdbError
        if isinstance(exc, AdbError):
            self.log(f"✗ 输入失败：{exc.message}", "error")
            if exc.hint:
                self.log(f"  {exc.hint}", "warn")
        else:
            self.log(f"✗ 输入异常：{exc}", "error")

    def _maybe_auto_backup(self, dev) -> None:
        """设备在线时按配置触发自动备份（带冷却，避免每次刷新都跑）。"""
        try:
            if dev is None or not getattr(dev, "online", False):
                return
            if not self.cfg.get("auto_backup_enabled", False):
                return
            import time as _t
            last = self.cfg.get("auto_backup_last") or {}
            if not isinstance(last, dict):
                last = {}
            cooldown = max(0, int(self.cfg.get("auto_backup_cooldown", 30) or 0)) * 60
            prev = 0.0
            try:
                prev = float(last.get(dev.serial, 0) or 0)
            except (TypeError, ValueError):
                prev = 0.0
            if _t.time() - prev < cooldown:
                return
            last[dev.serial] = _t.time()
            self.cfg.set("auto_backup_last", last)
            self.log(f"设备上线，自动备份启动：{dev.display_name}", "info")
            self.files_panel.run_auto_backup(triggered=True)
        except Exception as exc:  # noqa: BLE001 - 触发失败不影响主流程
            self.log(f"自动备份触发异常：{exc}", "warn")

    def _notify_panels(self, device) -> None:
        """把设备变更通知到所有功能页；单个页面出错不影响整体。"""
        for panel in self.panels:
            try:
                panel.on_device(device)
            except Exception as exc:  # noqa: BLE001
                self.log(f"页面刷新异常：{exc}", "warn")

    # ------------------------------------------------------------------ #
    # 设置 / 帮助
    # ------------------------------------------------------------------ #

    def open_settings(self) -> None:
        win = tk.Toplevel(self)
        win.title("设置")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        win.configure(background=COLOR["bg"])

        row = 0

        def add_row(label: str, var: tk.StringVar, browse=None, width=62):
            nonlocal row
            ttk.Label(win, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=6)
            ttk.Entry(win, textvariable=var, width=width).grid(row=row, column=1, padx=6, pady=6)
            if browse:
                ttk.Button(win, text="浏览", width=8, command=browse).grid(row=row, column=2, padx=6)
            row += 1

        adb_var = tk.StringVar(value=self.adb.adb_path)
        scrcpy_var = tk.StringVar(value=self.adb.scrcpy_path or "")
        export_var = tk.StringVar(value=self.cfg.get("export_dir"))

        add_row("adb 路径", adb_var, lambda: _pick_file(adb_var, "选择 adb.exe"))
        add_row("scrcpy 路径（可选）", scrcpy_var, lambda: _pick_file(scrcpy_var, "选择 scrcpy.exe"))
        add_row("默认导出目录", export_var, lambda: _pick_dir(export_var))

        ttk.Label(win, text="投屏默认参数", style="Title.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 2))
        row += 1
        fps_var = tk.StringVar(value=str(self.cfg.get("fps", 6)))
        quality_var = tk.StringVar(value=self.cfg.get("quality", "标准"))
        width_var = tk.StringVar(value=str(self.cfg.get("max_width", 720)))
        add_row("帧率（1-15）", fps_var, width=12)
        add_row("画质档位", quality_var, width=12)
        add_row("画面最大宽度（像素，0=不限制）", width_var, width=12)

        confirm_var = tk.BooleanVar(value=bool(self.cfg.get("confirm_destructive", True)))
        ttk.Checkbutton(win, text="执行高风险操作时强制二次确认（强烈建议开启）",
                        variable=confirm_var).grid(row=row, column=0, columnspan=3,
                                                   sticky="w", padx=12, pady=10)

        def save():
            self.adb.adb_path = adb_var.get().strip()
            self.adb.scrcpy_path = scrcpy_var.get().strip() or None
            self.cfg.set("adb_path", self.adb.adb_path)
            self.cfg.set("scrcpy_path", scrcpy_var.get().strip())
            self.cfg.set("export_dir", export_var.get().strip())
            try:
                self.cfg.set("fps", max(1, min(15, int(fps_var.get()))))
                self.cfg.set("max_width", max(0, int(width_var.get())))
            except ValueError:
                pass
            self.cfg.set("quality", quality_var.get())
            self.cfg.set("confirm_destructive", confirm_var.get())
            self.log("设置已保存", "ok")
            win.destroy()
            self.refresh_devices()

        bar = ttk.Frame(win)
        bar.grid(row=row + 1, column=0, columnspan=3, pady=(6, 14))
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=12)
        ttk.Button(bar, text="保存", style="Accent.TButton", command=save).pack(side="right")
        win.update_idletasks()
        win.geometry(f"+{self.winfo_rootx() + 180}+{self.winfo_rooty() + 120}")

    #: 检测到设备但未授权时的处置指引（屏幕损坏场景专用）
    UNAUTH_GUIDE = (
        "电脑已经识别到手机，但手机还没有授权这台电脑进行 USB 调试。\n"
        "授权只能由手机端确认，这是 Android 的安全机制，任何工具都无法代替或绕过。\n"
        "\n"
        "请在手机上完成这一步：\n"
        "  1. 点亮手机屏幕，会出现『允许 USB 调试吗？』对话框。\n"
        "  2. 勾选『一律允许使用这台计算机』，然后点『允许 / 确定』。\n"
        "  3. 回到本工具点『刷新设备』，状态变为『● 已授权 · 可操控』即可投屏。\n"
        "\n"
        "如果屏幕碎裂看不清或点不准，可用以下几种办法：\n"
        "  A. OTG 转接线 + USB 鼠标：盲点『允许』按钮（通常在屏幕中下部）。\n"
        "  B. 外接显示器：若手机 Type-C 支持视频输出（DP Alt Mode / MHL），\n"
        "     用转接器接到显示器或电视上，就能看到画面再配合鼠标点击。\n"
        "  C. 无线调试（Android 11+ 且已开启过）：先用 OTG 鼠标在\n"
        "     『开发者选项 → 无线调试』里配对，再用 adb pair / connect 连接。\n"
        "\n"
        "注意：USB 调试授权是按「电脑」分别保存的，换一台电脑就要重新授权一次。"
    )

    def show_unauthorized_help(self) -> None:
        messagebox.showwarning("设备未授权 USB 调试", self.UNAUTH_GUIDE)

    def show_guide(self) -> None:
        guide = (
            "【连接与授权步骤】\n"
            "1. 手机端：设置 → 关于手机 → 连续点击『版本号』7 次，开启开发者选项。\n"
            "2. 返回设置 → 系统/更多设置 → 开发者选项 → 打开『USB 调试』。\n"
            "3. 用数据线连接电脑，手机端选择『传输文件（MTP）』模式。\n"
            "4. 手机弹出『允许 USB 调试？』时点『确定』，建议勾选『一律允许使用这台计算机』。\n"
            "5. 回到本工具点击『刷新设备』，状态应显示为『● 已授权 · 可操控』。\n\n"
            "【屏幕损坏导致无法点按授权弹窗】\n"
            "  · 方案 A：用 OTG 转接线接一个 USB 鼠标，盲点『允许』按钮（位置通常在屏幕中下部）。\n"
            "  · 方案 B：若手机支持，使用已授权过的同一台电脑重新连接（授权记录按电脑密钥保存）。\n"
            "  · 方案 C：先用同型号正常手机完成授权后，再换回故障机（部分 ROM 无效）。\n"
            "  · 本工具不提供任何绕过锁屏密码、图案、指纹的功能，也请仅在自己拥有\n"
            "    合法所有权的设备上使用。\n\n"
            "【未检测到设备的常见原因】\n"
            "  · 使用了只能充电的数据线（换一根支持数据传输的线）。\n"
            "  · Windows 缺少 USB 驱动（安装厂商驱动或通用 ADB 驱动）。\n"
            "  · USB 调试被关闭，或仅『充电』模式下连接。\n"
            "  · adb 版本过旧，无法识别新系统（升级 platform-tools）。\n"
        )
        win = tk.Toplevel(self)
        win.title("连接帮助")
        win.configure(background=COLOR["bg"])
        win.transient(self)
        text = tk.Text(win, wrap="word", width=76, height=26, font=(FONT, 10),
                       relief="solid", bd=1)
        text.pack(padx=14, pady=14, fill="both", expand=True)
        text.insert("1.0", guide)
        text.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 12))
        win.update_idletasks()
        win.geometry(f"+{self.winfo_rootx() + 200}+{self.winfo_rooty() + 80}")

    #: 没开 USB 调试 / 没 root / 屏幕损坏场景的处置指引
    NO_DEBUG_GUIDE = (
        "【手机没开 USB 调试、没 root、屏幕又坏了 —— 怎么办？】\n"
        "\n"
        "本工具的一切功能都依赖『USB 调试』（adb 通道）：没开 USB 调试时，\n"
        "adb 连设备都看不到，工具无法接管。这种情况请按下面的顺序尝试：\n"
        "\n"
        "① MTP 文件传输（最实用，无需 adb、无需 root）\n"
        "   直接用 USB 数据线连电脑，满足两个条件就能像 U 盘一样拷数据：\n"
        "   · 锁屏是『滑动锁 / 无密码』。若设了 PIN/图案/密码，MTP 会被\n"
        "     锁屏拦截（Android 10+ 必须先解锁才能看到文件）。\n"
        "   · 手机 USB 连接模式是『传输文件 (MTP)』。插线后电脑没反应，\n"
        "     说明模式停在『仅充电』——屏幕已坏无法切换的话，只能寄望\n"
        "     手机的出厂默认模式（部分机型默认就是 MTP）。\n"
        "   连上后点工具右上角『MTP 存储』，或直接打开 Windows 资源管理器，\n"
        "   在『此电脑』里找到手机图标，进入就能拷贝照片/视频/文档。\n"
        "\n"
        "② 云同步数据\n"
        "   若手机以前开过 Google 相册 / 厂商云 / 微信电脑版备份，\n"
        "   照片、通讯录等可能已在云端，用电脑登录直接取回。\n"
        "\n"
        "③ 换屏维修（保数据的正道）\n"
        "   维修店更换屏幕/总成后，数据原样保留；届时开启 USB 调试并授权\n"
        "   这台电脑，本工具即可完全接管（投屏、导出、root 全套）。\n"
        "\n"
        "④ 提醒\n"
        "   · 不要因屏幕坏了就恢复出厂 / 盲目刷机，那会永久删除数据。\n"
        "   · 无线调试需要看屏幕上的配对码，屏幕坏了不可行。\n"
        "   · 本工具不提供任何绕过锁屏密码的功能。"
    )

    def show_no_debug_help(self) -> None:
        win = tk.Toplevel(self)
        win.title("没开 USB 调试怎么办")
        win.configure(background=COLOR["bg"])
        win.transient(self)
        text = tk.Text(win, wrap="word", width=78, height=28, font=(FONT, 10),
                       relief="solid", bd=1)
        text.pack(padx=14, pady=14, fill="both", expand=True)
        text.insert("1.0", self.NO_DEBUG_GUIDE)
        text.configure(state="disabled")
        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=(0, 12))
        ttk.Button(bar, text="打开 MTP 存储（此电脑）", style="Accent.TButton",
                   command=lambda: (self.open_mtp(), win.destroy())).pack(side="right", padx=14)
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
        win.update_idletasks()
        win.geometry(f"+{self.winfo_rootx() + 180}+{self.winfo_rooty() + 80}")

    def open_mtp(self) -> None:
        """打开 Windows『此电脑』，MTP 设备会显示在可移动设备列表中（无需 adb/root）。"""
        try:
            os.startfile("shell:MyComputerFolder")  # noqa: S606
            self.log("已打开『此电脑』：若手机以 MTP 模式连接且已解锁，"
                     "会看到一个手机图标，双击进入即可拷贝文件。", "ok")
        except Exception:
            try:
                subprocess.Popen(["explorer.exe", "shell:MyComputerFolder"])
                self.log("已打开『此电脑』。", "ok")
            except Exception as exc:  # noqa: BLE001
                self.log(f"打开失败：{exc}", "error")

    def on_close(self) -> None:
        try:
            self.screen_panel.shutdown()
            self.ops_panel.shutdown()
        except Exception:
            pass
        self.destroy()


def _pick_file(var: tk.StringVar, title: str) -> None:
    path = filedialog.askopenfilename(title=title)
    if path:
        var.set(path)


def _pick_dir(var: tk.StringVar) -> None:
    path = filedialog.askdirectory(title="选择目录")
    if path:
        var.set(path)
