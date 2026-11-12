# Windows 口播与电影短片工作流

一个总调度入口和八项执行技能，组成可恢复的AI影视/口播制作链。主要目标是 Windows 上的剪映与 DaVinci Resolve；默认交付为 MP4、SRT、WAV 和可编辑时间线。

| 技能 | 职责 |
| --- | --- |
| [ai-production-orchestrator](skills/ai-production-orchestrator/SKILL.md) | 总制片与执行调度：项目状态、依赖、资产版本、预算、任务回执、断点续跑、返工和交付 |
| [talking-head-writer](skills/talking-head-writer/SKILL.md) | 选题、受众、口播结构、逐句稿、保留 / 删除决策、字幕词表、B-roll 素材需求 |
| [cinematic-director](skills/cinematic-director/SKILL.md) | 机位、景别、调度、表演、镜头覆盖、声音、视觉风格和调色意图 |
| [talking-head-auto-editor](skills/talking-head-auto-editor/SKILL.md) | 转写或读取时间稿、剪辑计划、B-roll、字幕、基础调色 / LUT、MP4 与时间线交接 |
| [video-review-autofix](skills/video-review-autofix/SKILL.md) | 技术检测、真实画面复核、有依据的自动修复、重检和返工记录 |
| [audio-production-director](skills/audio-production-director/SKILL.md) | 匹配剪辑需求制作配音、音效与音乐，完成对齐、人声避让、混音和分轨 |
| [story-canvas-producer](skills/story-canvas-producer/SKILL.md) | 用可视化画布管理角色定妆、场景、故事连续性和有限轮次候选筛选 |
| [video-shots](skills/video-shots/SKILL.md) | 从参考视频测量切镜、抽帧、分析景别 / 运镜 / 节奏并导出报告 |
| [cinematic-video-prompt-engineer](skills/cinematic-video-prompt-engineer/SKILL.md) | 为缺少的 AI 素材编写镜头、光影、表演和连续性提示词 |

## 用法

上传口播、逐词稿或已有素材后说：

> 按 Windows 剪映 / 达芬奇流程剪这条口播：删掉重复和多余停顿，自动挑选或生成合适的补充画面，加字幕并统一色彩，审核修正后交付成片和可编辑时间线。

技能按任务自行交接，不会把“写好了素材提示词”报告成“素材已生成”，不会把“机器检测完成”报告成“口型、内容与审美全部通过”。

端到端任务从总调度开始：它创建项目，自动执行本地阶段，并持续调用已有技能与生成/审核工具消费动作队列。口播、故事和混合制作按各自依赖图运行。独立CLI持久化外部动作，不能在没有宿主代理或独立服务执行器时假装后台生成。

## 本机使用

见 [Windows 安装与软件连接](windows/README.md)。已有 Python 时可运行 windows/setup.ps1；它会把九项技能复制到当前 Windows 账户的 Codex 技能目录。FFmpeg / FFprobe、可选的本地语音识别模型及桌面剪辑软件需在执行机器上可用。项目启动与续跑使用 windows/run-production.ps1，外部动作由宿主代理继续执行。

## 实际适配范围

- MP4 与 SRT 可用于剪映及多数剪辑软件继续制作。
- 达芬奇用 XML / EDL 交接剪辑；字幕单独导入 SRT。原生脚本桥接需本机软件和可用 scripting 接口。
- 剪映原生草稿依赖当前版本模板及正确的 Windows 素材路径；未经同版本导入测试，不标记为已兼容。
- FFmpeg 调色和 LUT 可作用于渲染成片；它们不等于达芬奇原生节点。Log / HDR 需要正确的输入色彩空间。
- 本地语音识别无需 API Key，但需准备模型。素材生成依赖实际连接的服务与额度。
- 声音生成复用已连接服务，音乐取决于工作区相应权限；本地混音、分轨和 WAV 交接不需要生成服务。
- 角色与故事默认使用离线分镜画布；人物建模指影视角色设定、多视图定妆与一致性。Canva 仅在实际支持的可编辑页面范围内使用。

本仓库提供代码和协议，未远程连接用户 Windows 桌面。合成媒体测试与软件 UI 导入验证分开记录，详见 [验证记录](VERIFICATION.md)。

## 工作流、版本与许可

[工作流](WORKFLOW.md) · [统一任务格式](schemas/postproduction-job.schema.json) · [来源与文件校验](sources.lock.json)

原有两项技能保留固定上游版本。导演技能为 MIT 上游加中文口播交接适配；编导、口播执行器、审核纠错、音频制作和故事画布为按统一协议制作的原创技能。第三方许可保留在各技能目录中，后续更新不自动追随上游。

总调度及其版本账本、执行器、适配器和交付机制同为本仓库原创；状态与素材放项目目录，技能源代码和用户任务数据分开维护。

用户素材、模型、私有路径、音轨、关键帧和真实任务产物不提交到这个公开仓库。
