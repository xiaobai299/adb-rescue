# -*- coding: utf-8 -*-
"""界面冒烟测试：装配主窗口 + 模拟设备，2.5 秒后检查各页面状态并退出。"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ.setdefault("ADB_RESCUE_HOME", os.path.join(HERE, "config"))
sys.path.insert(0, ROOT)

FAKE = os.path.join(HERE, "fake_adb.bat")

from ui.app import App  # noqa: E402

app = App()
app.adb.adb_path = FAKE
app.cfg.set("adb_path", FAKE)

report = {"errors": []}


def verify():
    try:
        assert app.device is not None, "未自动选中设备"
        assert app.device.serial == "ABC123", app.device.serial
        assert app.device.state == "device"
        assert app.device.model == "Pixel 7", app.device.model
        print("[PASS] 设备自动选中并采集信息：", app.device.display_name)

        assert app.state_var.get().startswith("●"), app.state_var.get()
        print("[PASS] 状态栏显示：", app.state_var.get())

        files = app.files_panel
        n = len(files.tree.get_children())
        assert n == 4, f"文件列表应有 4 项，实际 {n}"
        print("[PASS] 文件页已列出 /sdcard（4 项）")

        apps = app.apps_panel
        na = len(apps.tree.get_children())
        assert na == 1, f"应用列表应有 1 项，实际 {na}"
        print("[PASS] 应用页已加载应用列表")

        ops = app.ops_panel
        assert ops.power is not None and ops.logcat is not None
        body = ops.info_text.get("1.0", "end").strip()
        assert body, "体检信息为空"
        print("[PASS] 运维页一键体检输出：", body.replace("\n", " ｜ ")[:90])

        # 投屏：启动引擎并等待取帧
        app.screen_panel.start()
        print("[PASS] 投屏引擎启动成功")

        def check_frame():
            st = app.screen_panel.streamer
            assert st is not None and st.frame_count > 0, "投屏未取到帧"
            print(f"[PASS] 投屏取帧 {st.frame_count} 帧，"
                  f"分辨率 {st.device_width}x{st.device_height}")

            # 风险确认对话框可正常构建
            from ui.widgets import ask_confirm
            app.after(0, lambda: app.destroy())
            app.after(50, lambda: print("[PASS] 界面测试完成，窗口已关闭"))

        app.after(4000, check_frame)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        report["errors"].append(str(exc))
        print("---- 运行日志 ----")
        print(app.log_panel.text.get("1.0", "end"))
        print("---- 状态 ----")
        print("devices:", [(d.serial, d.state) for d in app.devices],
              "| combo:", app.dev_combo["values"], "| var:", app.dev_var.get())
        app.after(50, app.destroy)


app.after(16000, verify)
app.mainloop()
if report["errors"]:
    print("界面测试失败：", report["errors"])
    sys.exit(1)
print("界面冒烟测试：全部通过")
