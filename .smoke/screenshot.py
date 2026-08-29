# -*- coding: utf-8 -*-
"""抓取界面截图用于人工检查布局（使用模拟 adb）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
sys.path.insert(0, ROOT)
FAKE = os.path.join(HERE, "fake_adb.bat")

from PIL import ImageGrab  # noqa: E402
from ui.app import App  # noqa: E402

app = App()
app.adb.adb_path = FAKE
app.cfg.set("adb_path", FAKE)
app.geometry("1320x860+40+20")


def shot():
    app.lift()
    app.attributes("-topmost", True)
    app.update()
    app.after(300, grab)


def grab():
    img = ImageGrab.grab()
    path = os.path.join(HERE, "ui.png")
    img.save(path)
    print("saved", path, img.size)
    app.destroy()


app.after(17000, shot)
app.mainloop()
