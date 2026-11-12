---
name: talking-head-auto-editor
description: 当用户要剪口播、访谈、课程，删除已核实的口误与重复take、缩短停顿、自动挑选或制作B-roll/AI素材、加字幕、基础调色、输出成片或交给Windows剪映/CapCut、达芬奇Resolve、Premiere继续剪辑时使用。支持本地逐词转写、FFmpeg实际渲染和XML/EDL/SRT交付。剪映草稿为版本敏感的试验适配；不把静态文件验证称为原生软件已导入。
---

# 口播自动剪辑与跨软件交付

`{baseDir}` 是此文件所在目录。主入口为 `scripts/editor.py`，标准库 + FFmpeg/FFprobe 即可运行；本地 ASR 是可选依赖。相对媒体路径始终锚定 **job.json 所在目录**，不锚定终端当前目录。

## 先确定实际交付

沿用本次用户已给出的平台、受众、时长、画幅、剪辑力度、调色方向和素材授权，合理的技术选择直接完成，不重复询问。素材不足只阻塞依赖该素材的步骤。用户只让配置技能时，不花额度制作视频或图像。

默认交付：实际 MP4 + SRT + job.json + 可继续编辑的 XML/EDL + 兼容性/审核记录。用户只要计划或只要时间线时遵守该范围。

先读 [job协议与兼容性](references/job-and-interchange.md)，Windows 运行读 [安装与原生连接](references/windows-setup.md)。

```sh
python "{baseDir}/scripts/editor.py" doctor
```

doctor 只报告本地工具能力，**不会连接 Resolve，不证明目标剪辑软件可用**。FFmpeg 若缺 `subtitles`，不能交付烧录字幕；可以输出独立 SRT 并明确未烧录。不要编造缺失工具的成功状态。

## 1. 转写和口播主剪辑

### 本地转写或复用真实逐词稿

有与源片对应的逐词稿直接复用。没有词稿且用户已授权本地转写时，使用已准备的模型目录：

```sh
python "{baseDir}/scripts/editor.py" transcribe input.mp4 --model "D:/Models/faster-whisper-small" --language zh --out work/words.json
```

该入口仅加载本地模型，`local_files_only=True`，不下载模型、不上传音频。`requirements-optional.txt` 只用于按需安装本地 ASR 依赖；模型另行明确准备。ASR 机器稿仍需核对人名、数字、专有名词和疑似幻觉。只有段落时间戳不能冒充逐词时间戳。

### 按词间空隙制作剪辑底稿

```sh
python "{baseDir}/scripts/editor.py" plan input.mp4 --transcript work/words.json --out work/job.json --max-pause 1.2 --keep-pause 0.4
```

自动步骤只缩短真实词时间戳之间、同时经FFmpeg测得安静的长空隙，保留呼吸/停顿余量；词稿漏词但仍有声音的间隔不会仅凭词稿删掉。不自动删除说出的词。比较脚本、逐词稿和实际声音，选择完整、语义正确的 take，再修改 `clips` 的源片区间；口误、重复、口头禅是否删除由上下文决定。不得仅凭音量、静音或文件名宣称某句话错误。

无词稿时可运行不带 `--transcript` 的 `plan`：它检测静音但默认**保留全片**，把候选记入 `planning.silence_candidates`。明确选择缩短实测静音时才使用 `--trim-measured-silence`，仍须听切口和语义停顿。不要把静音检测描述为文字识别。

所有源文件保留。拼接后字幕必须映射到输出时间，不能继续用删减前的原片时间。

## 2. 自动选配及制作素材

完整流程见 [素材选择与生成](references/asset-selection.md)。这一步负责真正选配素材，不止交一段提示词。

```sh
python "{baseDir}/scripts/editor.py" inventory user_assets --out work/asset-inventory.json --frames-dir work/asset-evidence
```

依次执行：

1. 根据已核实转写建立 `asset-needs.json`：标出输出时间、要解释的主张/动作、需要显示的主体、时长与画幅。保留以说话人表情为重点的段落，不为每句话机械覆盖B-roll。
2. 优先盘点用户素材。查看脚本提取的关键帧，填写实际看到的主体/动作/场景；按语义匹配40%、主体证据20%、画幅与清晰度15%、可用时长10%、权利与来源15%评分。权利不明、主体不符、画面不可读的素材不因总分高而自动采用。不要把脚本的元数据盘点称为已完成视觉判断。
3. 输出 `asset-plan.json`：每个候选的证据、评分、取舍和真实路径；同时标出缺口。使用 `examples/asset-plan.example.json` 的字段，不捏造已观察内容。
4. 缺口需要AI素材且本次授权允许制作时，优先使用当前已连接、能完成任务的图像/视频生成工具（如 image_gen、Runway），调用前读相应工具要求/技能。沿用用户授权，不为已授权的正常制作重复确认。**本配置任务本身不授权花额度做样片**。
5. 生成返回后，实际下载/保留文件、查看画面、测量尺寸与时长。只有实际产出的素材才能标记 `generated` 并以真实本地路径写入 `job.broll`。图像可作为静帧B-roll，脚本会按时间线时长持续显示；不要把提示词、失效URL或空占位路径称为已生成素材。
6. 无生成服务、额度或素材不可用时，在计划中明确缺口，继续可独立完成的口播主剪辑。不能悄悄上传用户素材到另一个服务，也不自动发布内容。

