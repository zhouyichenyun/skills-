# Windows：剪映 / DaVinci Resolve

这套技能同时提供通用 MP4 / SRT 和可继续编辑的时间线。这里的安装脚本只安装技能，不会安装剪映或达芬奇，也不会远程控制你的电脑。

## 本机安装

下载或克隆本仓库，在仓库根目录运行 PowerShell：

    .\windows\setup.ps1

已有旧版本时，可使用 -Update；旧技能会先备份到技能目录旁的 skill-backups。可选 -InstallLocalASR 安装本地转写依赖到仓库 .venv。已有逐词稿或 SRT 可以跳过转写。Python 需 3.10+，FFmpeg/FFprobe 需可从 PATH 运行；video-shots 另需 Node.js 18+。

需要一起准备离线转写模型时运行：

    .\windows\setup.ps1 -ASRModel small

它会安装本地 ASR 依赖并显式从 Hugging Face 下载固定版本的公开模型到 models 目录，逐文件校验；不上传音频。small 适合比 tiny 更重视准确度的中文口播，准确度仍需校对专名和数字。快速测试可用 -ASRModel tiny；已经有本地模型时直接把模型目录交给转写命令，不重复下载。转写需使用 .venv\Scripts\python.exe。

脚本没有更改 PowerShell 执行策略。若机器策略阻止运行，可直接使用：

    python scripts/install_local.py

## 剪映

可直接把生成的 MP4 与 SRT 导入剪映继续剪辑。可编辑原生草稿需要使用当前剪映版本实际创建的空白工程模板，并核对素材路径和草稿目录；结构导出不等于已通过你的版本导入测试。默认生成新工程，不覆盖现有草稿。不会将达芬奇 XML 当成剪映原生项目。

## 达芬奇

通过自动剪辑技能导出的 XML / EDL 交接剪辑。字幕另用 SRT；FFmpeg 已烧录的字幕、B-roll、LUT 和响度不会自动变成达芬奇原生节点或可编辑效果。

如使用本机 Python 桥接，需安装并启动相应支持版本的 Resolve，配置其官方 Python scripting 模块和可用的脚本访问设置。外部脚本能力取决于 Resolve 版本、版本许可和本机配置；不要假定免费版与 Studio 均支持同一接口。先运行桥接的 dry-run / 连接诊断，再在新的项目中导入。此云端环境未连接你的 Windows 桌面。

原生调色可使用用户已有、匹配当前镜头的 DRX / LUT。输入为 Log / HDR 而色彩空间未知时先识别输入；不会直接套用 Rec.709 对比度参数声称完成专业色彩管理。

## 素材和模型

先从用户提供的素材库选择语义相关、画幅与时长适合且权利状态明确的 B-roll。缺口交给已连接的图像 / 视频生成工具制作，并实际检查生成结果。提示词草案与生成服务调用是不同步骤；没有生成结果的素材不能标记 ready。

音轨和媒体默认在本地处理。本机转写不要求 API Key，模型文件与 FFmpeg 需要事先准备。素材生成是否可用及是否消耗额度，以实际连接的服务为准。
