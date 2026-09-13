# -*- coding: utf-8 -*-
"""自动备份与剪贴板测试：增量清单、短信导出、缺失目录降级、剪贴板通道。"""

from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
_local_tmp = os.path.join(HERE, "tmp")
os.makedirs(_local_tmp, exist_ok=True)
os.environ["TMP"] = _local_tmp
os.environ["TEMP"] = _local_tmp
tempfile.tempdir = _local_tmp
sys.path.insert(0, ROOT)

from core.adb import AdbClient  # noqa: E402
from core.backup import AutoBackup, summarize  # noqa: E402
from core.inputctl import InputController, _extract_clip_text  # noqa: E402
from test_util import TmpDir  # noqa: E402

FAKE = os.path.join(HERE, "fake_adb.bat")
adb = AdbClient(adb_path=FAKE)
SERIAL = "ABC123"
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


print("== 1. 自动备份：增量（首次全备，二次全跳过） ==")
with TmpDir() as td:
    ab = AutoBackup(adb, SERIAL)
    r1 = ab.run(["photos"], td)
    check("首次备份新文件 2 个（jpg+mp4）", r1["new"] == 2, r1)
    check("manifest 已写入", os.path.isfile(os.path.join(td, ".manifest.json")))
    r2 = ab.run(["photos"], td)
    check("二次备份全部跳过", r2["skipped"] == 2 and r2["new"] == 0, r2)
    check("目标目录已建立", os.path.isdir(os.path.join(td, "photos")))

print("== 2. 短信 CSV 导出（无 root 走 ContentProvider） ==")
with TmpDir() as td:
    ab = AutoBackup(adb, SERIAL)
    r = ab.run(["sms"], td)
    csvs = [f for f in os.listdir(td) if f.startswith("sms_")]
    check("生成短信 CSV", len(csvs) == 1 and r["new"] == 1, csvs)
    if csvs:
        text = open(os.path.join(td, csvs[0]), encoding="utf-8-sig").read()
        check("CSV 含号码与内容", "10086" in text and "余额" in text, text[:100])

print("== 3. 缺失目录不报错；未 root 的微信优雅降级 ==")
with TmpDir() as td:
    ab = AutoBackup(adb, SERIAL)
    r = ab.run(["screenshots", "wechat"], td)
    check("不存在的目录被跳过（不抛异常）", isinstance(r["new"], int), r)
    check("微信未 root 记为失败项", any(k == "wechat" for k, _ in r["failed"]), r["failed"])

print("== 4. 剪贴板通道 ==")
ctl = InputController(adb, SERIAL)
clip_ok, clip_text, clip_ch = ctl.clipboard_get()
check("读取手机剪贴板（cmd clipboard）", clip_ok and clip_text == "abc123", (clip_ok, clip_text, clip_ch))
check("写入手机剪贴板（Clipper 广播）", ctl.clipboard_set("abc") is True)
check("全选 Ctrl+A 发送成功", ctl.select_all() is True)
for fn_name in ("copy", "paste", "cut", "undo", "redo"):
    try:
        getattr(ctl, fn_name)()
        check(f"编辑键 {fn_name} 不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check(f"编辑键 {fn_name} 不抛异常", False, str(exc))
decoded = _extract_clip_text('Broadcast completed: result=0, data="YWJj5rWL6K+V"')
check("ADBKeyboard 剪贴板回文解析（Base64）", decoded == "abc测试", decoded)

print("== 5. 摘要行 ==")
s = summarize({"new": 1, "skipped": 2, "failed": [], "stopped": False})
check("summarize 中文摘要", "新增 1" in s and "跳过 2" in s, s)

print()
print(f"结果：通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
