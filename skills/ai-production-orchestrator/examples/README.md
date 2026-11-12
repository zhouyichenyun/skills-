# 可复现的自动化验证

在已安装FFmpeg/FFprobe并具备九项技能的环境运行：

    python scripts/demo.py --out-dir work/production-demo

它生成明确标记为测试的短视频与合成音，自动运行：

环境检查 → 剪辑/字幕 → XML → 声音计划 → 混音 → 合成 → 音轨XML → QC → 技术修复 → 最终QC → 便携交付包。

随后原项目再run一次，检查所有阶段复用而不重复执行。不会调用付费生成，不包含用户素材，不声称完成真实口播/AI故事或艺术听审。最终状态应为technical_done_pending_review。

实际项目不要求用户编写这份spec：总调度代理根据用户brief与真实素材建立项目，消费创作、生成和审核动作，并持续运行到交付或明确阻塞。
