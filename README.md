# Windows 口播与电影短片工作流

六项 Agent Skills，覆盖参考视频分析、编导策划、导演设计、AI 提示词、口播剪辑和成片审核。主要目标是 Windows 上的剪映与 DaVinci Resolve；通用交付为 MP4、SRT 和时间线文件。

| 技能 | 职责 |
| --- | --- |
| [talking-head-writer](skills/talking-head-writer/SKILL.md) | 选题、受众、口播结构、逐句稿、保留 / 删除决策、字幕词表、B-roll 素材需求 |
| [cinematic-director](skills/cinematic-director/SKILL.md) | 机位、景别、调度、表演、镜头覆盖、声音、视觉风格和调色意图 |
| [talking-head-auto-editor](skills/talking-head-auto-editor/SKILL.md) | 转写或读取时间稿、剪辑计划、B-roll、字幕、基础调色 / LUT、MP4 与时间线交接 |
| [video-review-autofix](skills/video-review-autofix/SKILL.md) | 技术检测、真实画面复核、有依据的自动修复、重检和返工记录 |
| [video-shots](skills/video-shots/SKILL.md) | 从参考视频测量切镜、抽帧、分析景别 / 运镜 / 节奏并导出报告 |
| [cinematic-video-prompt-engineer](skills/cinematic-video-prompt-engineer/SKILL.md) | 为缺少的 AI 素材编写镜头、光影、表演和连续性提示词 |

## 用法

上传口播、逐词稿或已有素材后说：

> 按 Windows 剪映 / 达芬奇流程剪这条口播：删掉重复和多余停顿，自动挑选或生成合适的补充画面，加字幕并统一色彩，审核修正后交付成片和可编辑时间线。

技能按任务自行交接，不会把“写好了素材提示词”报告成“素材已生成”，不会把“机器检测完成”报告成“口型、内容与审美全部通过”。

## 本机使用

见 [Windows 安装与软件连接](windows/README.md)。已有 Python 时可运行 windows/setup.ps1；它会把六项技能复制到当前 Windows 账户的 Codex 技能目录。FFmpeg / FFprobe、可选的本地语音识别模型及桌面剪辑软件需在执行机器上可用。

## 实际适配范围

- MP4 与 SRT 可用于剪映及多数剪辑软件继续制作。
- 达芬奇用 XML / EDL 交接剪辑；字幕单独导入 SRT。原生脚本桥接需本机软件和可用 scripting 接口。
- 剪映原生草稿依赖当前版本模板及正确的 Windows 素材路径；未经同版本导入测试，不标记为已兼容。
- FFmpeg 调色和 LUT 可作用于渲染成片；它们不等于达芬奇原生节点。Log / HDR 需要正确的输入色彩空间。
- 本地语音识别无需 API Key，但需准备模型。素材生成依赖实际连接的服务与额度。

本仓库提供代码和协议，未远程连接用户 Windows 桌面。合成媒体测试与软件 UI 导入验证分开记录，详见 [验证记录](VERIFICATION.md)。

## 工作流、版本与许可

[工作流](WORKFLOW.md) · [统一任务格式](schemas/postproduction-job.schema.json) · [来源与文件校验](sources.lock.json)

原有两项技能保留固定上游版本。导演技能为 MIT 上游加中文口播交接适配；编导、口播执行器和审核纠错为本次按统一协议制作的原创技能。第三方许可保留在各技能目录中，后续更新不自动追随上游。

用户素材、模型、私有路径、音轨、关键帧和真实任务产物不提交到这个公开仓库。