## 3. 字幕、调色和实际渲染

在 job 中填好输出时间的 `subtitles`、`broll` 和 `grade`。`neutral/warm/cool/contrast` 为轻微基础处理，不是自动电影调色保证。用户选择本地 `.cube` 时，必须知道其输入空间与素材一致。**未知Log/HDR不盲转Rec.709、不盲套LUT**；脚本会拦截明确标记PQ/HLG的源片，先取得明确正确的SDR副本或走原生色彩管理。

```sh
python "{baseDir}/scripts/editor.py" render work/job.json --out output/first-cut.mp4
```

主音轨在B-roll期间保留，B-roll原声默认不混入。渲染实际执行主片切片/缩放、B-roll cover覆盖、字幕烧录、所选基础调色/本地LUT和两遍测量响度。内部切片用无损FFV1/PCM，最终H.264/AAC，避免每个阶段反复有损转码。纯静音不可测响度会明确报告，不伪造LUFS结果。用 `--no-burn-subtitles` 只保留SRT。

技术验证包括实际输出帧数、时长、音轨和完整解码；结果在 `.render.json`。这些不是审美、语义或听感通过。

## 4. 跨软件交付和可选Resolve桥接

```sh
python "{baseDir}/scripts/editor.py" export work/job.json --out-dir output/editable
python "{baseDir}/scripts/editor.py" export work/job.json --out-dir output/jianying --jianying-template "D:/Projects/blank-same-version"
```

导出基本切点与源音轨的FCP7 XML（Resolve/Premiere导入候选）、XML第二轨B-roll、V1 CMX3600 EDL和独立SRT。`compatibility.md` 明确损失：**FFmpeg烧录字幕、cover裁切、LUT/调色和响度不会凭空还原为XML里的原生效果**。需要外观一致时交实际MP4，需要可编辑性时在软件里重链接并复核相应设置。

剪映JSON是非官方、版本敏感的 best effort。优先使用用户**同版本**空白草稿的独立副本；原模板不会被修改，不解密、不承诺适配所有版本。必须在实际软件里检查并保存后重开，才可称为原生验收通过。

可选本机Resolve连接：

```sh
python "{baseDir}/scripts/resolve_bridge.py" --timeline-xml output/editable/resolve-premiere.xml --dry-run
python "{baseDir}/scripts/resolve_bridge.py" --timeline-xml output/editable/resolve-premiere.xml --timeline-name "口播-v1"
python "{baseDir}/scripts/resolve_bridge.py" --timeline-xml output/editable/resolve-premiere.xml --timeline-name "口播-v2" --drx "D:/Looks/approved.drx" --drx-track 1
```

dry-run只检查输入，绝不是已连接。真实桥接需要本机Resolve正在运行、对应版本/许可开放Python脚本，以及正确的本地模块/库路径。不要硬称所有版本都需要或都不需要Studio，按本机官方Developer/Scripting文档核实。API成功也需在UI验证画面、声音、字幕、链接和所选DRX结果。没有本机条件时照常交XML/SRT，不假装远程操作了用户Windows软件。

## 5. 配音、音效与音乐

需要新配音、拟音、环境声、背景音乐或电影配乐时，读取已安装的 audio-production-director 及 [音频交接](references/audio-production-handoff.md)。按最终输出时间线实际制作并混音，再生成新的合成 MP4 或带音轨的 XML；不能把写好声音提示词当成已制作完成。保留或替换原声要明确，避免双重口播。

## 6. 交给效果审核纠错

存在 `video-review-autofix` 时读取其入口；否则自行执行实际媒体检查。每个问题记时间点、证据、严重度和修复动作。修复主job/素材/字幕后输出新版本，最多2轮，保留原片与上一版本。不得凭静态报告宣布审美通过，不自动扩大删改剧情范围。切口、口型、字幕、素材主张和肤色必须看/听实际结果；无法听音频就明确该项未检查。

交付时简洁说明：实际生成了哪些文件、哪些软件仅完成静态验证、哪些素材或审核仍缺失。已授权且可以完成的工作要做完，不停在“可以做”的描述。
