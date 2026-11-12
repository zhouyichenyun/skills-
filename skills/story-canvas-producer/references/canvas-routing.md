# 画布路由与数据

## 默认离线画布

story_board.py 从 board.json 生成单文件 HTML 分镜板。角色/场景、故事镜头、候选与问题以卡片呈现，可拖动、平移缩放、过滤和导出评审。没有实际图片时明确显示 planned，不能用占位图伪装生成完成。

图片为 PNG/JPEG/WEBP，原文件直接内嵌，不做图像修改；每张不超过8MiB、总内嵌不超过64MiB。PNG另有64MiB单图解压上限，检查压缩流、扫描行和滤镜；JPEG/WebP检查格式结构与尺寸。结构检查仍不等于完整解码或真实看图。超限时按场景或章节拆板，不偷偷缩减证据。HTML 不从网络加载资源。

布局和评审导出是新文件，不改原 board，不自动批准候选。画布上的备注可交回代理核对后形成新的 board 版本。后续执行生成、资产替换和锁定仍需遵守真实评审。

## 可选 Canva

当前 Canva create_design 不支持新建 whiteboard 或视频。可用的可编辑演示页/设计页可作为角色手册或分镜展示；不要称为已创建无限白板。

固定页面编辑的具体支持范围以当前工具为准，不能凭空改变页面数量/顺序。编辑保存要求先向用户预览并取得明确批准；相关工具原文是：“Show the user a preview … obtain explicit approval before saving changes.” 只有实际进入该保存流程时才需要此步骤，默认离线画布不新增这个批准环节。

用户要求自动选择时优先离线分镜板；仅在用户指定或明确需要 Canva 可编辑页面时采用它。当前配置任务不创建外部设计或保存占位页面。

## board v1

- version=1、title；
- characters：id、name、locked_traits、selected_asset_id；
- scenes：id、name、locked_facts、selected_asset_id；
- assets：id、kind、status、path、character_ids、scene_id、view；
- shots：id、order、scene_id、character_ids、continuity_group、start_state、action、end_state、selected_asset_id、depends_on；
- candidates：id、target_id、asset_id、round、scores、hard_failures、review_status、evidence；
- limits：candidates_per_round、max_rounds。

status 可为 planned/generated/reviewed/selected/rejected；仅 planned 可以没有可查看文件。kind 支持 character/scene/storyboard/shot/reference/image；明确类型与目标冲突会阻止选择和排名，reference/image为通用参考。selected_asset_id 要有对应目标的已评审、证据非空、无硬失败候选。角色身份和场景事实仍由真实看图核验。

同组镜头按 order 核对显式状态。有意跳变可用 continuity_overrides:[{key,reason}]。源路径相对 board.json；不能把用户 Windows 路径误认为云端已有文件。

validate 的语义错误退出10，运行错误退出2。build 可输出标为 invalid 的排障画布，必须查看其验证状态；无错误只证明字段/引用/声明一致，不等于视觉通过。
