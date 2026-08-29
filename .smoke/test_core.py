# -*- coding: utf-8 -*-
"""核心层冒烟测试：用模拟 adb 验证设备检测、信息采集、目录解析、控制指令。"""

from __future__ import annotations

import os
import sys
import tempfile
import time as _t

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 测试期间隔离配置目录，避免污染用户真实配置
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
# 把临时目录固定到项目内（沙箱/杀软可能禁止删除系统临时目录，导致清理失败）
_local_tmp = os.path.join(HERE, "tmp")
os.makedirs(_local_tmp, exist_ok=True)
os.environ["TMP"] = _local_tmp
os.environ["TEMP"] = _local_tmp
tempfile.tempdir = _local_tmp
sys.path.insert(0, ROOT)

from core.adb import AdbClient, AdbError, _to_bytes  # noqa: E402
from core.files import FileManager, _parse_ls_line, _parse_content_rows  # noqa: E402
from core.inputctl import InputController  # noqa: E402
from core.ops import AppManager, PowerOps  # noqa: E402
from core.screen import ScreenStreamer  # noqa: E402
from test_util import TmpDir  # noqa: E402

FAKE = os.path.join(HERE, "fake_adb.bat")
adb = AdbClient(adb_path=FAKE)
ok = 0
fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {extra}")


print("== 1. 设备检测 ==")
devices = adb.devices()
check("枚举到 2 台设备", len(devices) == 2, devices)
dev = devices[0]
check("第一台为已授权状态", dev.state == "device", dev.state)
check("型号解析为 Pixel 7", dev.model == "Pixel 7", dev.model)
check("未授权设备识别", devices[1].state == "unauthorized")

print("== 2. 设备信息采集 ==")
info = adb.collect_info(dev.serial)
check("品牌", info.brand == "Google", info.brand)
check("Android 版本", info.android_version == "14", info.android_version)
check("分辨率", info.resolution == "1080x2400", info.resolution)
check("电量", info.battery_level == "62", info.battery_level)
check("充电状态", info.charging == "USB", info.charging)
check("电池状态", info.battery_status == "充电中", info.battery_status)
check("内存摘要非空", bool(info.memory_summary), info.memory_summary)
check("屏幕点亮", info.screen_on is True, info.screen_on)
check("非 root", info.rooted is False)

print("== 3. 错误解释 ==")
hint = adb.explain("error: device unauthorized.")
check("未授权提示中文", "未授权" in hint, hint)
hint = adb.explain("error: no devices/emulators found")
check("无设备提示中文", "未检测到任何设备" in hint, hint)

print("== 4. 目录解析（兼容两种 ls 格式） ==")
fm = FileManager(adb, dev.serial)
entries = fm.list_dir("/sdcard")
check("解析出 4 个条目", len(entries) == 4, [e.name for e in entries])
names = [e.name for e in entries]
check("含中文带空格文件名", "我的 照片.jpg" in names, names)
dcim = next(e for e in entries if e.name == "DCIM")
check("目录识别", dcim.is_dir is True)
check("文件大小解析", dcim.size == 4096, dcim.size)
note = next(e for e in entries if e.name == "notes.txt")
check("文件类型判定", note.kind == "文档", note.kind)
photo = next(e for e in entries if e.name == "我的 照片.jpg")
check("图片类型判定", photo.kind == "图片", photo.kind)
# 旧版 toolbox 格式（无 links 字段）
legacy = "-rw-rw---- root     sdcard_rw      204 2026-08-25 18:44 legacy.txt"
e2 = _parse_ls_line(legacy, "/sdcard")
check("兼容旧格式", e2 is not None and e2.name == "legacy.txt" and e2.size == 204,
      e2)
# 符号链接
link = "lrw-rw---- 1 root sdcard_rw 21 2026-08-25 18:44 shortcut -> /sdcard/DCIM"
e3 = _parse_ls_line(link, "/sdcard")
check("符号链接剥离", e3 is not None and e3.name == "shortcut", e3)

print("== 5. content query 解析 ==")
rows = _parse_content_rows("Row: 0 display_name=张三, number=13800000000, data2=2\nRow: 1 display_name=Bob, number=139, data2=1")
check("解析 2 行", len(rows) == 2, rows)
check("中文姓名", rows[0]["display_name"] == "张三", rows[0])

