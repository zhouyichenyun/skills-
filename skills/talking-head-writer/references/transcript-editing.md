# 源稿、take 决策与时间映射

## 保存三层文本

1. `raw_transcript`：识别或人工记录的原话，不覆盖。
2. `spoken_edit`：选用真实 take 后的实际口语；每句指向来源词或区间。
3. `subtitle_text`：字幕显示的规范文本，例如单位、数字和专名写法；仍可追溯原话。

识别不清的词用不确定标记，不根据主题“补全”录音。需要听审时记录 `needs_audio_review`；没有听音能力就继续完成文本与素材规划，不声称听审已通过。

## take 与编辑决策

每项记录：`decision_id`、`line_id`、`source_id`、`source_start`、`source_end`、`action`、`reason`、`evidence`、`verified`。`action` 可为 `keep`、`drop`、`retake`、`review`。`retake` 不生成已有源路径；未录完前保留为需求。

可自动提出的候选：同一句明显重录的较差版本、无意义重复、已核实的口误、过长准备空白。以下不能只按词表删：否定词、语气中含义必要的犹豫、引用、保留争议或限定条件、解释前提、情绪转折。语音的“嗯”有时表达态度，不能全局删词。

从词级时间切到媒体切点时，考虑音素起止、呼吸、房间底噪和嘴形，必要时留很短的声学余量。余量以实际听音为准，不宣称固定 80ms 永远可用。静音检测只产生候选，不能证明语义可删。

## 时间计算

本交接版本只支持顺序拼接、1 倍速 `clips`；源区间采用半开区间 `[start,end)`，单位为秒。输出起点等于前面所有保留区间时长之和。

对于一段保留区间：

`output_t = output_start + (source_t - source_start)`

只在该区间内映射。被删词没有输出时间，应为 `null` 并保留删除原因。跨剪切边界的字幕应先分开，再分别映射；不要线性压缩整条原 SRT。字幕不能跨越不连续的源语句并暗示它们原来是一句连续表达。

如将来需要变速、多轨交叉或自动重定时，交给支持此能力的新版本契约；当前 job 不接受隐含的 `speed`。

## transcript-map.json

顶层使用 `version:1`、`verified_against_media` 和 `segments`。每个段包含：

- `clip_id`、`line_ids`、`source`、`source_start`、`source_end`。
- `output_start`、`output_end`，长度应与源长度一致。
- `tokens`：可选的词级数组，包含 `raw`、`display`、`source_start`、`source_end`、`output_start`、`output_end`、`status`。没有真实词级时间就不生成 tokens，不能伪造精度。
- `subtitle_ids`：映射到输出字幕。规范化专名时记录 `normalization_note`。

被删内容放 `dropped` 数组：保留源区间与决策 ID，输出区间为 null。原稿句号 ID 和素材计划引用句号 ID 均不因删改重排而丢失。

最后核对：各 clip 源区间可读、长度为正；映射输出连续且总长等于成片；字幕位于 `[0,total_duration]`；原稿到字幕的规范化只改书写不改含义；不把删掉的原话重新写入字幕。
