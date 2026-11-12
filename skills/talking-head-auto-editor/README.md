# 口播自动剪辑与跨软件交付

入口：[SKILL.md](SKILL.md)。Python标准库+FFmpeg/ffprobe可剪辑和导出；本地ASR可选。

```sh
python scripts/editor.py doctor
python scripts/editor.py plan input.mp4 --transcript words.json --out work/job.json
python scripts/editor.py render work/job.json --out output/first-cut.mp4
python scripts/editor.py export work/job.json --out-dir output/editable
```

配合Agent执行完整素材步骤：按真实口播语义评估用户素材、查看关键帧、记录来源/权利，缺口按授权调用已连接的AI生成工具，完成后检查真实文件再写入B-roll。配置本技能不直接花额度生成素材。

实际效果以MP4为准。XML含基础主轨、源音轨和B-roll第二轨，SRT单独交接；FFmpeg烧录字幕、裁切、LUT/调色与响度并不自动还原成原生软件效果。剪映草稿是同版本模板适配/非官方候选，未在目标UI实际验收。

本地合成媒体测试：

```sh
python scripts/selftest.py --work work/new-empty-editor-test
```

当前已通过9组集成回归：实测静音+词稿保护、无词稿保留源片、含音频/B-roll/中英字幕/warm调色实际137帧MP4与解码、XML双视频轨/音轨/SRT/EDL、剪映模板源文件不改、已有输出和源保护、30000/1001fps非整秒切口及实际.cube渲染、混fpsXML元数据、Resolve仅dry-run且默认独立工程。

这些测试没有执行Windows、Resolve/Premiere/剪映UI导入，没有替代真人听感或叙事判断。ASR入口另由当前配置流程以公开英文样音验证；未验证中文精度。所有源文件不覆盖，测试使用新目录中的合成媒体。
