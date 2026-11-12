# 示例结构：先替换真实媒体路径

下面仅展示15秒剪辑的计划形状，不含生成完成的素材，不能直接执行。dialogue.wav 是保留原口播的全长音轨；若改为新配音，应改为 voiceover 并去掉对应原口播，不能同时播放同一句。

    {
      "version": 1,
      "name": "口播声音示例",
      "duration": 15,
      "sample_rate": 48000,
      "channels": 2,
      "target_lufs": -16,
      "true_peak_db": -1.5,
      "ducking": {"enabled": true, "threshold_db": -30, "ratio": 8, "attack_ms": 15, "release_ms": 250},
      "tracks": [
        {"id": "DIALOGUE_01", "role": "dialogue", "source": "media/dialogue.wav", "at": 0, "source_start": 0, "duration": 15, "gain_db": 0, "fade_in": 0, "fade_out": 0, "loop": false, "duck": false},
        {"id": "MUSIC_01", "role": "music", "source": "media/music.wav", "at": 0, "source_start": 0, "duration": 15, "gain_db": -18, "fade_in": 0.5, "fade_out": 1, "loop": false, "duck": true},
        {"id": "SFX_01", "role": "sfx", "source": "media/click.wav", "at": 4.2, "source_start": 0, "duration": 0.3, "gain_db": -6, "fade_in": 0.01, "fade_out": 0.1, "loop": false, "duck": false}
      ]
    }

先实际生成或选取文件，测量源长度并校验，再保存为自己的 audio-plan.json。素材短于使用区间会拒绝，不靠补静音伪装配音完成。
