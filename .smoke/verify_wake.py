# -*- coding: utf-8 -*-
"""真机验证：唤醒 + 延长熄屏时间 + 自动保活。"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
sys.path.insert(0, ROOT)

from core.adb import AdbClient, locate_adb  # noqa: E402
from core.inputctl import InputController, KeepAwake  # noqa: E402

adb = AdbClient(adb_path=locate_adb())
adb.start_server()
devs = [d for d in adb.devices() if d.online]
if not devs:
    print("没有已授权的设备，无法验证"); sys.exit(2)
serial = devs[0].serial
ctl = InputController(adb, serial)

print("=" * 60)
print("设备:", serial)
print("=" * 60)
print("1. 初始状态        :", ctl.screen_state())
print("   自动熄屏时间    :", ctl.get_screen_timeout(), "ms")

print("\n2. 模拟息屏（按电源键）...")
adb.shell(serial, "input keyevent 26", timeout=10)
time.sleep(2)
print("   息屏后状态      :", ctl.screen_state())

print("\n3. 执行 wake() ...")
rep = ctl.wake()
for k, v in rep.items():
    if k == "steps":
        for s in v:
            print(f"     · {s}")
    else:
        print(f"   {k:<14}: {v}")

print("\n4. 复检            :", ctl.screen_state())
print("   自动熄屏时间    :", ctl.get_screen_timeout(), "ms")
print("   是否锁屏        :", ctl.is_locked())

print("\n5. 测试自动保活（间隔 5 秒）...")
keeper = KeepAwake(ctl, interval=5)
keeper.on_event = lambda t, lvl: print(f"   [保活] {t}")
keeper.start()
print("   再次息屏...")
adb.shell(serial, "input keyevent 26", timeout=10)
time.sleep(2)
print("   息屏后          :", ctl.screen_state())
print("   等待 8 秒...")
time.sleep(8)
print("   现在状态        :", ctl.screen_state())
print("   自动点亮次数    :", keeper.wake_count)
keeper.stop()

print("\n" + "=" * 60)
print("验证结束。自动熄屏时间已被改为 %s 毫秒（原为 30000）。" % ctl.get_screen_timeout())
print("如需恢复：adb shell settings put system screen_off_timeout 30000")
print("=" * 60)
