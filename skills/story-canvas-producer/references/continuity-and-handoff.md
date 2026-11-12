# 故事连续性与交接

以镜头为单位写 start_state → action → end_state。可采用扁平状态键：

- CHAR_01.location、CHAR_01.facing、CHAR_01.holding_hand；
- CHAR_01.costume、CHAR_01.injury、CHAR_01.knowledge；
- PROP_CUP.holder、PROP_CUP.location；
- SCENE_01.time、SCENE_01.weather、SCENE_01.key_light。

下一镜同一 continuity_group 应继承已发生的状态。状态变化必须有可见动作、明确时间切换或故事中可解释的原因。例：上一镜杯子在右手，下一镜左手持杯而中间没有转手，属于需解释的断点。

交叉剪辑和回忆可用独立 continuity_group 管理各自状态。continuity_overrides 需要 key 与 reason；它声明刻意跳变，不能用来掩盖未看图或生成穿帮。脚本只比较明确的键值，不理解整段剧情，也不证明画面与声明一致。

每镜还要记录谁知道什么：角色没有获得证据之前不能先对秘密作反应。情绪与对白从信息触发发展，不为了“戏剧性”重写用户结局或人物关系。

## 视频提示词

把真实选定素材 ID、参考文件、起点状态、动作、终点状态、镜头时长、空间机位和需要保持的事实交给 cinematic-video-prompt-engineer。遵守其连续性合同：保留状态与身份，允许有目的的机位变化；不强制复制上一个尾帧为整段固定构图。

## 后期交接

给 talking-head-auto-editor / cinematic-director 的清单包括：

- 镜头 ID、顺序、用途与节目区间；
- 实际视频或静态图路径及可用时长；
- 角色/场景资产版本与候选评审；
- 需要声音的动作与情绪点；
- 需要字幕或准确文字的区间；
- 未解决的视觉问题与允许的替代方案。

AI 分镜图不是完整成片。用户要视频时还需实际视频生成、测量、剪辑与审核。画面、时间线结构、声音和最终成片各自记录完成证据。
