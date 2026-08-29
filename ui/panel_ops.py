# -*- coding: utf-8 -*-
"""运维工具页：截图、录屏、logcat、shell、快捷面板、系统信息。"""

from __future__ import annotations

import datetime as dt
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.adb import AdbError
from core.ops import LOG_LEVELS, LogcatReader, PowerOps, ScreenCapture
from ui.widgets import COLOR, FONT, FONT_MONO, LogPanel, run_async, Tooltip

DANGEROUS = {
    "重启": ("重启设备",
            "设备将立即重启。重启过程中会断开 ADB 连接，正在进行的导出/投屏会中断。",
            "请先保存未完成的导出任务。", "danger"),
    "Recovery": ("重启到 Recovery",
               "设备将进入 Recovery 模式。该模式下无法使用常规投屏与控制，"
               "需要手机端手动选择退出项。", "仅在需要刷机/清除缓存分区时使用。", "danger"),
    "Fastboot": ("重启到 Fastboot/Bootloader",
               "设备将进入 Fastboot 模式，ADB 控制将失效，需要改用 fastboot 命令。",
               "误入该模式后需长按电源键 10 秒左右才能重启回系统。", "danger"),
    "关机": ("关闭设备",
            "设备将关机。关机后必须长按电源键才能再次开机，"
            "若屏幕已损坏，请确认你能通过投屏再次唤醒它。",
            "关机后 ADB 连接断开，本工具将无法继续操控该设备。", "danger"),
}


class OpsPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.capture: ScreenCapture | None = None
        self.logcat: LogcatReader | None = None
        self.power: PowerOps | None = None

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=6)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=6)

        self._build_capture(left)
        self._build_quick(left)
        self._build_info(left)
        self._build_logcat(right)
        self._build_shell(right)

    # ------------------------------------------------------------------ #
    # 截图 / 录屏
    # ------------------------------------------------------------------ #

    def _build_capture(self, master) -> None:
        box = ttk.LabelFrame(master, text="  截图与录屏  ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        ttk.Button(box, text="立即截图并保存", style="Accent.TButton",
                   command=self.screenshot).pack(side="left")
        ttk.Button(box, text="打开保存目录", command=self.open_dir).pack(side="left", padx=6)

        ttk.Separator(box, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(box, text="时长").pack(side="left")
        self.rec_time = tk.StringVar(value="180")
        ttk.Spinbox(box, from_=5, to=1800, width=6, textvariable=self.rec_time).pack(side="left")
        ttk.Label(box, text="尺寸").pack(side="left", padx=(8, 0))
        self.rec_size = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.rec_size, width=11).pack(side="left", padx=2)
        ttk.Label(box, text="码率").pack(side="left", padx=(8, 0))
        self.rec_bitrate = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.rec_bitrate, width=8).pack(side="left", padx=2)
        self.btn_rec = ttk.Button(box, text="开始录制", command=self.toggle_record)
        self.btn_rec.pack(side="left", padx=8)
        ttk.Label(box, text="尺寸格式如 720x1280，留空为原生；码率如 4M",
                  style="Muted.TLabel").pack(side="left")

    def screenshot(self) -> None:
        if self.capture is None:
            self.app.log("请先连接设备。", "warn")
            return
        dest = os.path.join(self.app.cfg.get("export_dir"), "screenshots")
        os.makedirs(dest, exist_ok=True)
        path = os.path.join(dest, f"screen_{dt.datetime.now():%Y%m%d_%H%M%S}.png")

        def work():
            return self.capture.screenshot(path)

        def done(p):
            self.app.log(f"截图已保存：{p}", "ok")
            messagebox.showinfo("截图完成", f"已保存到：\n{p}")

        run_async(self.app, work, on_done=done, busy_text="截图中…")

    def toggle_record(self) -> None:
        if self.capture is None:
            self.app.log("请先连接设备。", "warn")
            return
        if self.capture.recording:
            dest = os.path.join(self.app.cfg.get("export_dir"), "records")
            os.makedirs(dest, exist_ok=True)
            path = os.path.join(dest, f"record_{dt.datetime.now():%Y%m%d_%H%M%S}.mp4")

            def work():
                return self.capture.stop_record(path)

            def done(p):
                self.btn_rec.configure(text="开始录制")
                self.app.log(f"录屏已保存：{p}", "ok")
                messagebox.showinfo("录屏完成", f"已保存到：\n{p}")

            run_async(self.app, work, on_done=done, busy_text="停止并保存录屏…")
        else:
            time_limit = int(self.rec_time.get() or 180)

            def work():
                self.capture.start_record(
                    time_limit=time_limit,
                    size=self.rec_size.get().strip(),
                    bit_rate=self.rec_bitrate.get().strip())
                return time_limit

            def done(t):
                self.btn_rec.configure(text="停止录制")
                self.app.log(f"录屏已开始，最长 {t} 秒（到时会自动停止）。", "ok")

            run_async(self.app, work, on_done=done, busy_text="启动录屏…")

    def open_dir(self) -> None:
        path = os.path.join(self.app.cfg.get("export_dir"))
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)  # noqa: S606
        except Exception:
            self.app.log(f"目录：{path}")

    # ------------------------------------------------------------------ #
    # 快捷面板
    # ------------------------------------------------------------------ #

    def _build_quick(self, master) -> None:
        box = ttk.LabelFrame(master, text="  快捷面板  ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        safe = [("唤醒屏幕", "唤醒"), ("锁屏", "锁屏"), ("展开通知栏", "通知栏"),
                ("快捷设置", "快捷设置"), ("最近任务", "多任务"), ("回到桌面", "Home")]
        for text, key in safe:
            ttk.Button(box, text=text, width=11,
                       command=lambda k=key: self._simple_key(k)).pack(side="left", padx=3, pady=2)

        ttk.Separator(box, orient="vertical").pack(side="left", fill="y", padx=8)
        for text in ("重启", "Recovery", "Fastboot", "关机"):
            ttk.Button(box, text=text, width=9, style="Danger.TButton",
                       command=lambda t=text: self._power(t)).pack(side="left", padx=3)

        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, text="屏幕亮度").pack(side="left")
        self.bright_var = tk.IntVar(value=128)
        ttk.Scale(bar, from_=0, to=255, variable=self.bright_var,
                  command=lambda _v: None).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(bar, text="应用亮度", width=10, command=self.apply_brightness).pack(side="left")
        Tooltip(bar, "拖动滑块后点击『应用亮度』，即可在屏幕损坏时调高亮度辅助外接显示。")

    def _simple_key(self, name: str) -> None:
        try:
            ctl = self.app.input
            if ctl is None:
                raise AdbError("设备不可操控", "请先选择已授权的设备。")
        except AdbError as exc:
            self.app.log(f"✗ {exc.message}｜{exc.hint}", "error")
            return
        q = self.app.input_q

        def work():
            if name == "通知栏":
                ctl.open_notifications()
            elif name == "快捷设置":
                ctl.open_quick_settings()
            elif name == "锁屏":
                ctl.lock_screen()
            elif name == "唤醒":
                return ctl.wake(extend_timeout=True, timeout_ms=600000)
            elif q is not None:
                # 普通按键走后台输入队列，界面不阻塞
                q.submit(ctl.key_by_name, name)
            return None

        def done(rep):
            if name == "唤醒":
                if rep is None:
                    self.app.log("✗ 唤醒失败（无返回）", "error")
                    return
                if rep["ok"]:
                    self.app.log("✓ 屏幕已点亮：" + "；".join(rep["steps"]), "ok")
                    if rep.get("locked"):
                        self.app.log("设备停在锁屏界面，请在投屏画面中自行输入密码/PIN。", "warn")
                else:
                    self.app.log(f"✗ 唤醒失败，屏幕仍为 {rep['after']} 状态。", "error")
                return
            if name not in ("通知栏", "快捷设置", "锁屏"):
                self.app.log(f"已发送：{name}", "ok")

        run_async(self.app, work, on_done=done, busy_text="执行中…")

    def _power(self, action: str) -> None:
        if self.power is None:
            self.app.log("请先连接设备。", "warn")
            return
        title, msg, note, risk = DANGEROUS[action]
        if not self.app.confirm(title=title, message=f"{msg}\n\n{note}", risk=risk,
                                confirm_label=f"确认{action}"):
            self.app.log(f"已取消：{action}", "warn")
            return

        def work():
            return {"重启": self.power.reboot,
                    "Recovery": self.power.reboot_recovery,
                    "Fastboot": self.power.reboot_bootloader,
                    "关机": self.power.shutdown}[action]()

        def done(result):
            self.app.log(f"{action} → {result}", "ok")
            messagebox.showinfo("完成", f"{result}\n\n设备将断开连接，请稍后点击『刷新设备』重新连接。")

        run_async(self.app, work, on_done=done, busy_text=f"执行{action}…")

    def apply_brightness(self) -> None:
        try:
            if self.app.input is None:
                raise AdbError("设备不可操控", "请先选择已授权的设备。")
            self.app.input.set_brightness(int(self.bright_var.get()))
            self.app.log(f"亮度已设为 {int(self.bright_var.get())}", "ok")
        except AdbError as exc:
            self.app.log(f"✗ {exc.message}", "error")

    # ------------------------------------------------------------------ #
    # 系统信息
    # ------------------------------------------------------------------ #

    def _build_info(self, master) -> None:
        box = ttk.LabelFrame(master, text="  系统信息  ", padding=8)
        box.pack(fill="both", expand=True)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Button(bar, text="电池", command=lambda: self._info("电池")).pack(side="left")
        ttk.Button(bar, text="存储", command=lambda: self._info("存储")).pack(side="left", padx=4)
        ttk.Button(bar, text="内存", command=lambda: self._info("内存")).pack(side="left")
        ttk.Button(bar, text="一键体检", style="Accent.TButton",
                   command=lambda: self._info("体检")).pack(side="left", padx=4)
        self.info_text = tk.Text(box, height=8, font=(FONT_MONO, 9), relief="solid", bd=1)
        self.info_text.pack(fill="both", expand=True, pady=(6, 0))

    def _info(self, kind: str) -> None:
        if self.power is None:
            self.app.log("请先连接设备。", "warn")
            return

        def work():
            if kind == "电池":
                return self.power.battery_info()
            if kind == "存储":
                return self.power.storage_info()
            if kind == "内存":
                return self.power.memory_info()
            return self.power.device_report()

        def done(data):
            self.info_text.configure(state="normal")
            self.info_text.delete("1.0", "end")
            for k, v in data.items():
                self.info_text.insert("end", f"{k:<12}：{v}\n")
            self.info_text.configure(state="disabled")
            self.app.log(f"{kind}信息已刷新", "ok")

        run_async(self.app, work, on_done=done, busy_text=f"读取{kind}信息…")

    # ------------------------------------------------------------------ #
    # logcat
    # ------------------------------------------------------------------ #

    def _build_logcat(self, master) -> None:
        box = ttk.LabelFrame(master, text="  实时日志 logcat  ", padding=8)
        box.pack(fill="both", expand=True, pady=(0, 8))

        bar = ttk.Frame(box)
        bar.pack(fill="x")
        self.btn_log = ttk.Button(bar, text="开始", style="Accent.TButton",
                                  command=self.toggle_logcat)
        self.btn_log.pack(side="left")
        ttk.Button(bar, text="清空缓冲区", command=self.clear_logcat).pack(side="left", padx=6)
        ttk.Button(bar, text="导出当前日志", command=self.dump_logcat).pack(side="left")

        bar2 = ttk.Frame(box)
        bar2.pack(fill="x", pady=(6, 2))
        ttk.Label(bar2, text="标签:级别").pack(side="left")
        self.filter_var = tk.StringVar(value=self.app.cfg.get("logcat_filter", ""))
        e = ttk.Entry(bar2, textvariable=self.filter_var, width=24)
        e.pack(side="left", padx=2)
        Tooltip(e, "日志过滤规则，格式如 ActivityManager:I MyApp:D *:S（*:S 表示屏蔽其余全部）。")

        ttk.Label(bar2, text="关键字").pack(side="left", padx=(10, 0))
        self.kw_var = tk.StringVar(value=self.app.cfg.get("logcat_keyword", ""))
        ttk.Entry(bar2, textvariable=self.kw_var, width=16).pack(side="left", padx=2)

        ttk.Label(bar2, text="级别").pack(side="left", padx=(10, 0))
        self.level_var = tk.StringVar(value="V")
        ttk.Combobox(bar2, textvariable=self.level_var, width=4, state="readonly",
                     values=LOG_LEVELS).pack(side="left")

        self.log_view = LogPanel(box, height=16, mono=True)
        self.log_view.pack(fill="both", expand=True, pady=(6, 0))

    def toggle_logcat(self) -> None:
        if self.logcat is None:
            self.app.log("请先连接设备。", "warn")
            return
        if self.logcat.running:
            self.logcat.stop()
            self.btn_log.configure(text="开始")
            self.app.log("logcat 已停止", "info")
            return
        spec = self.filter_var.get().strip()
        level = self.level_var.get().strip() or "V"
        if spec and "*:" not in spec:
            spec = f"{spec} *:S"
        elif not spec:
            spec = f"*:{level}"
        self.app.cfg.set("logcat_filter", self.filter_var.get())
        self.app.cfg.set("logcat_keyword", self.kw_var.get())
        self.logcat.on_line = self.log_view.write
        try:
            self.logcat.start(filter_spec=spec, keyword=self.kw_var.get().strip())
        except Exception as exc:  # noqa: BLE001
            self.app.log(f"✗ logcat 启动失败：{exc}", "error")
            return
        self.btn_log.configure(text="停止")
        self.app.log(f"logcat 已启动（过滤：{spec}）", "ok")

    def clear_logcat(self) -> None:
        if self.logcat:
            self.logcat.adb.raw(["-s", self.logcat.serial, "logcat", "-c"], timeout=20)
        self.log_view.clear()
        self.app.log("日志缓冲区已清空", "info")

    def dump_logcat(self) -> None:
        if self.logcat is None:
            self.app.log("请先连接设备。", "warn")
            return
        dest = os.path.join(self.app.cfg.get("export_dir"), "logs")
        os.makedirs(dest, exist_ok=True)
        path = os.path.join(dest, f"logcat_{dt.datetime.now():%Y%m%d_%H%M%S}.txt")

        def work():
            return self.logcat.dump(path)

        def done(p):
            self.app.log(f"日志已导出：{p}", "ok")
            messagebox.showinfo("导出完成", f"已保存到：\n{p}")

        run_async(self.app, work, on_done=done, busy_text="导出日志…")

    # ------------------------------------------------------------------ #
    # Shell
    # ------------------------------------------------------------------ #

    def _build_shell(self, master) -> None:
        box = ttk.LabelFrame(master, text="  执行 Shell 命令  ", padding=8)
        box.pack(fill="both", expand=False)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        self.cmd_var = tk.StringVar(value="")
        e = ttk.Entry(bar, textvariable=self.cmd_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda _ev: self.run_cmd())
        ttk.Button(bar, text="执行", style="Accent.TButton", command=self.run_cmd).pack(side="left", padx=6)
        ttk.Button(bar, text="清空输出", command=lambda: self._set_out("")).pack(side="left")
        self.root_shell = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="以 root 执行", variable=self.root_shell).pack(side="left", padx=8)
        Tooltip(bar, "勾选后命令通过 su 以 root 身份执行（需设备已 root 并授权 Shell）。")

        self.out = tk.Text(box, height=9, font=(FONT_MONO, 9), relief="solid", bd=1)
        self.out.pack(fill="both", expand=True, pady=(6, 0))
        examples = ("常用示例：getprop ro.product.model ｜ dumpsys battery ｜ pm list packages -3 ｜ "
                    "settings put system screen_brightness 255 ｜ input keyevent 26")
        ttk.Label(box, text=examples, style="Muted.TLabel", wraplength=560).pack(anchor="w", pady=(4, 0))

    def run_cmd(self) -> None:
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        dev = self.app.device
        if dev is None or not dev.online:
            self.app.log("请先连接设备。", "warn")
            return

        def work():
            if self.root_shell.get():
                from core.root import RootManager
                rm = RootManager(self.app.adb, dev.serial)
                return rm.shell(cmd, timeout=120)
            res = self.app.adb.shell(dev.serial, cmd, timeout=120)
            return (res.stdout or "") + (("\n[stderr] " + res.stderr) if res.stderr else "")

        def done(text):
            self._set_out(text or "（无输出）")
            self.app.log(f"$ {cmd}", "info")

        run_async(self.app, work, on_done=done, busy_text="执行命令…")

    def _set_out(self, text: str) -> None:
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", text)
        self.out.configure(state="disabled")

    # ------------------------------------------------------------------ #

    def on_device(self, device) -> None:
        ok = device is not None and device.online
        self.capture = ScreenCapture(self.app.adb, device.serial) if ok else None
        self.power = PowerOps(self.app.adb, device.serial) if ok else None
        self.logcat = LogcatReader(self.app.adb, device.serial) if ok else None
        if ok and device:
            self._info("体检")

    def shutdown(self) -> None:
        if self.logcat:
            self.logcat.stop()
