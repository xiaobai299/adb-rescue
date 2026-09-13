# -*- coding: utf-8 -*-
"""配置持久化：adb/scrcpy 路径、导出目录、投屏与操作偏好。

配置文件位置：%USERPROFILE%\\.adb_rescue\\config.json
"""

from __future__ import annotations

import json
import os
import threading

# 设置环境变量 ADB_RESCUE_HOME 可把配置目录改到别处（自动化测试用它隔离，避免污染用户配置）
CONFIG_DIR = os.environ.get("ADB_RESCUE_HOME") or os.path.join(os.path.expanduser("~"), ".adb_rescue")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS: dict = {
    "adb_path": "",
    "scrcpy_path": "",
    "export_dir": os.path.join(os.path.expanduser("~"), "Desktop", "ADB救援导出"),
    "last_serial": "",
    # 投屏
    "fps": 6,
    "quality": "标准",
    "rotation": 0,
    "max_width": 720,
    "auto_wake": True,
    # scrcpy 高帧率投屏
    "scrcpy_size": 0,           # 0 = 原生分辨率
    "scrcpy_fps": 0,            # 0 = 不限，跟随设备最大刷新率（120Hz 屏可跑满）
    "scrcpy_bitrate": "8M",     # 与直接运行 scrcpy 的默认一致（最稳）
    "scrcpy_codec": "h264",
    "scrcpy_no_control": True,  # True = 由本工具窗口控制；False = scrcpy 窗口直接控制（延迟更低）
    "scrcpy_turn_off": False,
    "scrcpy_no_audio": True,
    "scrcpy_low_latency": False,  # 低延迟模式：0 显示缓冲（取消抖动补偿，默认关闭）
    "scrcpy_tcpip": False,        # 无线 TCP/IP 连接（仅无线场景；USB 直连保持关闭）
    "scrcpy_audio": False,        # 同步手机音频到电脑
    # 自动备份（一插即救）
    "auto_backup_enabled": False,
    "auto_backup_items": ["photos", "screenshots", "sms", "contacts", "calllog"],
    "auto_backup_dir": "",            # 空 = 导出目录/AutoBackup
    "auto_backup_cooldown": 30,       # 同一设备两次自动备份的最小间隔（分钟）
    "auto_backup_last": {},           # serial -> 上次触发时间戳
    # 运维
    "confirm_destructive": True,
    "logcat_filter": "",
    "logcat_keyword": "",
}


class Config:
    _lock = threading.Lock()

    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            if os.path.isfile(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
                    self.data.update(json.load(fp))
        except Exception:
            pass
        # v2.0.2：scrcpy 默认码率回到与直接运行一致的 8M（12M 为 v2.0 旧默认，
        # 与"直接双击 scrcpy.exe 不卡"的行为对齐；用户后续可自行改回）
        if self.data.get("scrcpy_bitrate") == "12M":
            self.data["scrcpy_bitrate"] = "8M"
            self.save()

    def save(self) -> None:
        with self._lock:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            try:
                with open(CONFIG_FILE, "w", encoding="utf-8") as fp:
                    json.dump(self.data, fp, ensure_ascii=False, indent=2)
            except OSError:
                pass

    def get(self, key: str, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()
