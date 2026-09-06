# -*- coding: utf-8 -*-
"""通用界面组件：日志面板、风险确认对话框、异步任务包装、进度面板。"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

# --------------------------------------------------------------------------- #
# 主题
# --------------------------------------------------------------------------- #

FONT = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

COLOR = {
    "bg": "#F5F6F8",
    "panel": "#FFFFFF",
    "border": "#D9DCE1",
    "text": "#1F2329",
    "muted": "#6B7280",
    "primary": "#1668DC",
    "success": "#18A058",
    "warn": "#E8912D",
    "danger": "#D03050",
    "log_info": "#1F2329",
    "log_ok": "#18A058",
    "log_warn": "#E8912D",
    "log_err": "#D03050",
}


def style_root(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(background=COLOR["bg"])
    # 高 DPI 屏（125%/150% 缩放）：按系统缩放像素类尺寸，避免控件偏小
    try:
        dpi_scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        dpi_scale = 1.0
    style.configure(".", background=COLOR["bg"], foreground=COLOR["text"],
                    font=(FONT, 10))
    style.configure("TFrame", background=COLOR["bg"])
    style.configure("Panel.TFrame", background=COLOR["panel"], relief="flat")
    style.configure("TLabel", background=COLOR["bg"], foreground=COLOR["text"])
    style.configure("Panel.TLabel", background=COLOR["panel"])
    style.configure("Muted.TLabel", foreground=COLOR["muted"], background=COLOR["bg"])
    style.configure("Title.TLabel", font=(FONT, 11, "bold"))
    style.configure("TButton", padding=(10, 5))
    style.configure("Accent.TButton", background=COLOR["primary"], foreground="#FFFFFF")
    style.map("Accent.TButton", background=[("active", "#0958C0")])
    style.configure("Danger.TButton", background=COLOR["danger"], foreground="#FFFFFF")
    style.map("Danger.TButton", background=[("active", "#A81F3C")])
    style.configure("TNotebook", background=COLOR["bg"])
    style.configure("TNotebook.Tab", padding=(14, 6), font=(FONT, 10))
    style.configure("Treeview", rowheight=int(24 * dpi_scale), font=(FONT, 10))
    style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
    style.configure("TProgressbar", thickness=int(16 * dpi_scale))
    style.configure("Horizontal.TProgressbar", background=COLOR["primary"])


# --------------------------------------------------------------------------- #
# 日志面板（线程安全）
# --------------------------------------------------------------------------- #

TAG_COLOR = {
    "info": COLOR["log_info"],
    "ok": COLOR["log_ok"],
    "warn": COLOR["log_warn"],
    "error": COLOR["log_err"],
}


class LogPanel(ttk.Frame):
    """带颜色分级、可被任意线程写入的日志区。"""

    def __init__(self, master, height: int = 12, mono: bool = False):
        super().__init__(master)
        self._queue: queue.Queue = queue.Queue()
        self._max_lines = 3000

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="清空", width=6, command=self.clear).pack(side="right", padx=4, pady=2)
        self._follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动滚动", variable=self._follow).pack(side="right", padx=4)
        self._count_var = tk.StringVar(value="0 行")
        ttk.Label(bar, textvariable=self._count_var, style="Muted.TLabel").pack(side="left", padx=6)

        self.text = tk.Text(self, height=height, wrap="none",
                            font=(FONT_MONO if mono else FONT, 9),
                            bg="#FFFFFF", fg=COLOR["text"], relief="solid", bd=1)
        self.text.pack(fill="both", expand=True)
        for tag, color in TAG_COLOR.items():
            self.text.tag_configure(tag, foreground=color)

        yscroll = ttk.Scrollbar(self.text, command=self.text.yview)
        yscroll.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=yscroll.set)

        self._lines = 0
        self._pump()

    def write(self, message: str, level: str = "info") -> None:
        self._queue.put((message, level))

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._lines = 0
        self._count_var.set("0 行")

    def _pump(self) -> None:
        try:
            while True:
                message, level = self._queue.get_nowait()
                self.text.configure(state="normal")
                self.text.insert("end", message if message.endswith("\n") else message + "\n",
                                 level if level in TAG_COLOR else "info")
                self._lines += 1
                if self._lines > self._max_lines:
                    self.text.delete("1.0", "400.0")
                    self._lines -= 399
                if self._follow.get():
                    self.text.see("end")
                self.text.configure(state="disabled")
                self._count_var.set(f"{self._lines} 行")
        except queue.Empty:
            pass
        self.after(120, self._pump)


# --------------------------------------------------------------------------- #
# 风险确认
# --------------------------------------------------------------------------- #

RISK_META = {
    "warn": ("⚠ 操作影响设备状态", COLOR["warn"]),
    "danger": ("⛔ 高风险操作（不可撤销）", COLOR["danger"]),
}


def ask_confirm(parent, *, title: str, message: str, details: str = "",
                risk: str = "danger", require_text: str | None = None,
                confirm_label: str = "确认执行") -> bool:
    """二次确认对话框。risk=danger 时必须勾选确认框（或输入指定文字）。"""
    result = {"ok": False}
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.grab_set()
    win.resizable(False, False)
    win.configure(background=COLOR["bg"])

    head_text, head_color = RISK_META.get(risk, ("确认操作", COLOR["primary"]))
    tk.Label(win, text=head_text, fg=head_color, bg=COLOR["bg"],
             font=(FONT, 12, "bold")).pack(anchor="w", padx=16, pady=(14, 4))

    tk.Label(win, text=message, bg=COLOR["bg"], fg=COLOR["text"], justify="left",
             wraplength=460, font=(FONT, 10)).pack(anchor="w", padx=16, pady=4)

    if details:
        box = tk.Text(win, height=5, wrap="word", font=(FONT, 9), relief="solid", bd=1)
        box.pack(fill="x", padx=16, pady=6)
        box.insert("1.0", details)
        box.configure(state="disabled")

    check_var = tk.BooleanVar(value=False)
    entry_var = tk.StringVar(value="")
    if require_text:
        tk.Label(win, text=f"请在此输入【{require_text}】以继续：", bg=COLOR["bg"],
                 fg=COLOR["text"], font=(FONT, 9)).pack(anchor="w", padx=16)
        ttk.Entry(win, textvariable=entry_var, width=40).pack(fill="x", padx=16, pady=(2, 8))
    else:
        ttk.Checkbutton(win, text="我已了解上述风险，并确认对目标设备拥有合法授权",
                        variable=check_var).pack(anchor="w", padx=16, pady=8)

    def do_ok():
        if require_text:
            if entry_var.get().strip() != require_text:
                messagebox.showwarning("输入不匹配", f"请输入准确的【{require_text}】后再执行。", parent=win)
                return
        elif not check_var.get():
            messagebox.showwarning("需要确认", "请先勾选风险确认框。", parent=win)
            return
        result["ok"] = True
        win.destroy()

    bar = ttk.Frame(win)
    bar.pack(fill="x", pady=(6, 14))
    ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=(8, 16))
    ttk.Button(bar, text=confirm_label, style="Danger.TButton" if risk == "danger" else "Accent.TButton",
               command=do_ok).pack(side="right")

    win.update_idletasks()
    _center(win, parent)
    win.wait_window()
    return result["ok"]


def _center(win: tk.Toplevel, parent=None) -> None:
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    if parent is not None:
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    else:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


# --------------------------------------------------------------------------- #
# 进度面板
# --------------------------------------------------------------------------- #

class ProgressPanel(ttk.Frame):
    def __init__(self, master, label: str = "进度"):
        super().__init__(master)
        self.var = tk.StringVar(value="就绪")
        self.bar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(self, textvariable=self.var, style="Muted.TLabel").pack(anchor="w", padx=4)

    def set(self, percent: float, text: str = "") -> None:
        self.bar["value"] = max(0, min(100, percent))
        if text:
            self.var.set(text)

    def reset(self, text: str = "就绪") -> None:
        self.bar["value"] = 0
        self.var.set(text)

    @property
    def value(self) -> float:
        return float(self.bar["value"])


# --------------------------------------------------------------------------- #
# 异步任务
# --------------------------------------------------------------------------- #

def run_async(app, func, *, on_done=None, on_error=None, busy_text: str = "执行中…"):
    """在后台线程执行 func，回调在主线程执行。func 抛出的异常自动翻译为中文提示。"""
    app.set_busy(True, busy_text)

    def worker():
        try:
            value = func()
        except Exception as exc:  # noqa: BLE001 - 统一兜底，界面层展示中文
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            tb = traceback.format_exc()
            app.after(0, lambda: _finish(app, None, exc, detail, tb, on_error))
            return
        app.after(0, lambda: _finish(app, value, None, "", "", on_error))

    def _finish(_app, value, exc, detail, tb, err_cb):
        app.set_busy(False)
        if exc is not None:
            if err_cb:
                try:
                    err_cb(exc)
                    return
                except Exception:
                    pass
            _show_error(app, exc, detail, tb)
            return
        if on_done:
            try:
                on_done(value)
            except Exception as cb_exc:
                _show_error(app, cb_exc, str(cb_exc), "")

    threading.Thread(target=worker, daemon=True).start()


def _show_error(app, exc, detail: str, tb: str) -> None:
    from core.adb import AdbError
    if isinstance(exc, AdbError):
        app.log(f"✗ {exc.message}", "error")
        if exc.hint:
            app.log(f"  排查建议：{exc.hint}", "warn")
        if exc.detail:
            app.log(f"  原始输出：{exc.detail.strip()[:300]}", "info")
        messagebox.showerror("操作失败", f"{exc.message}\n\n{exc.hint}".strip(), parent=app)
    else:
        app.log(f"✗ 发生异常：{detail}", "error")
        if tb:
            app.log(tb.strip()[-800:], "error")
        messagebox.showerror("程序异常", f"{detail}\n\n详情见『日志』页。", parent=app)


class Tooltip:
    """轻量提示气泡。"""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, background="#FFFFE0", relief="solid", bd=1,
                 font=(FONT, 9), wraplength=320, justify="left").pack(ipadx=6, ipady=4)

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None
