# -*- coding: utf-8 -*-
"""真机版界面验证：用 tools/adb.exe 启动程序，检查是否能识别真实设备。

会自动关闭弹出的「未授权」提示框，仅用于验证连通性，不参与 run_tests.py。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
sys.path.insert(0, ROOT)

import tkinter.messagebox as mb  # noqa: E402

from ui.app import App  # noqa: E402

popped = []
mb.showwarning = lambda *a, **k: popped.append(a[1] if len(a) > 1 else "")
mb.showerror = lambda *a, **k: popped.append("ERROR:" + str(a[1]))
mb.showinfo = lambda *a, **k: None
mb.askyesno = lambda *a, **k: False

app = App()
app.adb.adb_path = os.path.join(ROOT, "tools", "adb.exe")


def report():
    print("=" * 60)
    print("adb 路径：", app.adb.adb_path, "存在：", os.path.isfile(app.adb.adb_path))
    print("检测到的设备：")
    for d in app.devices:
        print(f"   {d.serial}  状态={d.state}  {d.state_text}")
    print("顶部状态栏：", app.state_var.get())
    print("当前选中：", app.device.display_name if app.device else "无")
    if popped:
        print("弹出的提示框：", popped[0][:80].replace("\n", " / "), "...")
    print("-" * 60)
    print("运行日志：")
    print(app.log_panel.text.get("1.0", "end").strip())
    print("=" * 60)
    app.destroy()


app.after(9000, report)
app.mainloop()
