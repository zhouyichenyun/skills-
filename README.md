# 视频分镜与电影提示词 Skills

已收录两个技能，分别用于视频拉片和电影级 AI 视频提示词编写。

| 技能 | 用途 | 依赖 |
| --- | --- | --- |
| [video-shots](skills/video-shots/SKILL.md) | 从视频检测切点、测量时长、抽取关键帧；结合视觉分析输出景别、运镜、画面和节奏报告 | Node.js ≥18、FFmpeg、FFprobe；无需模型 API Key |
| [cinematic-video-prompt-engineer](skills/cinematic-video-prompt-engineer/SKILL.md) | 将剧情、参考素材或镜头分析转为中文电影级生成提示词，控制表演、运镜、光影和跨镜连续性 | 提示词编写无需 API Key；可选章节读取脚本使用 Python 3 |

## 直接使用

- 上传视频后说：“用 video-shots 分析这个视频的分镜，给出关键帧、景别、运镜和节奏报告。”
- 提供剧情后说：“用 cinematic-video-prompt-engineer 写一段 15 秒电影级视频提示词，直接给成品。”
- 串联使用：“先拆解这段视频的镜头语言，再按这个主题写一组电影级生成提示词。”

视频分析依赖可读取的本地视频和视觉查看能力。脚本负责测量与抽帧，视觉模型负责画面理解；检测结果需要查看关键帧校正。此技能不含语音转写。提示词技能本身输出文本，实际视频生成由所选生成服务执行。

## 安装位置与维护

本仓库是两项技能的版本来源。每项技能的 `SKILL.md`、脚本、参考库和许可证一并保留。支持 Agent Skills 的客户端可安装完整的 `skills/<技能名>/` 目录；只复制 `SKILL.md` 会丢失运行依赖。

本次固定到已检查的上游提交，来源和验证记录见 [sources.lock.json](sources.lock.json)。不自动跟随上游更新。

## 上游与许可

- video-shots：来自 [eternityspring/reelbench-skills](https://github.com/eternityspring/reelbench-skills/tree/8d8444894524c9beedbf8815dab517afb3ce3cbe/skills/video-shots)，Apache-2.0，许可位于技能目录。
- cinematic-video-prompt-engineer：来自 [CyberJ0605/cinematic-video-prompt-engineer-skill](https://github.com/CyberJ0605/cinematic-video-prompt-engineer-skill/tree/135663a5ba1f2618c180d6a06f21ec79b5a90aef/cinematic-video-prompt-engineer)，MIT，许可位于技能目录。

用户视频、关键帧和分析结果在任务输出目录处理，不提交到本公开仓库。
