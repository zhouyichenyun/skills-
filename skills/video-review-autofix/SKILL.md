---
name: video-review-autofix
description: Review a rendered talking-head or AI-assisted video, detect timestamped technical defects, perform authorized non-destructive repairs, rescan, and hand off unresolved visual or editorial defects. Use for 成片审核、自动纠错、字幕质检、口播后期质检、黑帧卡顿静音检查、音量修复、修完复检. Includes FFmpeg/FFprobe checks, bounded two-pass loudness repair, conservative SRT fixes, and edit-job rerender requests. Does not claim lip-sync, semantic, visual or desktop-editor verification from metadata.
---

# 成片审核与自动纠错

本技能维护“真实成片检查 → 定位责任层 → 在新文件中修复 → 复检”的闭环。适用于口播、B-roll、AI 补充画面、字幕、调色后的成片。不要把脚本运行成功或机器检测全绿写成审美通过。

`{baseDir}` 是此文件所在目录。依赖 Python 3.10+、FFmpeg、FFprobe；无需额外 Python 包或 API Key。脚本为原创标准库实现，不联网、不上传、不生成素材、不改源片。

## 先承接已授权任务

- 复用用户已确定的脚本、剪辑 job、字幕版本、画幅、平台与音量目标。用户已要求自动剪辑/自动纠错时，执行其范围内可逆的技术修复，不重复要求确认每个步骤。
- 对未指定的口播交付，暂用 `-16 LUFS`、真峰值上限 `-1.5 dBTP`，在报告记录为本项目工作目标；这不是所有平台的强制规范。
- 只审核用户指定文件，或上游编辑器明确交接的成片。不遍历无关私人目录。
- 纯技术问题与剧情、字幕语义、创意停顿、风格判断分开记录。黑场、静帧和停顿先看/听再决定，不凭检测命中盲删。

## 1. 机器检查和取证

```powershell
py "{baseDir}/scripts/video_qc.py" scan "final.mp4" --srt "captions.srt" --out "qc-v1" --extract-frames
```

Linux/macOS 使用 `python3` 或当前 Python 解释器。`--out` 必须是新目录；不覆盖已有输出。文件路径通过参数数组传给 FFmpeg，不经 shell 拼接。

产物：`report.json`、`report.md`、`commands.json`，启用取证时还有 `frames/*.jpg`。先读 JSON，再查看实际导出的帧；关键切口和同步问题需要读取该处短片或播放，不只看离散抽帧。

机器覆盖：

| 项目 | 实际证据 | 限制 |
|---|---|---|
| 媒体可探测、完整解码 | FFprobe JSON、FFmpeg 返回码与解码错误日志 | 解码通过不代表内容正确；检查主视频/主音轨 |
| 黑帧、冻结 | 全片 FFmpeg `blackdetect` / `freezedetect` 候选时间段 | 夜景、字卡、静态图、创意黑场可能合法 |
| 长静音 | 主音轨低于 -45 dB、持续 ≥0.7s 的区间 | 不是口误识别；呼吸、停顿和留白可能有意 |
| 响度、真峰值 | 主音轨 `loudnorm` 实测 | 很短或全静音片不可强称响度合格 |
| 音画结构 | 两流声明起点、时长差，阈值为至少 0.08s 或两帧 | 不是口型同步检测；元数据缺失则明确 partial |
| SRT | UTF-8、格式、起止、越界、重叠、空文本、阅读速度 | 仅外部字幕；默认18字符/秒为启发阈值，不检查语义或烧录位置 |

`coverage` 每项必须是 checked / partial / skipped / failed / blocked 等实际状态。缺检测器、命令失败或媒体不可读不能当 clean。CLI 退出码 `0` 表示无机器发现，`10` 表示存在发现，`3/4/5` 表示输入、操作或依赖失败。`10` 不是程序崩溃，继续读取报告。

## 2. 自动修复、重检与回退

```powershell
py "{baseDir}/scripts/video_qc.py" repair "final.mp4" --srt "captions.srt" --out "qc-repair-v1" --normalize-loudness --fix-subtitles --max-rounds 2 --extract-frames --job "job.json"
```

默认不重编码视频。根据真实问题和用户授权，可明确加 `--remux`（主音画流重封装为 MP4）或 `--transcode`（H.264/yuv420p + AAC 交付转码）；两者互斥。不是一看到错误就重编码，丢失的内容和细节不会被转码复原。

脚本实际自动修复：

