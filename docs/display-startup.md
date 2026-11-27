# 显示启动生命周期

`console_display_lifecycle.py` 在加载 IRIS 前保存原厂 ESD 模式，使用 root 私有、限本次 boot 的运行租约交接检查职责。原厂同步 TE 路径忽略 esd_enabled，可在固件初始化完成前误报 PANEL_DEAD。服务运行时以真实 IRIS/DCS 状态检查判定健康，而不是软件模拟 ESD 成功结果。

控制接口使用单次 os.write；零/短写立即失败，不使用缓冲文本写入重试。卸载自身模块后恢复原模式；ExecStopPost 再做幂等恢复。租约不匹配、外部状态被改变、模块仍被占有时不强写内核状态。

系统服务失败后5秒自动重试，5分钟最多2次启动，FailureAction/StartLimitAction为none，不自动整机重启。没有强制卸载模块、裸写内核内存或无限重复复位。

在 Linux root 的隔离临时目录运行：

```sh
python3 tests/test_display_lifecycle.py
```

测试写入的是临时普通文件，并非真实 sysfs。服务启停的实机结果不等于物理显示/冷启动验收。
