# 音频时间线与混音契约

可执行 audio-plan.json 使用 version=1。所有 at、duration、source_start、fade_in、fade_out 均为秒；source 相对计划文件目录。节目时间是剪辑后的输出时间，不是原片时间。

顶层字段：

- name：节目名称；
- duration：真实输出节目时长，按现有 job 的逐片整数帧累加后除以 fps；
- sample_rate=48000、channels=2；
- target_lufs=-16、true_peak_db=-1.5：可按交付规范调整；
- ducking：enabled、threshold_db、ratio、attack_ms、release_ms；
- tracks：真实文件构成的声音事件。

每条 track：

- id：唯一稳定标识；
- role：dialogue、voiceover、music、sfx 或 ambience；
- source：真实可读取的音频，或含音轨的视频；
- at：节目入点；source_start：源文件入点；duration：本次使用时长；
- gain_db：响度处理前的增益；
- fade_in、fade_out：事件内淡化时长；
- loop：明确允许循环；仅 music / ambience 可用；
- duck：该条铺底是否随人声避让；不对人声做自我侧链。

先确认音频流的实际可用长度足够，不用更长的视频/容器时长代替音频长度。脚本会在补齐节目时间线前核对每个cue真实解码样本数；AAC等容器的元数据和可解码长度可能不同，可先提取WAV再按样本数安排。cue结束之后的节目空白与伪造配音长度不同，要明确记录。语音不循环、不盲截；短音乐或环境声只有明确 loop 时才重复。多个声音同时出现是正常混音，需要保留角色与来源；不能靠 amix 默认缩放把台词忽大忽小。

## 输出

mix 输出目录必须是新目录：

- mix.wav：最终主混音；
- stems/<role>.wav：以 0 秒为起点的全节目长度分轨；
- cue-sheet.csv：时间、来源与角色表；
- report.json：实际时长、采样率、处理、响度与峰值结果。

分轨经过增益、淡化、时间对齐以及适用的 ducking；主混音再经总线处理。因此分轨适合继续制作，master 是当前成片声音参考，两者不承诺逐样本相加完全一致。

mix.wav 与画面时长一致后使用 mux 生成新 MP4；保留原视频流，音频编码为通用 AAC。AAC 封装时长可能有编码 padding，实际音画结构需按一帧量级复核，不能仅凭容器 duration 宣称口型同步。

## 剪映与达芬奇

剪映可导入 WAV 与成片 MP4。分轨是完整节目长度，从 0 秒放入各音轨。不要把 master 和已经包含的分轨一起打开，避免重复。

FCP7 XML 交接用 audio_nle.py。默认替换新 XML 副本的音轨，防止保留原口播后再叠加含同一口播的混音。stems 模式继续混音，mix 模式仅一份主混音；两者都保留视频节点和源 XML。

本机 NLE UI 未验证时只报告格式与媒体检查。XML 的效果、混音与剪映原生工程是不同交付；不承诺所有软件版本完全保真。
