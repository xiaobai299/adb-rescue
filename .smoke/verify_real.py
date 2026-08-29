# -*- coding: utf-8 -*-
"""真机连通性验证：使用 tools/adb.exe 检测真实连接的设备。

需要真实手机与 USB 连接，不参与 run_tests.py 的自动测试。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.adb import AdbClient  # noqa: E402

ADB = os.path.join(ROOT, "tools", "adb.exe")
adb = AdbClient(adb_path=ADB)
print("adb 版本：", adb.version())

adb.start_server()
devices = adb.devices()
if not devices:
    print("\n结果：未检测到任何设备。")
    print("请检查：① 数据线支持数据传输 ② 手机已开启 USB 调试 ③ 驱动已安装")
    sys.exit(2)

print(f"\n检测到 {len(devices)} 台设备：")
for d in devices:
    print(f"  - {d.serial}  状态={d.state}  {d.state_text}")
    if d.state == "device":
        info = adb.collect_info(d.serial)
        print(f"      型号={info.model or info.brand} Android={info.android_version} "
              f"分辨率={info.resolution} 电量={info.battery_level}%")
        print(f"      存储={info.storage_summary}")
        print("      → 状态正常，可以投屏")
    elif d.state == "unauthorized":
        print("      → 请在手机上点『允许 USB 调试』后重新检测")
