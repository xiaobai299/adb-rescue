# -*- coding: utf-8 -*-
"""投屏与控制页：内置帧引擎渲染、鼠标手势映射、按键与文本输入。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from core.adb import AdbError
from core.inputctl import KeepAwake
from core.screen import QUALITY_PRESETS, ScreenStreamer, ScrcpyLauncher
from ui.widgets import COLOR, FONT, run_async, Tooltip

LONG_PRESS_MS = 800
DRAG_THRESHOLD = 8


class ScreenPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.streamer: ScreenStreamer | None = None
        self.scrcpy = ScrcpyLauncher(app.adb)
        self.photo = None
        self._disp_scale = 1.0
        self._offset = (0.0, 0.0)
        self._drag = None
        self.keeper: KeepAwake | None = None
        self._wake_state_job = None
        self._line = None
        self._fullscreen = False

        self._build_toolbar()
        self._build_scrcpy_bar()
        self._build_builtin_bar()
        self._build_wake_bar()
        self._build_canvas()
        self._build_keys()
        self._build_input()
        self._schedule_wake_state()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #

    def _build_toolbar(self) -> None:
        """主控制条：开始/停止投屏、引擎选择、唤醒入口、全屏。

        每页只保留一个蓝色主按钮（开始投屏）；它按下方所选引擎启动，
        scrcpy 缺失时自动回退内置引擎，保证救援场景下按钮永远可用。
        """
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")

        self.btn_start = ttk.Button(bar, text="开始投屏", style="Accent.TButton",
                                    command=self.start_mirror)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止投屏", command=self.stop_mirror,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        Tooltip(self.btn_start,
                "用下方所选引擎开始投屏。\n"
                "默认 scrcpy 高帧率引擎（推荐，自动套用稳妥参数 720p/60 帧/8M）；\n"
                "未找到 scrcpy 时自动回退到内置引擎。")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Label(bar, text="引擎").pack(side="left")
        self.engine_var = tk.StringVar(value="scrcpy 高帧率（推荐）")
        eng = ttk.Combobox(bar, textvariable=self.engine_var, width=20, state="readonly",
                           values=["scrcpy 高帧率（推荐）", "内置引擎（3~10 FPS 兜底）"])
        eng.pack(side="left", padx=(2, 8))
        Tooltip(eng, "scrcpy：设备端硬件编码，可跑满屏幕刷新率，救援首选。\n"
                     "内置引擎：adb 截帧兜底，无需 scrcpy，实测只有 3~10 FPS。")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.btn_wake = ttk.Button(bar, text="唤醒屏幕",
                                   command=self.do_wake, state="disabled")
        self.btn_wake.pack(side="left")
        self.btn_wake_bright = ttk.Button(bar, text="唤醒并调高亮度",
                                          command=lambda: self.do_wake(brighten=True),
                                          state="disabled")
        self.btn_wake_bright.pack(side="left", padx=(4, 0))
        Tooltip(self.btn_wake_bright,
                "点亮屏幕并临时调高亮度（恢复亮度需在手机上操作或重启后自动还原）。")

        self.btn_unlock = ttk.Button(bar, text="一键解锁",
                                     command=self.do_unlock, state="disabled")
        self.btn_unlock.pack(side="left", padx=(4, 0))
        Tooltip(self.btn_unlock,
                "用下方『键盘输入』框里的内容作为锁屏密码，一键完成：\n"
                "唤醒 → 唤出密码面板 → 输入 → 回车提交 → 验证。\n"
                "仅限输入你自己的已知密码；本工具不提供任何绕过锁屏的功能。")

        ttk.Button(bar, text="全屏 (F11)", command=self.toggle_fullscreen).pack(side="right")

    def _build_wake_bar(self) -> None:
        """自动保活与屏幕状态；唤醒按钮在上方主控制条。"""
        box = ttk.LabelFrame(self, text="  保活与屏幕状态（救援第一步：先点亮屏幕）  ", padding=6)
        box.pack(fill="x", padx=8, pady=(0, 4))

        self.keep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="自动保活（息屏后自动重新点亮）",
                        variable=self.keep_var, command=self.toggle_keep_awake).pack(side="left")

        ttk.Label(box, text="间隔").pack(side="left")
        self.keep_interval = tk.StringVar(value="15")
        ttk.Spinbox(box, from_=5, to=120, width=4,
                    textvariable=self.keep_interval).pack(side="left", padx=(2, 8))

        self.state_lbl = ttk.Label(box, text="状态：未知", style="Title.TLabel")
        self.state_lbl.pack(side="right", padx=6)

    # ---------------- 唤醒 ---------------- #

    def do_wake(self, brighten: bool = False, silent: bool = False) -> None:
        try:
            ctl = self._require_input()
        except AdbError as exc:
            if not silent:
                self.app.log(f"✗ {exc.message}", "error")
            return

        def work():
            return ctl.wake(extend_timeout=True, timeout_ms=600000,
                            brighten=brighten)

        def done(rep):
            locked = rep.get("locked")
            if rep["ok"]:
                self.app.log("✓ 屏幕已点亮：" + "；".join(rep["steps"]), "ok")
                if locked and not silent:
                    self.app.log("提示：设备停在锁屏界面，请在投屏画面中自行输入密码/PIN"
                                 "（本工具不提供任何绕过锁屏的功能）。", "warn")
                    messagebox.showinfo(
                        "已唤醒，但需要解锁",
                        "屏幕已点亮，但设备当前停在锁屏界面。\n\n"
                        "请在投屏窗口中用鼠标输入你的密码 / 图案 / PIN 完成解锁。\n"
                        "出于安全考虑，本工具不提供绕过锁屏的功能。")
            elif not silent:
                self.app.log(f"✗ 唤醒失败：屏幕仍为 {rep['after']} 状态。"
                             f"请确认数据线连接正常、手机有电。", "error")
            self.refresh_wake_state()

        run_async(self.app, work, on_done=done, busy_text="唤醒屏幕…")

    def do_unlock(self) -> None:
        """一键解锁：用『键盘输入』框内容作为锁屏密码（机主自行输入已知密码）。"""
        password = self.text_var.get()
        if not password:
            self.app.log("请先在下方『键盘输入』框中输入锁屏密码，再点『一键解锁』。", "warn")
            return
        try:
            ctl = self._require_input()
        except AdbError as exc:
            self.app.log(f"✗ {exc.message}", "error")
            if exc.hint:
                self.app.log(f"  {exc.hint}", "warn")
            return

        def work():
            return ctl.unlock_with_password(password)

        def done(rep):
            for step in rep["steps"]:
                self.app.log(f"· {step}", "info")
            if rep["locked_after"] is False:
                self.app.log("✓ 解锁成功", "ok")
                self.hint_var.set("设备已解锁")
                self.text_var.set("")  # 密码用完即清，不留存
            elif rep["locked_after"] is None:
                self.app.log("? 无法读取锁屏状态。请投屏确认画面。", "warn")
                self.refresh_wake_state()
            else:
                self.app.log("✗ 仍在锁屏。若密码确认无误：可能是系统因多次失败"
                             "进入冷却倒计时，稍等后再试；或先投屏查看当前画面。", "error")
                self.refresh_wake_state()

        run_async(self.app, work, on_done=done, busy_text="解锁中…")

    def toggle_keep_awake(self) -> None:
        if self.keep_var.get():
            if self.app.input is None:
                self.keep_var.set(False)
                self.app.log("请先选择已授权的设备。", "warn")
                return
            self.keeper = KeepAwake(self.app.input, interval=float(self.keep_interval.get() or 15))
            self.keeper.on_event = lambda t, lvl="info": self.app.after_ui(
                lambda: self.app.log(t, lvl))
            self.keeper.start()
            self.app.log(f"自动保活已开启：每 {self.keep_interval.get()} 秒检测一次屏幕状态。", "ok")
        else:
            if self.keeper:
                self.keeper.stop()
                self.app.log(f"自动保活已停止，期间共自动点亮 {self.keeper.wake_count} 次。", "info")
                self.keeper = None

    def refresh_wake_state(self) -> None:
        """刷新屏幕状态标签（每 3 秒一次，仅在有设备时查询）。"""
        ctl = self.app.input
        if ctl is None:
            self.state_lbl.configure(text="状态：未连接设备", foreground=COLOR["muted"])
            return

        def work():
            return {"state": ctl.screen_state(), "locked": ctl.is_locked(),
                    "timeout": ctl.get_screen_timeout()}

        def done(info):
            state = info["state"]
            color = COLOR["success"] if state == "Awake" else COLOR["danger"]
            text = "已点亮" if state == "Awake" else ("已熄灭" if state == "Asleep" else state)
            extra = ""
            if info["locked"]:
                extra = " ｜ 停在锁屏"
            if info["timeout"]:
                extra += f" ｜ {info['timeout'] // 1000} 秒后自动熄屏"
            self.state_lbl.configure(text=f"状态：● {text}{extra}", foreground=color)

        def fail(exc):
            self.state_lbl.configure(text="状态：读取失败", foreground=COLOR["warn"])

        run_async(self.app, work, on_done=done, on_error=fail)

    def _schedule_wake_state(self) -> None:
        if self.app.input is not None:
            self.refresh_wake_state()
        self._wake_state_job = self.after(3000, self._schedule_wake_state)

    def _build_scrcpy_bar(self) -> None:
        """高帧率投屏（scrcpy）参数区。

        scrcpy 通过设备端 MediaCodec 硬件编码传输 H.264/H.265 视频流，
        可以跑到屏幕原生刷新率（你的 120Hz 屏可跑满 120 帧）。
        启动/停止统一由主控制条的『开始投屏 / 停止投屏』负责。
        """
        box = ttk.LabelFrame(self, text="  scrcpy 高帧率引擎参数（推荐）  ",
                             padding=6)
        box.pack(fill="x", padx=8, pady=(0, 4))

        # 每行独立 Frame：同一父容器里混用 side="left"/"top" 会让 packer
        # 把后打包的行塞进右侧空腔（旧版此行因此被裁在窗口外）
        row1 = ttk.Frame(box)
        row1.pack(fill="x")

        ttk.Label(row1, text="帧率").pack(side="left")
        self.sc_fps_var = tk.StringVar(value=self._fps_label(self.app.cfg.get("scrcpy_fps", 0)))
        fps_cb = ttk.Combobox(row1, textvariable=self.sc_fps_var, width=14, state="readonly",
                              values=list(ScrcpyLauncher.FPS_PRESETS.keys()))
        fps_cb.pack(side="left", padx=(2, 6))
        Tooltip(fps_cb, "选『不限（跟随设备）』时不限制采集帧率，120Hz 屏幕可跑到 120 帧；\n"
                        "画面卡顿时降到 60 更稳。")

        ttk.Label(row1, text="分辨率").pack(side="left")
        self.sc_size_var = tk.StringVar(value=self._size_label(self.app.cfg.get("scrcpy_size", 0)))
        size_cb = ttk.Combobox(row1, textvariable=self.sc_size_var, width=10, state="readonly",
                               values=["原生 1080", "1920", "1440", "1080", "720"])
        size_cb.pack(side="left", padx=(2, 6))

        ttk.Label(row1, text="码率").pack(side="left")
        self.sc_bitrate_var = tk.StringVar(value=self.app.cfg.get("scrcpy_bitrate", "8M"))
        br_cb = ttk.Combobox(row1, textvariable=self.sc_bitrate_var, width=6, state="readonly",
                             values=["4M", "8M", "12M", "16M", "24M"])
        br_cb.pack(side="left", padx=(2, 6))
        Tooltip(br_cb, "帧率越高需要的码率越大。120 帧建议 12M 以上；USB 2.0 口上限约 25~30M。")

        ttk.Label(row1, text="编码").pack(side="left")
        self.sc_codec_var = tk.StringVar(value=self.app.cfg.get("scrcpy_codec", "h264"))
        ttk.Combobox(row1, textvariable=self.sc_codec_var, width=6, state="readonly",
                     values=["h264", "h265", "av1"]).pack(side="left", padx=(2, 6))

        self.sc_control_var = tk.BooleanVar(value=not self.app.cfg.get("scrcpy_no_control", True))
        ttk.Checkbutton(row1, text="scrcpy 窗口直接控制（延迟更低）",
                        variable=self.sc_control_var).pack(side="left", padx=(6, 2))
        self.sc_off_var = tk.BooleanVar(value=bool(self.app.cfg.get("scrcpy_turn_off", False)))
        ttk.Checkbutton(row1, text="关闭手机屏幕", variable=self.sc_off_var).pack(side="left")

        bar2 = ttk.Frame(box)
        bar2.pack(fill="x", pady=(6, 0))
        self.sc_latency_var = tk.BooleanVar(value=bool(self.app.cfg.get("scrcpy_low_latency", False)))
        ttk.Checkbutton(bar2, text="低延迟模式（0 显示缓冲）",
                        variable=self.sc_latency_var).pack(side="left")
        self.sc_tcpip_var = tk.BooleanVar(value=bool(self.app.cfg.get("scrcpy_tcpip", False)))
        ttk.Checkbutton(bar2, text="无线 TCP/IP 连接",
                        variable=self.sc_tcpip_var).pack(side="left", padx=(10, 2))
        self.sc_audio_var = tk.BooleanVar(value=bool(self.app.cfg.get("scrcpy_audio", False)))
        ttk.Checkbutton(bar2, text="同步手机音频",
                        variable=self.sc_audio_var).pack(side="left")
        Tooltip(bar2, "低延迟：把视频显示缓冲压到 0ms 并启用 baseline 编码。\n"
                      "注意：0 缓冲会取消抖动补偿，稍有波动画面反而一顿一顿，默认请保持关闭。\n"
                      "无线 TCP/IP：在 scrcpy 4.x 中会把设备重连到 Wi-Fi TCP/IP，\n"
                      "仅适合无线连接场景；USB 直连请保持关闭（无线比 USB 慢）。\n"
                      "音频：把手机声音同步到电脑（需 scrcpy 窗口直接控制）。")

        self.sc_info_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.sc_info_var, style="Muted.TLabel").pack(
            anchor="w", pady=(4, 0))

        self.after(500, self._refresh_scrcpy_state)

    def _build_builtin_bar(self) -> None:
        """内置帧引擎参数（兜底：无 scrcpy 或低配环境使用）。"""
        box = ttk.LabelFrame(self, text="  内置引擎参数（adb 截帧兜底，3~10 FPS）  ", padding=6)
        box.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Label(box, text="画质").pack(side="left")
        self.quality_var = tk.StringVar(value=self.app.cfg.get("quality", "标准"))
        q = ttk.Combobox(box, textvariable=self.quality_var, width=12, state="readonly",
                         values=list(QUALITY_PRESETS.keys()))
        q.pack(side="left", padx=(2, 8))
        q.bind("<<ComboboxSelected>>", self._apply_settings)
        Tooltip(q, "画面越清晰，单帧数据量越大、帧率越低。救援场景建议先用『标准』。")

        ttk.Label(box, text="帧率").pack(side="left")
        self.fps_var = tk.StringVar(value=str(self.app.cfg.get("fps", 6)))
        fps = ttk.Spinbox(box, from_=1, to=30, width=4, textvariable=self.fps_var,
                          command=self._apply_settings)
        fps.pack(side="left", padx=(2, 8))
        Tooltip(fps, "内置引擎受 adb 单次截图耗时限制，实测通常只有 3~10 FPS；\n"
                     "把这里调高也不会超过设备的实际出图速度。\n"
                     "需要 60/120 帧请选择『scrcpy 高帧率』引擎。")

        ttk.Label(box, text="旋转").pack(side="left")
        self.rot_var = tk.StringVar(value=str(self.app.cfg.get("rotation", 0)))
        rot = ttk.Combobox(box, textvariable=self.rot_var, width=6, state="readonly",
                           values=["0", "90", "180", "270"])
        rot.pack(side="left", padx=(2, 8))
        rot.bind("<<ComboboxSelected>>", self._apply_settings)

        ttk.Label(box, text="最大宽度").pack(side="left")
        self.width_var = tk.StringVar(value=str(self.app.cfg.get("max_width", 720)))
        w = ttk.Spinbox(box, from_=0, to=2560, increment=80, width=6,
                        textvariable=self.width_var, command=self._apply_settings)
        w.pack(side="left", padx=(2, 8))
        Tooltip(w, "限制传输画面的宽度可显著提速，0 表示按原始分辨率。")

        ttk.Separator(box, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Label(box, text="显示缩放").pack(side="left")
        self.zoom_var = tk.StringVar(value="适应窗口")
        z = ttk.Combobox(box, textvariable=self.zoom_var, width=10, state="readonly",
                         values=["适应窗口", "50%", "75%", "100%", "150%"])
        z.pack(side="left", padx=(2, 8))
        z.bind("<<ComboboxSelected>>", lambda _e: self._render())

    @staticmethod
    def _fps_label(value: int) -> str:
        for name, v in ScrcpyLauncher.FPS_PRESETS.items():
            if v == int(value or 0):
                return name
        return "不限（跟随设备）"

    @staticmethod
    def _size_label(value: int) -> str:
        return "原生 1080" if not int(value or 0) else str(int(value))

    def _refresh_scrcpy_state(self) -> None:
        if self.scrcpy is None or self.scrcpy.proc is None:
            self.scrcpy = ScrcpyLauncher(self.app.adb)
            self.scrcpy.on_exit = lambda: self.app.after_ui(self._scrcpy_exited)
        if self.scrcpy.available():
            # 版本输出自带官网 URL（如 "scrcpy 4.1 <https://...>"），对用户是噪音
            ver = self.scrcpy.version().split(" <")[0].strip()
            self.sc_info_var.set(
                f"已就绪：{ver} ｜ 启动后自动点亮并保持屏幕常亮"
                f"（手机息屏时帧率会掉到个位数）")
        else:
            self.sc_info_var.set(
                "未找到 scrcpy：点『开始投屏』将自动改用内置引擎（3~10 FPS）；"
                "也可在『设置』中指定 scrcpy.exe 路径。")

    def _build_canvas(self) -> None:
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8)
        # 默认高度取小值：投屏页固定控件较多，画布靠 expand 撑满剩余空间，
        # 默认窗口（860 高）下才不会把下方按键/输入区挤出窗外
        self.canvas = tk.Canvas(wrap, bg="#101216", height=180, highlightthickness=1,
                                highlightbackground=COLOR["border"])
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._render())

        # 鼠标手势
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda _e: self._key("返回"))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._swipe_dir("下"))
        self.canvas.bind("<Button-5>", lambda e: self._swipe_dir("上"))
        self.canvas.bind("<KeyPress>", self._on_key_press)
        self.canvas.configure(takefocus=1)

        info = ttk.Frame(self, padding=(8, 2))
        info.pack(fill="x")
        self.info_var = tk.StringVar(value="未开始投屏")
        ttk.Label(info, textvariable=self.info_var, style="Muted.TLabel").pack(side="left")
        self.hint_var = tk.StringVar(value="操作提示：左键=点击，快速拖动=滑动，慢速拖动=画手势（图案解锁），长按 0.8 秒=长按，右键=返回，滚轮=滚动页面")
        ttk.Label(info, textvariable=self.hint_var, style="Muted.TLabel").pack(side="right")

    def _build_keys(self) -> None:
        box = ttk.LabelFrame(self, text="  按键与手势  ", padding=6)
        box.pack(fill="x", padx=8, pady=(4, 2))

        # 分两行排布，避免窄窗口（最小宽 1120）时按钮被裁掉
        keys_top = ["返回", "Home", "多任务", "唤醒", "电源", "音量+", "音量-",
                    "通知栏", "快捷设置"]
        keys_bottom = ["删除", "回车", "截图(系统)"]
        for idx, name in enumerate(keys_top):
            ttk.Button(box, text=name, width=9,
                       command=lambda n=name: self._key(n)).grid(row=0, column=idx,
                                                                 padx=2, pady=2)
        for idx, name in enumerate(keys_bottom):
            ttk.Button(box, text=name, width=9,
                       command=lambda n=name: self._key(n)).grid(row=1, column=idx,
                                                                 padx=2, pady=2)

        ttk.Separator(box, orient="vertical").grid(row=0, column=len(keys_top),
                                                   rowspan=2, sticky="ns", padx=6)

        gestures = [("上滑", "上"), ("下滑", "下"), ("左滑", "左"), ("右滑", "右")]
        for i, (label, d) in enumerate(gestures):
            ttk.Button(box, text=label, width=7,
                       command=lambda dd=d: self._swipe_dir(dd)).grid(
                row=0, column=len(keys_top) + 1 + i, padx=2, pady=2)

        ttk.Button(box, text="长按电源菜单", width=12,
                   command=self._long_power).grid(row=1, column=len(keys_top) + 1,
                                                  columnspan=2, padx=4, pady=2,
                                                  sticky="ew")

    def _build_input(self) -> None:
        box = ttk.LabelFrame(self, text="  键盘输入  ", padding=6)
        box.pack(fill="x", padx=8, pady=(2, 8))

        self.text_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.text_var)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.bind("<Return>", lambda _e: self._send_text())
        ttk.Button(box, text="发送到手机", style="Accent.TButton",
                   command=self._send_text).pack(side="left")
        ttk.Button(box, text="清空输入框", command=self._clear_field).pack(side="left", padx=6)
        ttk.Button(box, text="复制到剪贴板", command=self._clipboard).pack(side="left")

        self.chan_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.chan_var, style="Muted.TLabel").pack(side="left", padx=10)
        self.passthrough = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="画布键盘直通（点画面后直接打字）",
                        variable=self.passthrough).pack(side="right")
        Tooltip(entry, "英文/数字可直接发送；中文需要手机已安装并启用 ADBKeyboard 输入法。"
                       "未安装时工具会给出明确提示，不会静默失败。")

        self.bind_all("<F11>", lambda _e: self.toggle_fullscreen())
        self.bind_all("<Escape>", lambda _e: self._exit_fullscreen())

    # ------------------------------------------------------------------ #
    # 投屏引擎
    # ------------------------------------------------------------------ #

    def on_device(self, device) -> None:
        usable = device is not None and device.online
        self.btn_start.configure(state="normal" if usable else "disabled")
        self.btn_wake.configure(state="normal" if usable else "disabled")
        self.btn_wake_bright.configure(state="normal" if usable else "disabled")
        self.btn_unlock.configure(state="normal" if usable else "disabled")
        if not usable:
            self.stop()
            if self.scrcpy and self.scrcpy.proc and self.scrcpy.proc.poll() is None:
                self.stop_scrcpy()
            if self.keeper:
                self.keep_var.set(False)
                self.toggle_keep_awake()
            self.state_lbl.configure(text="状态：未连接设备", foreground=COLOR["muted"])
        else:
            self.after(600, self.refresh_wake_state)

    def _apply_settings(self) -> None:
        if self.streamer:
            self.streamer.fps = self._clamp_fps()
            self.streamer.quality_name = self.quality_var.get()
            self.streamer.rotation = int(self.rot_var.get() or 0)
            self.streamer.max_width = int(self.width_var.get() or 0)
        self.app.cfg.set("fps", self._clamp_fps())
        self.app.cfg.set("quality", self.quality_var.get())
        self.app.cfg.set("rotation", int(self.rot_var.get() or 0))
        self.app.cfg.set("max_width", int(self.width_var.get() or 0))
        self._render()

    @staticmethod
    def _clamp_fps(default: int = 8) -> int:
        """内置引擎帧率上限 30；再高也没有意义，单次截图耗时才是瓶颈。"""
        return max(1, min(30, default))

    def start_mirror(self) -> None:
        """主控制条『开始投屏』：按所选引擎启动。

        scrcpy 引擎自动套用与『直接双击 scrcpy.exe』一致的稳妥参数；
        未找到 scrcpy 时自动回退内置引擎，保证按钮在救援场景永远可用。
        """
        if self.engine_var.get().startswith("scrcpy"):
            if self.scrcpy.available():
                self.launch_scrcpy_smooth()
            else:
                self.app.log("未找到 scrcpy，已改用内置引擎（3~10 FPS 兜底）；"
                             "可在『设置』中指定 scrcpy.exe 路径。", "warn")
                self.start()
            return
        self.start()

    def stop_mirror(self) -> None:
        """停止任一正在运行的投屏引擎（两个引擎共用『停止投屏』按钮）。"""
        if self.streamer and self.streamer.running:
            self.stop()
        if self.scrcpy and self.scrcpy.proc and self.scrcpy.proc.poll() is None:
            self.stop_scrcpy()

    def start(self) -> None:
        if self.streamer and self.streamer.running:
            return
        try:
            dev = self.app.ensure_device()
        except AdbError as exc:
            self.app.log(f"✗ {exc.message}", "error")
            self.app.log(f"  {exc.hint}", "warn")
            return

        self.streamer = ScreenStreamer(self.app.adb, dev.serial)
        self.streamer.fps = self._clamp_fps(int(self.fps_var.get() or 6))
        self.streamer.quality_name = self.quality_var.get()
        self.streamer.rotation = int(self.rot_var.get() or 0)
        self.streamer.max_width = int(self.width_var.get() or 0)
        self.streamer.on_status = lambda t, lvl="info": self.app.after_ui(lambda: self.app.log(t, lvl))
        # 互斥：内置引擎与 scrcpy 同时运行会争抢 USB 带宽导致画面卡顿，
        # 启动内置引擎前先停掉正在运行的 scrcpy
        if self.scrcpy and self.scrcpy.proc and self.scrcpy.proc.poll() is None:
            self.stop_scrcpy()
            self.app.log("已停止 scrcpy（内置引擎与 scrcpy 不可同时运行，避免抢带宽）。", "warn")
        try:
            self.streamer.start()
        except AdbError as exc:
            self.app.log(f"✗ {exc.message}", "error")
            self.app.log(f"  {exc.hint}", "warn")
            return

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.app.log("投屏已启动（内置帧引擎）。", "ok")
        if self.app.cfg.get("auto_wake", True):
            self.do_wake(silent=True)
        self._tick()

    def stop(self) -> None:
        if self.streamer:
            self.streamer.stop()
            self.streamer = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.info_var.set("投屏已停止")

    def shutdown(self) -> None:
        self.stop()
        if self._wake_state_job:
            try:
                self.after_cancel(self._wake_state_job)
            except Exception:
                pass
        if self.keeper:
            self.keeper.stop()
            self.keeper = None
        try:
            self.stop_scrcpy()
        except Exception:
            pass

    def _tick(self) -> None:
        if not self.streamer or not self.streamer.running:
            return
        self._render()
        self.after(60, self._tick)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #

    def _render(self) -> None:
        st = self.streamer
        if st is None or st.latest is None:
            return
        try:
            from PIL import ImageTk
        except ImportError:
            self.info_var.set("缺少 Pillow 依赖，无法渲染画面")
            return

        frame = st.latest
        fw, fh = frame.size
        rw, rh = st.device_size()
        if st.rotation in (90, 270):
            rw, rh = rh, rw
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)

        # 缩放：适应窗口=等比填满画布；百分比=相对设备原生分辨率的绝对比例
        zoom_text = self.zoom_var.get()
        if zoom_text.endswith("%"):
            try:
                fit = int(zoom_text.rstrip("%")) / 100.0
            except ValueError:
                fit = min(cw / rw, ch / rh)
        else:
            fit = min(cw / rw, ch / rh)
        fit = max(0.05, min(fit, 4.0))

        disp_w, disp_h = max(1, int(rw * fit)), max(1, int(rh * fit))
        if (disp_w, disp_h) != (fw, fh):
            resample = 1 if disp_w < fw else 2
            frame = frame.resize((disp_w, disp_h), resample)
        self.photo = ImageTk.PhotoImage(frame)
        self.canvas.delete("screen")
        self.canvas.create_image(cw // 2, ch // 2, image=self.photo, tags="screen")

        self._disp_scale = fit
        self._offset = ((cw - disp_w) / 2, (ch - disp_h) / 2)
        self.info_var.set(
            f"设备 {st.device_width}×{st.device_height}｜显示 {disp_w}×{disp_h}｜"
            f"实际 {st.actual_fps} FPS｜单帧耗时 {st.capture_ms} ms｜第 {st.frame_count} 帧")

    # ------------------------------------------------------------------ #
    # 鼠标 / 键盘
    # ------------------------------------------------------------------ #

    def _to_device(self, cx, cy):
        st = self.streamer
        if st is None:
            return 0, 0
        return st.map_to_device(cx, cy, self._offset[0], self._offset[1], self._disp_scale)

    def _require_input(self):
        if self.app.input is None:
            raise AdbError("当前设备不可操控", "请先在顶部选择一台状态为『已授权 · 可操控』的设备。")
        return self.app.input

    def _on_press(self, event):
        if self.streamer is None:
            return
        self.canvas.focus_set()
        self._drag = {"x": event.x, "y": event.y, "t": event.time, "moved": False,
                      "lx": event.x, "ly": event.y, "pts": [(event.x, event.y)]}

    def _on_motion(self, event):
        d = self._drag
        if not d:
            return
        if abs(event.x - d["x"]) > DRAG_THRESHOLD or abs(event.y - d["y"]) > DRAG_THRESHOLD:
            d["moved"] = True
        d["lx"], d["ly"] = event.x, event.y
        if d["moved"]:
            # 记录轨迹点（限速去重，供图案解锁等完整手势使用）
            pts = d["pts"]
            if len(pts) < 240:
                lx, ly = pts[-1]
                if abs(event.x - lx) >= 12 or abs(event.y - ly) >= 12:
                    pts.append((event.x, event.y))
            self.canvas.delete("guide")
            self.canvas.create_line(d["x"], d["y"], event.x, event.y,
                                    fill="#1668DC", width=2, tags="guide")

    def _on_release(self, event):
        d = self._drag
        self._drag = None
        self.canvas.delete("guide")
        if not d or self.streamer is None:
            return
        duration = max(60, event.time - d["t"])
        try:
            ctl = self._require_input()
            q = self.app.input_q
            if q is None:
                raise AdbError("输入通道未就绪", "请重新选择设备。")
            if d["moved"]:
                pts = d.get("pts") or [(d["x"], d["y"]), (event.x, event.y)]
                # 慢速长轨迹（图案解锁等）→ 单次连续手势完整发送；
                # 快速滑动 → 保持单条 swipe（跟手）
                if len(pts) >= 4 and duration >= 600:
                    dev_pts = [self._to_device(px, py) for px, py in pts]
                    q.submit(ctl.gesture, dev_pts)
                    self.hint_var.set(f"手势轨迹 {len(dev_pts)} 点（用于图案解锁等）")
                else:
                    x1, y1 = self._to_device(d["x"], d["y"])
                    x2, y2 = self._to_device(event.x, event.y)
                    q.submit(ctl.swipe, x1, y1, x2, y2, min(2000, duration))
                    self.hint_var.set(f"滑动 ({x1},{y1}) → ({x2},{y2})")
            elif duration >= LONG_PRESS_MS:
                x, y = self._to_device(event.x, event.y)
                q.submit(ctl.long_press, x, y, min(3000, duration))
                self.hint_var.set(f"长按 ({x},{y}) {duration} ms")
            else:
                x, y = self._to_device(event.x, event.y)
                q.submit(ctl.tap, x, y)
                self.hint_var.set(f"点击 ({x},{y})")
        except AdbError as exc:
            self.app.log(f"✗ 控制失败：{exc.message}", "error")
            if exc.hint:
                self.app.log(f"  {exc.hint}", "warn")

    def _on_wheel(self, event):
        if event.delta > 0:
            self._swipe_dir("下")
        else:
            self._swipe_dir("上")

    def _swipe_dir(self, direction: str) -> None:
        try:
            ctl = self._require_input()
            q = self.app.input_q
            if q is None:
                raise AdbError("输入通道未就绪", "请重新选择设备。")
            st = self.streamer
            w, h = st.device_size() if st else (1080, 2400)
            q.submit(ctl.swipe_direction, direction, w, h)
            self.hint_var.set(f"已{direction}滑")
        except AdbError as exc:
            self.app.log(f"✗ 滑动失败：{exc.message}", "error")

    def _key(self, name: str) -> None:
        try:
            ctl = self._require_input()
            q = self.app.input_q
            if q is None:
                raise AdbError("输入通道未就绪", "请重新选择设备。")
            q.submit(ctl.key_by_name, name)
            self.hint_var.set(f"已发送：{name}")
        except AdbError as exc:
            self.app.log(f"✗ {name} 失败：{exc.message}", "error")
            if exc.hint:
                self.app.log(f"  {exc.hint}", "warn")

    def _long_power(self) -> None:
        try:
            ctl = self._require_input()
            q = self.app.input_q
            if q is None:
                raise AdbError("输入通道未就绪", "请重新选择设备。")
            q.submit(ctl.long_press_power)
            self.hint_var.set("已发送长按电源")
        except AdbError as exc:
            self.app.log(f"✗ 失败：{exc.message}", "error")

    def _send_text(self) -> None:
        text = self.text_var.get()
        if not text:
            return

        def work():
            return self._require_input().input_text(text)

        def done(channel):
            self.chan_var.set(f"已通过{channel}发送 {len(text)} 个字符")
            self.app.log(f"文本已发送（{channel}）", "ok")

        run_async(self.app, work, on_done=done, busy_text="发送文本…")

    def _clear_field(self) -> None:
        def work():
            self._require_input().clear_field()
            return True
        run_async(self.app, work, on_done=lambda _v: self.app.log("已清空输入框", "ok"),
                  busy_text="清空输入框…")

    def _clipboard(self) -> None:
        text = self.text_var.get()
        if not text:
            return

        def work():
            ok = self._require_input().clipboard_set(text)
            if not ok:
                raise AdbError("写入剪贴板失败",
                               "该功能依赖 Clipper 等支持广播写入剪贴板的应用。"
                               "替代方案：发送文本后，在投屏中长按输入框选择『粘贴』。")
            return True

        run_async(self.app, work,
                  on_done=lambda _v: self.app.log("已尝试写入手机剪贴板", "ok"),
                  busy_text="写入剪贴板…")

    def _on_key_press(self, event):
        if not self.passthrough.get() or self.app.input is None:
            return
        q = self.app.input_q
        if q is None:
            return
        ctl = self.app.input
        if len(event.char) and event.char.isprintable():
            q.submit(ctl.input_text_ascii, event.char)
        elif event.keysym == "Return":
            q.submit(ctl.key_event, 66)
        elif event.keysym == "BackSpace":
            q.submit(ctl.key_event, 67)

    # ------------------------------------------------------------------ #
    # 全屏 / scrcpy
    # ------------------------------------------------------------------ #

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.app.attributes("-fullscreen", self._fullscreen)
        if self._fullscreen:
            self.app.log("已进入全屏，按 Esc 退出。", "info")

    def _exit_fullscreen(self) -> None:
        if self._fullscreen:
            self._fullscreen = False
            self.app.attributes("-fullscreen", False)

    def launch_scrcpy_smooth(self) -> None:
        """以"直接双击 scrcpy.exe"的稳妥参数启动 scrcpy（主按钮 scrcpy 引擎路径）。

        直接运行的 scrcpy（USB 直连、8M、默认缓冲、直接控制）实测不卡；
        本方法把参数统一为该配置并降为 720p/60 帧，进一步减轻电脑解码负担。
        注意：不勾选低延迟（--video-buffer=0）与无线 TCP/IP —— 这两项
        在 USB 直连场景反而会让画面一顿一顿。
        """
        self.sc_fps_var.set("60 FPS")
        self.sc_size_var.set("720")
        self.sc_bitrate_var.set("8M")
        self.sc_latency_var.set(False)
        self.sc_tcpip_var.set(False)
        self.sc_control_var.set(True)   # scrcpy 窗口直接控制（与直接运行一致）
        self.sc_audio_var.set(False)
        self.app.log("已套用稳妥参数：720p / 60 FPS / 8M / USB 直连 / 默认缓冲 / 直接控制。", "ok")
        self.launch_scrcpy()

    def launch_scrcpy(self) -> None:
        try:
            dev = self.app.ensure_device()
        except AdbError as exc:
            self.app.log(f"✗ {exc.message}", "error")
            if exc.hint:
                self.app.log(f"  {exc.hint}", "warn")
            return
        if not self.scrcpy.available():
            self.app.log("未找到 scrcpy。可继续使用内置引擎，或在『设置』中指定路径。", "warn")
            return
        # 互斥：先停掉内置帧引擎，避免两个采集通道同时跑、争抢 USB 带宽导致画面卡顿
        if self.streamer and self.streamer.running:
            self.stop()
            self.app.log("已停止内置引擎（scrcpy 与内置引擎不可同时运行，避免抢带宽）。", "warn")
        # 上一个 scrcpy 实例还在跑时先停掉，避免两个窗口争抢同一设备
        if self.scrcpy.proc and self.scrcpy.proc.poll() is None:
            self.stop_scrcpy()

        max_fps = ScrcpyLauncher.FPS_PRESETS.get(self.sc_fps_var.get(), 0)
        size_text = self.sc_size_var.get()
        max_size = 0 if size_text.startswith("原生") else int(size_text)
        no_control = not self.sc_control_var.get()
        low_latency = self.sc_latency_var.get()
        tcpip = self.sc_tcpip_var.get()
        audio = self.sc_audio_var.get()

        self.app.cfg.set("scrcpy_fps", max_fps)
        self.app.cfg.set("scrcpy_size", max_size)
        self.app.cfg.set("scrcpy_bitrate", self.sc_bitrate_var.get())
        self.app.cfg.set("scrcpy_codec", self.sc_codec_var.get())
        self.app.cfg.set("scrcpy_no_control", no_control)
        self.app.cfg.set("scrcpy_turn_off", self.sc_off_var.get())
        self.app.cfg.set("scrcpy_low_latency", low_latency)
        self.app.cfg.set("scrcpy_tcpip", tcpip)
        self.app.cfg.set("scrcpy_audio", audio)

        def work():
            self.scrcpy.launch(
                dev.serial,
                max_size=max_size,
                max_fps=max_fps,
                bit_rate=self.sc_bitrate_var.get(),
                video_codec=self.sc_codec_var.get(),
                rotation=int(self.rot_var.get() or 0),
                turn_screen_off=self.sc_off_var.get(),
                no_control=no_control,
                no_audio=not audio,
                stay_awake=True,
                always_on_top=True,
                window_title=f"救援镜像 - {dev.serial}",
                low_latency=low_latency,
                tcpip=tcpip,
                audio=audio,
            )
            return True

        def done(_v):
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            fps_text = "不限（跟随设备，最高可达屏幕刷新率）" if max_fps == 0 else f"{max_fps} FPS"
            ctrl_text = "scrcpy 窗口直接控制" if not no_control else "控制仍在本窗口完成"
            extra = []
            if low_latency:
                extra.append("低延迟")
            if tcpip:
                extra.append("TCP/IP 通道")
            if audio:
                extra.append("音频同步")
            self.app.log(f"scrcpy 已启动：帧率 {fps_text}，分辨率上限 "
                         f"{'原生' if not max_size else str(max_size)}，码率 "
                         f"{self.sc_bitrate_var.get()}，{ctrl_text}"
                         + (f"，{' + '.join(extra)}" if extra else "") + "。", "ok")
            if no_control:
                self.app.log("提示：当前为『控制仍在本窗口』模式，输入走 adb 通道延迟较高；"
                             "需要最低延迟请在启动前勾选『scrcpy 窗口直接控制』"
                             "（画面里直接鼠标操作，设备端本地注入）。", "warn")

        run_async(self.app, work, on_done=done, busy_text="启动 scrcpy…")

    def stop_scrcpy(self) -> None:
        self.scrcpy.stop()
        self.app.log("scrcpy 已停止", "info")
        if not (self.streamer and self.streamer.running):
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

    def _scrcpy_exited(self) -> None:
        """scrcpy 窗口被用户直接关闭时恢复按钮状态（主动停止不会走到这里）。"""
        if self.scrcpy.proc is None:
            return
        self.scrcpy.proc = None
        self.app.log("scrcpy 已退出。", "info")
        if not (self.streamer and self.streamer.running):
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
