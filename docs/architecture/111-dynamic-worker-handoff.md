# v3 通用 Worker 与终端路由持久交接

## 目标与边界

将已实现的 graph-v15 三轮调查接入独立 Worker；终端只表示调查阶段完成并交接下一阶段。`await_human` 映射父运行 `await_human`，`candidate_evaluation` 映射 `await_candidate`，两者均保留 `completed_at/result/metrics=NULL`，Incident 继续 investigating 并占用活动运行唯一槽位。候选不是固定根因或执行授权；本片不新增人工输入、图内 interrupt/resume 或候选评估器。

## 状态与原子交接

新增不可变 `investigation_terminal_handoffs`，一运行至多一条，绑定原路由 ID/散列、目的地、当前交接尝试和时间。原路由尝试可不同于当前接管尝试。工作项 completed 并清空租约，父尝试 handed_off 并保存本次用量，运行进入对应等待态；同事务追加可重放的 `run.handed_off` 任务事件。未知用量保持未知，共享预算与历史调用不重置。

交接锁定父工作项，在最高轮路由的持久来源重建后再次检查实际时钟下的有效租约。拒绝跨运行、不同散列、follow_up、存在后续轮次、未完成任务和 reserved 外部请求。原生复合外键、不可变守卫和延迟跨表约束阻止部分交接与伪造等待态。旧 complete 明确拒绝 v3；v1/v2 成功契约保留。

同尝试、同来源、同模型和用量的命令重放返回原事实，不重复结算或追加事件；其他命令冲突。查询重新验证业务来源，无事实返回 null，不从 checkpoint 推断结果。

## Worker、失败与恢复

现有默认 Worker 保持 v1/v2。显式启用动态能力后，同时支持 v1/v2 和冻结 v3 graph-v15；SQL 同时过滤工作流及图版本，旧 v3 图保留而不被新图领取。进程装配现有 LangGraph 工厂，沿用 has_checkpoint/investigate/resume 与原图版本，不再造调度器。

配置 `OPSSWARM_WORKER_ENABLE_DYNAMIC_WORKFLOW=true`，要求 frozen、processing_timeout_seconds 至少 180，租约比处理窗口至少长 30 秒；建议处理 240 秒、租约 300 秒。Compose 冻结池接收开关，停止宽限可用 STOP_GRACE_PERIOD 配置。采用 `release-500-worker-v1` 固定演练材料，最新模拟观察锚定事故开始并保留相对时间，完整内容散列包含实际事故身份；仅支持 order-service 发布故障。它不是现场取证，不修改已发布基线或旧 v1/v2 来源。不同来源的历史 graph-v15 若快照不匹配必须失败，不能静默换材料。预算仍受运行固定策略限制，未增加心跳或无限续租。

运行调用预算建议 max_model_calls=16、max_duration_seconds=900，为 300 秒租约过期后的恢复留出窗口；已固定的默认 300 秒总时限不会因 Worker 窗口变长而自动延长。过期后预算门禁仍停止自动调查。

模型/图错误沿既有失败退避和最大尝试规则；超时不会清零共享预算。图路由提交后交接前崩溃，由到期父租约的新尝试复用业务事实和 checkpoint。交接事务失败整体回滚，错误向进程传播，租约到期后重试；达到最大尝试仍进入失败。交接提交后进程崩溃，任务已完成不再领取；原命令可幂等重放。等待态没有自动恢复命令，后续人工或候选路径必须另行设计版本及授权。

## 查询

`GET /api/v1/investigations/{run_id}/terminal-handoff` 返回交接事实或 null；未知运行 404，损坏来源 409。已有运行、尝试、预算与任务事件查询反映新状态。查询不触发模型、候选评估、规划或执行。
