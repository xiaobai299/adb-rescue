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
    """打包验证用：打印运行环境摘要后退出（不启动 GUI）。"""
    print("== ADB 救援工具 自检 ==")
    print(f"Python      : {sys.version.split()[0]}")
    print(f"tkinter     : {tk.TkVersion}")
    try:
        import PIL
        print(f"Pillow      : {PIL.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"Pillow      : 缺失（{exc}）")
    from core.adb import locate_adb, locate_scrcpy, candidate_adb_paths, app_base_dir
    print(f"程序根目录  : {app_base_dir()}")
    adb = locate_adb()
    print(f"adb         : {adb or '未找到'}")
    sc = locate_scrcpy()
    print(f"scrcpy      : {sc or '未找到'}")
    if adb:
        from core.adb import AdbClient
        print(f"adb 版本    : {AdbClient(adb_path=adb).version()}")
    if sc:
        from core.screen import ScrcpyLauncher
        print(f"scrcpy 版本 : {ScrcpyLauncher(AdbClient(adb_path=adb or ''), scrcpy_path=sc).version()}")
    ok = bool(adb) and bool(sc)
    print("结果: " + ("通过 ✔" if ok else "异常 ✘（缺少 adb 或 scrcpy，需将 tools/ 放在 exe 同级目录）"))
    return 0 if ok else 1


def main() -> None:
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
