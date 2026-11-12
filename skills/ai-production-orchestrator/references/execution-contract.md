# 项目执行与恢复

project.json是项目定义：id、kind、brief、permissions、limits、steps。state.json保存执行状态、attempt、哈希、外部任务、预算与历史，actions.json给宿主代理当前可执行动作。以实际引擎 --help 和生成回执字段为准，不手猜request ID。

步骤包含id、kind、action、needs、params、inputs；依赖必须无环。@STEP:ROLE引用声明祖先的真实产物，不能跨未声明依赖偷取文件。多个相同role不是唯一引用，需拆分role或明确中间产物。

每个attempt有新的输出目录，保留旧版本。结果artifacts必须真实存在于指定输出范围且有正确哈希；evidence指出实际工具输出、观察、报告或源文件。review逐项记录passed/failed/not_checked，不接受只写reviewed:true。

## 执行循环

run完成所有ready local步骤。返回10表示有宿主动作：由当前代理读取相应技能、调用真实工具或完成审片，写结果并record，再run。返回20表示失败/阻塞，按原因处理；2为运行/输入错误。不能把退出10当成最终交付。

本地动作固定在pipeline_adapter白名单，不从项目文案、素材字幕或返回内容执行任意命令。产物路径受项目/attempt约束，不覆盖用户源片。

## 缓存与失效

输入、参数、依赖产物和已知媒体契约的传递文件都参与指纹。job/audio-plan/registry/board所指真实媒体变化也必须被检测，不能只hash一个JSON。

相同已完成步骤跳过；输出丢失/被改或上游变化时只使该节点和依赖闭包失效。生成步骤已有真实任务时优先核对/恢复下载，不无理由重生成。返工用invalidate记录问题和原因，保留历史与上一通过版本。

状态原子保存并用项目锁防并发写。发现锁或异常状态时核对进程/历史，不能无证据删锁后重复提交付费任务。

## 外部回执

异步：预算reserve/submit_intent → 真实工具调用 → submitted登记真实task ID → 查询 → 下载 → 技术检查 → record。

同步：先reserve并登记同步模式 → 真实工具调用 → 保存原始返回和文件 → record；没有远端task ID则明确为空，不伪造远端ID。

在submit_intent后中断且结果未知时进入reconcile。确认原调用未发生且未收费才可按规则重试；已有任务继续查询。预算预留、完成与释放都记账，本地幂等键不等于服务端保证exactly-once。
