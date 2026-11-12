# 宿主动作回执

request_id、input_fingerprint和output_dir直接从actions.json当前动作复制。不能手猜ID、复用旧输入指纹或把以下示例当真实执行证据。

## agent：脚本、导演方案等

    {
      "status": "succeeded",
      "request_id": "COPY_FROM_ACTION",
      "input_fingerprint": "COPY_FROM_ACTION",
      "generation_performed": false,
      "artifacts": [{"role": "script", "path": "CURRENT_ATTEMPT/script.md"}],
      "evidence": [{"path": "CURRENT_ATTEMPT/analysis.md", "note": "实际读取素材后形成的依据"}]
    }

生成媒体必须走provider预算事务，不藏在agent回执里。用户原始素材的读取、检查和脚本写作不算新生成媒体。

## provider：异步或同步

先reserve；异步工具返回真实task ID后立即submitted，再继续查询。真实文件保存到当前attempt中，技术检测/评审另做。

    {
      "status": "succeeded",
      "provider_status": "succeeded",
      "request_id": "COPY_FROM_ACTION",
      "input_fingerprint": "COPY_FROM_ACTION",
      "task_id": "REAL_ASYNC_TASK_ID",
      "artifacts": [{"role": "video", "path": "CURRENT_ATTEMPT/clip.mp4"}],
      "evidence": [{"path": "CURRENT_ATTEMPT/tool-result.json", "note": "实际服务回执"}]
    }

image_gen等同步接口使用reserve --mode synchronous，没有远端task ID时省略或null，另提供tool_receipt指向实际返回记录。保留request_id和文件hash，不能伪造远端ID。provider步骤必须声明expected_media:image/video/audio，文本文件不能冒充生成媒体。

工具调用参数仅取当前schema支持项；expected_media、引擎request_id、预算和审核字段是控制元数据，不直接塞进生成工具参数。

## review：真实看图/听音

步骤params.video/final_video/subject绑定实际待审文件，required_checks由交付要求决定，回执不能缩减。

    {
      "status": "succeeded",
      "request_id": "COPY_FROM_ACTION",
      "input_fingerprint": "COPY_FROM_ACTION",
      "checks": {
        "visual": {"state": "passed", "notes": "实际看图结论", "evidence": ["CURRENT_ATTEMPT/frame-01.jpg"]},
        "listening": {"state": "passed", "notes": "真实听审结论", "evidence": ["CURRENT_ATTEMPT/listening-review.md"]},
        "continuity": {"state": "passed", "notes": "相邻镜头与状态核对", "evidence": ["CURRENT_ATTEMPT/continuity-review.md"]}
      }
    }

未检查时写not_checked，不能为了继续生产而伪填passed。引擎生成绑定subject_sha256的review_result，最终交付核对它是否属于当前最终视频。只交技术版本时明确acceptance:technical，保留待创意审片状态。
