# 能力、依赖与自动化范围

当前可靠执行层：Python标准库 + FFmpeg/FFprobe；本地ASR可使用已准备的faster-whisper环境/模型；画布为单文件离线HTML。现有八技能提供具体操作，不要求用户逐个调用。

preflight.py只读报告二进制/滤镜、技能、可选模型和Resolve状态。ready、missing、not_checked、not_connected分开。软件模块存在不等于已连接；Resolve只有真实scriptapp响应后才可记连接，并且不得为探测创建或保存工程。

宿主连接的生成工具由当前代理调用，凭据保持在工具/服务中；不读取或导出OAuth令牌。Python CLI的动作队列不是独立服务凭据，离开代理会话后不会假装继续调用MCP。

Windows run-production.ps1提供固定命令启动/续跑本地阶段。若要跨会话长期无人值守，还需要实际部署的代理执行器、可用服务凭据与任务运行环境；本次不会用空配置声称这些已就绪。

当前云端没有接入用户Windows桌面。默认走已验证的FFmpeg成片与MP4/SRT/WAV/XML/EDL交付，避免把可选剪映/Resolve原生控制当整个流程的必需依赖。原生工程和DRX效果在本机另验。

配置与测试不花生成额度。真实项目能否完成还取决于实际素材、服务权限/额度、模型结果与可用的真实看图/听音能力；缺项保持可恢复状态。
