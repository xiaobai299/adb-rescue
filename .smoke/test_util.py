# -*- coding: utf-8 -*-
"""测试公用工具。

沙箱/杀软环境下 Python 的 tempfile.mkdtemp/TemporaryDirectory 存在两类问题：
  1) 目录清理（chmod + rmtree）可能被拒绝并抛 PermissionError；
  2) 向 mkdtemp 创建的目录内写文件可能被拒绝（沙箱规则）。
这里改用 os.makedirs 自建唯一目录（实测可正常读写），清理失败仅警告，
不影响断言结果。
"""

from __future__ import annotations

import os
import shutil
import uuid


class TmpDir:
    """临时目录上下文管理器（os.makedirs 创建，清理失败不抛异常）。"""

    def __init__(self, prefix: str = "rescue_test_"):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
        os.makedirs(base, exist_ok=True)
        self.path = os.path.join(base, f"{prefix}{uuid.uuid4().hex}")
        os.makedirs(self.path, exist_ok=True)

    def __enter__(self) -> str:
        return self.path

    def __exit__(self, *_exc) -> bool:
        try:
            shutil.rmtree(self.path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        return False
