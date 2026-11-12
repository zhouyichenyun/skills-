# 剪辑交接契约 v1

## 产物

- `script.md`：brief 与逐句稿，稳定 line_id。
- `edit-decisions.json`：源时间、take 选择和删改依据。
- `transcript-map.json`：源语句/词与成片字幕时间的映射。
- `asset-plan.json`：素材候选、来源、许可和缺口。
- `glossary.json`：专名的原读法、字幕写法与证据。
- `job.json`：供自动剪辑执行的真实文件与输出时间表。

无需每次打印所有内部表。按用户实际交付范围提供文件，关键状态放在短说明里。用户没提供视频时，写稿与素材 brief 可以完成；job 等待真实素材，不阻塞其余工作。

## job.json 精确结构

```json
{
  "version": 1,
  "name": "project-name",
  "fps": 30,
  "width": 1080,
  "height": 1920,
  "clips": [
    {"source": "media/take-main.mp4", "start": 2.0, "end": 6.0, "id": "A01"}
  ],
  "broll": [
    {"source": "media/phone-detail.mp4", "at": 1.0, "duration": 2.0, "source_start": 0.5, "fit": "cover"}
  ],
  "subtitles": [
    {"start": 0.0, "end": 3.7, "text": "字幕内容以实际保留话语为准"}
  ],
  "grade": {"preset": "neutral", "lut": null},
  "audio": {"target_lufs": -16}
}
```

上面是结构示意，不是已存在的素材声明，不能直接执行。实际 job 的每个路径都须核验。

## 时间和文件规则

- 路径相对于 job 文件。传给 Windows 用户的路径由下游适配本机；不要把 Linux 工作目录伪称为用户磁盘。
- `clips` 按数组顺序连续拼接，`start/end` 是各源文件中的秒，半开区间，1 倍速。
- `broll.at`、`subtitles.start/end` 是拼接后的输出秒，不能误用源时间。
- 每个 `clip.id` 唯一；一个源可出现多次，但每段独立计算。
- 输出总时长为 `sum(clip.end-clip.start)`；B-roll 和字幕不可超出它。
- B-roll `source_start+duration` 不可超出源片可用长度；v1 只使用 `fit:cover`。
- 不把未实现的转场、变速、多机位、降噪参数、字幕样式或标题轨添加进 job。需要更复杂能力时放在人类可读的后期说明，不能保证 v1 执行器支持。
- `grade` 默认 neutral/lut null。导演确认色彩意图后，下游在有色彩空间信息与实际素材时执行调色。编导不凭文本猜 LUT、Log 格式或肤色准确性。
- `audio.target_lufs` 默认 -16，仅是交接目标；不能仅凭该字段声称成片响度已经达标。

## 字幕与原稿追踪

`transcript-map.json` 是完整追踪源；job 字段保持精简，不在 job.subtitles 塞入额外键。映射侧的 `subtitle_id` 可按数组位置采用 `SUB001` 等稳定编号，并记录源 token/句号 ID。字幕做专名规范化、合并或分句时保留对应关系。

## 给剪映/达芬奇的交接说明

编导交付平台无关的 job 与素材计划。下游自动剪辑可据支持能力导出草稿/交换时间线、SRT 和 MP4；具体格式和可用效果以实际执行器与软件版本验证为准。不能把 JSON、提示词或文件导出成功当成剪映/达芬奇已成功导入。导入后回查素材重连、时长、声音、字幕字体和画幅，再进行效果审核。
