# -*- coding: utf-8 -*-
"""安卓手机救援控制工具 —— 程序入口。

运行方式：
    python main.py
    ADB救援工具.exe            # 打包后的可执行文件

依赖：Python 3.9+ / Pillow（图像渲染）/ ADB（Android SDK platform-tools）
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _check_env() -> None:
    if sys.version_info < (3, 9):
        raise SystemExit("需要 Python 3.9 或更高版本。")
    try:
        import PIL  # noqa: F401
    except ImportError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "缺少依赖",
            "未检测到 Pillow（图像库），投屏功能无法运行。\n\n"
            "请在命令行执行：\n    pip install Pillow\n\n"
            "然后再启动本程序。")
        root.destroy()
        raise SystemExit(1)


def _selftest() -> int:
    """打包验证用：输出运行环境摘要后退出（不启动 GUI）。

    控制台运行时打印到 stdout；打包成无控制台 exe（sys.stdout 为 None）
    时写入 exe 旁的 selftest.log，自检不通过时再弹窗提示。"""
    lines: list[str] = []

    def out(text: str = "") -> None:
        lines.append(text)
        try:
            print(text)
        except Exception:  # noqa: BLE001 - windowed exe 无 stdout，忽略
            pass

    out("== ADB 救援工具 自检 ==")
    out(f"Python      : {sys.version.split()[0]}")
    out(f"tkinter     : {tk.TkVersion}")
    try:
        import PIL
        out(f"Pillow      : {PIL.__version__}")
    except Exception as exc:  # noqa: BLE001
        out(f"Pillow      : 缺失（{exc}）")
    from core.adb import AdbClient, locate_adb, locate_scrcpy, app_base_dir
    base = app_base_dir()
    out(f"程序根目录  : {base}")
    adb = locate_adb()
    out(f"adb         : {adb or '未找到'}")
    sc = locate_scrcpy()
    out(f"scrcpy      : {sc or '未找到'}")
    if adb:
        out(f"adb 版本    : {AdbClient(adb_path=adb).version()}")
    if sc:
        from core.screen import ScrcpyLauncher
        out(f"scrcpy 版本 : {ScrcpyLauncher(AdbClient(adb_path=adb or ''), scrcpy_path=sc).version()}")
    ok = bool(adb) and bool(sc)
    out("结果: " + ("通过 ✔" if ok else "异常 ✘（缺少 adb 或 scrcpy，需将 tools/ 放在 exe 同级目录）"))

    if sys.stdout is None:
        try:
            with open(os.path.join(base, "selftest.log"), "w", encoding="utf-8") as fp:
                fp.write("\n".join(lines) + "\n")
        except OSError:
            pass
        if not ok:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("自检未通过", "\n".join(lines) + "\n\n详情见 exe 旁 selftest.log")
            root.destroy()
    return 0 if ok else 1


def _enable_windows_high_dpi() -> None:
    """声明高 DPI 感知（Windows）。

    不声明时系统会把整个窗口位图拉伸，125%/150% 缩放的屏幕上文字发虚；
    必须在创建任何 Tk 窗口之前调用。SetProcessDpiAwareness 重复声明会
    返回错误，忽略即可。"""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI Aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> None:
    _enable_windows_high_dpi()
    if "--selftest" in sys.argv:
        # 避免 GBK 控制台无法打印 ✔ 等字符
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.exit(_selftest())
    _check_env()
    from ui.app import App
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
