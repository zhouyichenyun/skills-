# Windows 本地运行与可选原生连接

1. Python 3.10+（推荐3.11/3.12）与FFmpeg/ffprobe加入PATH。不要把密钥写入仓库。
2. 主脚本无pip强依赖。本地ASR在虚拟环境中安装 `requirements-optional.txt`；模型在用户明确授权安装或任务必要且已获授权时，由setup/downloader另行准备到本地目录。转写命令本身始终只读本地模型，不会偷偷联网下载或上传媒体。依赖锁定为已验证的faster-whisper1.2.1与PyAV16.1.0；Windows新机仍须验证本机Python对应wheel可用。
3. 用 `python scripts/editor.py doctor` 看FFmpeg是否支持subtitles/libass、lut3d、loudnorm和overlay。必须实际安装支持的FFmpeg构建才有这些能力。
4. 路径带空格时加引号，例如：`python scripts/editor.py render "D:/Project/work/job.json" --out "D:/Project/output/v1.mp4"`。JSON路径建议用正斜杠，或使用合法的反斜杠转义。
5. 先打开生成的MP4和SRT复核；在剪映中可直接导入MP4继续剪辑。要可编辑源片时间线时，试验草稿必须同版本模板适配并实际验收。

## Resolve Python桥接

优先使用软件自己的 Help / Documentation / Developer / Scripting 文档核对当前版本与许可，外部脚本功能可能因版本/平台/许可而异。原生软件不随Skill安装。

常见模块路径为：`C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules`。
可用 `RESOLVE_SCRIPT_API` 指向 Developer/Scripting 根目录，并按本机官方README设置 `RESOLVE_SCRIPT_LIB`。Windows动态库路径经常位于Resolve安装目录，不能照抄别的版本。

如设置项可用，在 Resolve > Preferences > System > General 设 External scripting using 为 Local，并启动软件。运行桥接时使用能加载该版本官方Python模块的Python版本。先 `--dry-run` 只检查XML；真实调用才测试连接并导入新时间线。

桥接不启动/安装Resolve，不使用远程公网服务。默认创建带唯一后缀的新项目并导入新时间线，不保存或改动用户原来的工程；只有显式使用 `--use-current-project` 才会保存并导入当前项目。可选DRX只作用于明确选择的视频轨，不下载任何Look。运行前让用户自行保存当前未保存工作，导入后检查媒体重链接和效果。

连接失败时保留XML/EDL/SRT交付并报告准确阻塞，不能用dry-run结果代替原生连接证明。

## 本次配置的ASR验证范围

当前云会话另行准备了tiny本地模型，并用公开英文JFK样音实际运行transcribe，得到22个词且报告network_used:false。这验证了本地入口及该依赖组合，不代表已测试中文识别精度。Windows新机需要在自己的虚拟环境和模型目录重新验证，不自动获得云会话的模型文件。
