# Job v1 与时间线互通

```json
{
  "version": 1,
  "name": "口播-v1",
  "fps": "30000/1001",
  "width": 1920,
  "height": 1080,
  "clips": [{"id":"a1","source":"../media/a.mp4","start":0.25,"end":4.75}],
  "broll": [{"source":"../media/detail.mp4","at":1.0,"duration":2.0,"source_start":0.5,"fit":"cover"}],
  "subtitles": [{"start":0.2,"end":2.8,"text":"经过核实的字幕"}],
  "grade": {"preset":"neutral","lut":null},
  "audio": {"target_lufs":-16}
}
```

- clips 的 start/end 是每个源视频中的秒数；主片按列表顺序紧接拼接。
- broll 的 at/duration 和 subtitles 的 start/end 是删减/拼接后的**输出秒数**。
- 相对 source/lut 路径锚定 job 文件所在目录。网络URL不是本地素材。
- fps 可以是数字或有理数字符串。每段输出时长分别量化到最近的输出帧，报告实际帧数；切口可能有最多半帧舍入。
- B-roll覆盖画面，保持主音轨，不混入其自身原声；静态图片可作为B-roll。当前不支持B-roll相互重叠、任意多层合成、速度曲线、转场模板或自动面部追踪。
- 非整数fps的EDL使用名义帧编号和NDF标记，不保证与所有软件/源时码策略一致。混合帧率、摄影机原始时码、超过24小时的事件应优先XML并在目标软件核对。
- XML是FCP7-style xmeml，不是现代FCPXML；Resolve/Premiere可尝试导入。XML里的文件源尺寸/帧率/音轨来自ffprobe，主轨和B-roll轨独立；字幕走SRT。
- 最终MP4烧录效果不等于原生时间线效果。XML不携带FFmpeg的LUT、eq、crop cover、响度处理或libass字幕样式。兼容性报告是交付一部分，不能省略。
- 素材迁移到Windows后需重链接；在目标机器使用同一个相对路径job重新export可生成当地绝对路径。
- 剪映best-effort仅包含基础素材/轨道/时间区间，不能视为官方格式。给同版本模板时只写新副本，保留其他模板状态，但旧素材特效引用会清除，仍必须手工原生验收。

脚本没有复制第三方库代码，采用原创标准库实现。格式设计参考：Apple FCP XML Interchange Format（xmeml）、Blackmagic Resolve 本机 Developer/Scripting 文档；实际兼容性以目标软件导入结果为准。
