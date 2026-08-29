"""ADB 手机救援控制工具 - 核心层

分层说明（自下而上，禁止反向依赖）：
    core.adb      —— 设备通信层：adb 定位、命令执行、设备枚举与信息采集
    core.screen   —— 投屏层：  帧采集与图像处理
    core.inputctl —— 控制层：  点击/滑动/文本/按键（只依赖通信层）
    core.files    —— 文件层：  远程浏览、数据导出、联系人/短信
    core.ops      —— 运维层：  APK、截图录屏、logcat、shell、电源
    ui.*          —— 界面层：  仅调用 core，不直接执行 adb
"""
