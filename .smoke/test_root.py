# -*- coding: utf-8 -*-
"""Root 层冒烟测试：su 检测、root shell、流式导出、完整备份、数据库直读。

使用 .smoke/fake_adb.py 内置的模拟 root 文件系统（含真实 sqlite 数据库），
不需要真实手机。验证 root 模块在"已 root + Magisk"场景下的全部关键路径。
"""

from __future__ import annotations

import csv
import os
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
# 把临时目录固定到项目内（沙箱/杀软可能禁止删除系统临时目录，导致清理失败）
_local_tmp = os.path.join(HERE, "tmp")
os.makedirs(_local_tmp, exist_ok=True)
os.environ["TMP"] = _local_tmp
os.environ["TEMP"] = _local_tmp
tempfile.tempdir = _local_tmp
sys.path.insert(0, ROOT)

from core.adb import AdbClient  # noqa: E402
from core.files import FileManager  # noqa: E402
from core.root import RootManager  # noqa: E402
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


print("== 1. root 状态检测 ==")
rm = RootManager(adb, SERIAL)
check("su 可用", rm.su_available() is True)
st = rm.get_status(refresh=True)
check("方法为 Magisk", st.method == "magisk", st.method)
check("Magisk 版本", "28100" in st.magisk_version, st.magisk_version)
check("SELinux 为强制", st.selinux == "Enforcing", st.selinux)
check("/data/data 可读", st.data_readable is True, st.detail)

print("== 1b. adb.collect_root_info 批量采集 ==")
dev = adb.collect_info(SERIAL)
adb.collect_root_info(dev)
check("rooted 标志", dev.rooted is True)
check("root 方式 magisk", dev.root_method == "magisk", dev.root_method)
check("magisk 版本字段", "28100" in dev.magisk_version, dev.magisk_version)

print("== 2. root shell 与文件读取 ==")
out = rm.shell("id")
check("root shell 输出 uid=0", "uid=0" in out, out)
data = rm.read_binary("/data/app/~~a1==/com.demo.app-1/base.apk")
check("root 读取 APK 字节", data.startswith(b"PK\x03\x04"), data[:8])
secret = rm.read_binary("/data/data/com.demo.app/files/secret.txt")
check("root 读取应用私有文件", b"demo secret data" in secret, secret)

print("== 3. root 目录浏览 ==")
lines = rm.list_dir("/data/data/com.demo.app")
check("列出应用数据目录", any("files" in ln for ln in lines), lines)

print("== 4. 流式文件下载 ==")
with TmpDir() as td:
    local = os.path.join(td, "secret.txt")
    st = rm.stream_file("/data/data/com.demo.app/files/secret.txt", local)
    check("stream_file 成功", st["ok"] and st["bytes"] > 0, st)
    check("内容一致", open(local, encoding="utf-8").read() == "demo secret data\n")

print("== 5. 目录流式打包（tar.gz） ==")
with TmpDir() as td:
    tar = os.path.join(td, "app.tar.gz")
    st = rm.stream_tar("/data/data/com.demo.app", tar)
    check("stream_tar 成功", st["ok"] and st["bytes"] > 0, st)
    with tarfile.open(tar, "r:gz") as tf:
        names = tf.getnames()
    check("tar 内含 files/secret.txt", any(n.endswith("files/secret.txt") for n in names), names)

print("== 6. 应用完整备份（APK + 内部数据） ==")
with TmpDir() as td:
    res = rm.backup_app_full("com.demo.app", td, include_external=False)
    check("备份到 APK", len(res["apks"]) == 1, res)
    check("备份到数据包", len(res["archives"]) == 1, res["archives"])
    check("无错误", not res["errors"], res["errors"])
    apk = res["apks"][0]
    check("APK 内容正确", open(apk, "rb").read().startswith(b"PK\x03\x04"))
    arc = res["archives"][0]
    with tarfile.open(arc, "r:gz") as tf:
        names = tf.getnames()
    check("数据包含私有文件", any(n.endswith("secret.txt") for n in names), names)

print("== 7. 短信 / 通讯录 / 通话记录 数据库直读 ==")
with TmpDir() as td:
    sms_csv = rm.export_sms_root(td)
    rows = list(csv.reader(open(sms_csv, encoding="utf-8-sig")))
    check("短信导出行数 >1", len(rows) >= 2, rows)
    body_ok = any("root测试短信" in (r[3] if len(r) > 3 else "") for r in rows[1:])
    check("短信内容正确", body_ok, rows)

    con_csv = rm.export_contacts_root(td)
    rows = list(csv.reader(open(con_csv, encoding="utf-8-sig")))
    check("联系人含张三", any(len(r) > 1 and r[1] == "13800000000" and r[0] == "张三" for r in rows), rows)

    call_csv = rm.export_calllog_root(td)
    rows = list(csv.reader(open(call_csv, encoding="utf-8-sig")))
    check("通话记录含 10010", any(len(r) > 2 and r[2] == "10010" for r in rows), rows)

print("== 8. FileManager root 回退（浏览 /data） ==")
fm = FileManager(adb, SERIAL, root=rm)
entries = fm.list_dir("/data")
check("root 回退浏览 /data", len(entries) > 0, [e.name for e in entries][:5])
with TmpDir() as td:
    # 通过 root 通道拉取 /data 下私有文件
    saved = fm.pull_file("/data/data/com.demo.app/files/secret.txt", td)
    check("root 拉取 /data 文件", os.path.isfile(saved)
          and open(saved, encoding="utf-8").read() == "demo secret data\n", saved)

print("== 9. 系统级操作不抛异常 ==")
for name, fn in [
    ("关闭动画", lambda: rm.set_animations(0)),
    ("恢复动画", lambda: rm.set_animations(1)),
    ("强制 GPU", lambda: rm.force_gpu(True)),
    ("屏幕常亮", lambda: rm.keep_screen_on_root()),
    ("SELinux 宽容", lambda: rm.set_selinux("Permissive")),
    ("SELinux 恢复", lambda: rm.set_selinux("Enforcing")),
    ("冻结应用", lambda: rm.freeze("com.android.browser", True)),
    ("解冻应用", lambda: rm.freeze("com.android.browser", False)),
    ("系统应用卸载", lambda: rm.uninstall_for_user("com.android.browser")),
    ("恢复系统应用", lambda: rm.restore_system_app("com.android.browser")),
    ("进程列表", lambda: rm.process_list_root()),
]:
    try:
        fn()
        check(name, True)
    except Exception as exc:  # noqa: BLE001
        check(name, False, str(exc))

sysapps = rm.list_system_packages()
check("系统应用列表含 Magisk", "com.topjohnwu.magisk" in sysapps, sysapps)

print()
print(f"结果：通过 {ok} 项，失败 {fail} 项")
sys.exit(1 if fail else 0)