print("== 6. 唤醒与控制指令 ==")
ctl = InputController(adb, dev.serial)
check("读取屏幕状态", ctl.screen_state() != "Unknown", ctl.screen_state())
rep = ctl.wake()
check("唤醒返回状态报告", isinstance(rep, dict) and "ok" in rep, rep)
check("唤醒步骤有记录", len(rep.get("steps", [])) > 0, rep.get("steps"))
check("唤醒后状态已复查", rep.get("after") in ("Awake", "Asleep", "Unknown"), rep.get("after"))
check("tap 不抛异常", ctl.tap(100, 200) is None)

print("== 6b. 保活器（启动后停止） ==")
from core.inputctl import KeepAwake  # noqa: E402
keeper = KeepAwake(ctl, interval=0.5)
keeper.start()
_t.sleep(1.2)
keeper.stop()
check("保活线程可启停", not keeper.running)

print("== 6c. 输入队列（后台串行执行，不阻塞主线程） ==")
from core.inputctl import InputQueue  # noqa: E402
iq = InputQueue(ctl)
iq.submit(ctl.tap, 10, 20)
iq.submit(ctl.key_event, 4)
deadline = _t.time() + 5
while iq.sent < 2 and _t.time() < deadline:
    _t.sleep(0.05)
check("输入队列顺序执行 2 条命令", iq.sent == 2, iq.sent)

print("== 6d. 连续手势（图案解锁 motionevent） ==")
ctl.gesture([(100, 200), (300, 400), (500, 200), (700, 400)])
check("gesture 发送 DOWN/MOVE/UP 不抛异常", True)
ctl.gesture([(0, 0), (10, 10)])
check("gesture 两点也正常", True)

print("== 7. 应用管理 ==")
am = AppManager(adb, dev.serial)
pkgs = am.list_packages(third_party=True)
check("列出第三方应用", pkgs and pkgs[0][0] == "com.demo.app", pkgs)
with TmpDir() as td:
    saved = am.backup_apk("com.demo.app", td)
    check("APK 备份调用成功", len(saved) == 1, saved)

print("== 8. 系统信息 ==")
pw = PowerOps(adb, dev.serial)
bat = pw.battery_info()
check("电池信息含电量", bat.get("电量") == "62", bat)
check("温度换算", bat.get("温度") == "28.5 ℃", bat.get("温度"))
rep = pw.device_report()
check("体检报告非空", len(rep) >= 3, rep)

print("== 9. 投屏引擎（坐标映射） ==")
st = ScreenStreamer(adb, dev.serial)
st.device_width, st.device_height = 1080, 2400
for rot, (cx, cy), expect in [
    (0, (0, 0), (0, 0)),
    (0, (1079, 2399), (1079, 2399)),
    (90, (0, 0), (0, 2399)),
    (90, (2399, 1079), (1079, 0)),
    (180, (0, 0), (1079, 2399)),
    (180, (1079, 2399), (0, 0)),
    (270, (0, 0), (1079, 0)),
    (270, (2399, 1079), (0, 2399)),
]:
    st.rotation = rot
    got = st.map_to_device(cx, cy, 0, 0, 1.0)
    check(f"旋转 {rot}° 映射 {expect}", got == expect, got)
st.rotation = 0
check("显示尺寸（0°）", st.display_size() == (1080 * 3 // 4, 2400 * 3 // 4), st.display_size())

print("== 9b. 投屏引擎（真实取帧） ==")
st2 = ScreenStreamer(adb, dev.serial)
st2.fps = 10
st2.quality_name = "流畅（省带宽）"
st2.start()
_t.sleep(1.5)
st2.stop()
check("取帧成功", st2.frame_count > 0, st2.frame_count)
check("解析出设备分辨率", (st2.device_width, st2.device_height) == (1080, 2400),
      (st2.device_width, st2.device_height))
check("帧已按画质缩放", st2.latest is not None and st2.latest.size == (540, 1200),
      st2.latest.size if st2.latest else None)

print("== 10. 字节换算 ==")
check("df KB 换算", _to_bytes("117440512", "", unit_is_kb=True) == 117440512 * 1024)
check("带后缀换算", _to_bytes("1.5", "G") == int(1.5 * 1024 ** 3))

print()
print(f"结果：通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