- 响度目标偏差或真峰值超限：先测量，再将完整测量值送入第二遍 `loudnorm`；输出48kHz AAC并重新测量。仅裁掉滤镜引入的音频尾部填充，不按视频时长盲切原有音尾。视频可直接流拷贝。
- SRT：删除确无可见文本的空 cue；仅对不超过0.25s的字幕结尾短越界截到媒体末尾。保留原文件和每条改动日志。
- 重叠、过快、负时长、整条越界、长越界、格式损坏：保留并报错，不擅改台词、瞎延长、压到片尾或凭空识别原文。需按实际语音/稿件修正后重新渲染。

输出 `repair.json` 包含 before/after 报告、每轮命令及测量、原片哈希、源文件是否保持不变、当前可用输出与剩余问题。最多两轮；仍不通过就留下可定位的返工请求，不无限重跑。

**外部 SRT 修复不会改变已经烧录在视频像素里的字幕。** 必须更新新 job 的字幕内容，交给剪辑技能重渲染，再对实际编码成片重新扫描。不得仅因侧边字幕通过就声称视频字幕已修好。

失败时保留上一个检查过的文件；失败的部分输出在日志标明，不交付为最终片。不覆盖用户原片或原字幕。

## 3. 视觉和听觉审核

读取 [visual-review.md](references/visual-review.md)，查看最终渲染文件而非只看源素材。按任务实际查看：

剧情片、规则怪谈、悬疑开场及用户反馈“看不懂/莫名其妙”时，必须先执行 [首看叙事审核](references/narrative-first-view.md)。技术检查、素材可用、叙事通过和下载可用分别给结论；叙事硬失败或未完成正常速度视听审核时，不能用抽帧、指标分数、提示词意图替代“可交付”。

1. 正常速度检查口播完整性、信息节奏、音乐与人声关系、字幕是否跟话。
2. 切点局部检查断词、吞尾音、跳姿、口型、B-roll覆盖、色彩切换、字幕安全区。
3. AI素材检查脸/手/物件/文字/背景稳定性；不合适的镜头仅替换该素材，不无故重做已合格镜头。
4. 调色检查源色彩空间、曝光、白平衡、肤色与镜间匹配，不把橙青 LUT 等同电影感。

若当前工具只能查看抽帧，记录“抽帧已审，完整播放/听觉待验证”。没有实际声音取证不能声称听过，没有视频播放证据不能声称完整看过。不要输出虚构的综合质量分。

使用首看模板记录人物当前目的、空间关系、可见因果、异常基准与悬念问题。任一关键答案只能从剧本补出，或关键敲击/对白不知来自哪里、由谁听见和回应，均为叙事硬失败；退回编导/导演/剪辑，不能靠增加剪切频率或重跑生成自动解除。

## 4. 返工交接与统一 job

`--job` 可接收版本1的剪辑 job。源路径相对 job 所在目录：

```json
{
  "version": 1,
  "name": "口播项目",
  "fps": 25,
  "width": 1920,
  "height": 1080,
  "clips": [{"source": "media/talk.mp4", "start": 0, "end": 12, "id": "A01"}],
  "broll": [{"source": "media/broll.mp4", "at": 3, "duration": 2, "source_start": 0, "fit": "cover"}],
  "subtitles": [{"start": 0.2, "end": 2, "text": "口播字幕"}],
  "grade": {"preset": "neutral", "lut": null},
  "audio": {"target_lufs": -16}
}
```

脚本写 `rerender-request.json`，把有时间信息的问题映射回顺序拼接 clips 与其上覆盖的 broll，记录源片位置、建议修改字段及动作。无时间的整片问题明确保留为全片检查。它不改 job、不执行剪辑器、不声称重渲染完成。

按照 [repair-handoff.md](references/repair-handoff.md) 修改**新版本** job：编导负责稿件/信息顺序，导演负责画面和素材选择，提示词技能负责失败 AI 镜头的局部修正，自动剪辑技能负责重剪/重烧字幕/调色与编码。重渲染后回到步骤1。

支持处理从 Windows 剪映、达芬奇或其他软件导出的实际视频和 SRT；此技能不直接连接用户桌面软件。软件工程格式与调色交接由剪辑技能负责，不把导出文件或生成 LUT 写成已在达芬奇验证。

## 验证本技能

```powershell
py "{baseDir}/scripts/selftest.py" --out "qc-synthetic-test"
```

自测生成真实合成视频、坏字幕和故障输入：正常片、黑/冻/静音、响度修复、短/长越界字幕、保留原片、返工时间映射以及失败关闭。通过只证明这些合成用例，不证明某条用户成片的审美或桌面软件验收。
