# -*- coding: utf-8 -*-
"""一键运行全部测试。

用法：
    python run_tests.py

会依次执行：
    1. .smoke/test_core.py    —— 核心层（设备检测/信息采集/解析/控制/投屏）冒烟测试
    2. .smoke/test_ui.py      —— 图形界面端到端测试（模拟 adb，约 25 秒）
    3. .smoke/test_dialog.py  —— 高风险操作二次确认弹窗测试

说明：本机 Python 需带 tkinter 且已安装 Pillow；测试使用 .smoke 下的模拟 adb，
不需要真实手机。
"""

from __future__ import annotations

import os
import subprocess
import sys

# 重定向输出时避免 GBK 代码页无法编码 ▶ 等字符
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SMOKE = os.path.join(HERE, ".smoke")

SUITES = [
    ("核心层冒烟测试", "test_core.py"),
    ("Root 层冒烟测试", "test_root.py"),
    ("自动备份与剪贴板测试", "test_backup.py"),
    ("界面端到端测试", "test_ui.py"),
    ("风险确认弹窗测试", "test_dialog.py"),
]


def main() -> int:
    try:
        import PIL  # noqa: F401
        import tkinter  # noqa: F401
    except ImportError as exc:
        print(f"环境不满足：{exc}\n请先执行：pip install -r requirements.txt")
        return 1

    passed = 0
    for name, script in SUITES:
        path = os.path.join(SMOKE, script)
        if not os.path.isfile(path):
            print(f"[跳过] {name}（缺少 {script}）")
            continue
        print("=" * 60)
        print(f"▶ {name}（{script}）")
        print("=" * 60)
        proc = subprocess.run([sys.executable, "-u", path], cwd=HERE)
        if proc.returncode == 0:
            passed += 1
            print(f"✔ {name}：通过\n")
        else:
            print(f"✘ {name}：失败\n")
    print("=" * 60)
    print(f"测试套件汇总：{passed}/{len(SUITES)} 个套件通过")
    return 0 if passed == len(SUITES) else 1


if __name__ == "__main__":
    sys.exit(main())
