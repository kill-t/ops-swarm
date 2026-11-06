# 本地 Compose 人工续接与中断恢复

2026-11-06，沿用独立 `opsswarm-smoke` 项目和既有验收卷。模型响应由容器内脚本固定，现场事实来自本地变更服务和订单 HTTP 探针，命令、证据、账本和执行结果由真实 PostgreSQL 持久化。

## 已验证的边界

- 浏览器使用可信工程师身份提交人工补充和重评材料准备命令，两项均显示命令已记录。准备 Worker 保存结果后，旧调查的材料被标记为 `blocked`，原因为 `run_deadline_exceeded`。保留旧运行和原截止时间。
- 新调查在审查模型调用成功落账、审查事实尚未保存时注入异常。原 Worker 退出后，另一个一次性容器通过检查点恢复到 `await_human`。前后账本一致，均为 5 次模型预留和 2 次工具预留。恢复日志无模型请求，只产生 1 条持久交接事件。这次注入验证异常后的跨进程恢复。恢复日志仍提示部分类型的 checkpoint 反序列化白名单告警。
- 通过同源 API 用新事故执行可信人工补充、材料准备、独立重评和候选评估。系统依据实际演练发布记录生成高风险单步回滚候选。普通工程师审批返回 403，审批人与执行者使用独立身份。首次派发的相同幂等键重放返回原任务。执行 Worker 缺少可信配置时以 `operator_unauthenticated` 阻断。隔离覆盖配置需把与控制面相同的可信模式和凭据绑定传给执行 Worker。
- 补齐配置后，新事故实际从 `2026.01.01-rc1` 回滚到 `stable`，修订号从 4 变为 5，业务探针由 HTTP 500 变为连续三次 HTTP 200。收敛决定比样本有效期晚约 48 秒，记录为 `held_stale`，事故维持 `mitigating`，原样本和结论保留。
- 新事故按相同策略执行，重复派发返回同一任务 ID，修订号从 6 变为 7。让恢复采样、父运行收尾和事故收敛连续消费，三次 HTTP 200 后，调查进入 `settled`，事故进入 `resolved`。
- 失败场景首跑使用 Windows 宿主机的告警时间，记录的事故开始时间比容器固定的观察截止晚约 1.06 秒。调查在首次模型调用前触发 `coordination_window_invalid`。定向重试得到同一拒绝，账本仍为空。后续场景用演练变更服务返回的实际发生时间上报，保留时间守卫。
- 一次冷启动派发在准备截止前约 0.33 秒登记操作，执行前复核触发 `release_execution_source_stale`。租约到期后只读对账保存了 `not_applied`。原操作及过期证据保留，准备窗口仍为约 30 秒。
- 先启动执行 Worker 再派发，回滚生效后于结果落库前退出进程。首个尝试自然到期，接手进程只对原操作发出一次 GET。两次尝试共享同一操作，最终为 `applied_current`，版本修订号始终为回滚后的 17。
- 同次回滚后注入独立连接池故障，连续三次业务探针返回 HTTP 500，收敛结果为 `observed_failure`，事故回到 `investigating`。首次再调查申请通过，同键重放返回原申请，第二个键被 409 拒绝。再调查协调以 `model_input_budget_exceeded` 阻断，尚未进入专业取证、综合、独立复查和人工接管。测试完成后恢复连接池配置，订单探针返回 HTTP 200。

## 发布缺口

只读重建被冻结的再调查协调材料，序列化输入为 52,117 字节。现行估算器按 UTF-8 字节数加 256，得到 52,373 个估算输入 Token，比单次上限 48,000 多 4,373。原调查材料约 28.7 KB，是最大的材料项。下一步核对原调查材料中的重复内容，验证能否使用有界上下文并保留出处、反证、人工约束和原账本。按完整行为复测真实新窗口取证、复查及人工接管后，再收口发布。

## 复现与面试表达

复用[启动记录](local-compose-smoke.md)的 Compose 基线。验收用配置、脚本和结构化结果存于项目忽略目录 `.pytest_cache/compose-local-smoke/`。运行 `continue_graph.py` 注入审查保存前的异常，运行 `deployment_case.py` 验证人工续接到收敛，按脚本使用本地可信凭据和演练 Worker 容器。设置 `OPSSWARM_SMOKE_FAILED_PROBE=1` 进入中断对账和未恢复场景。

- `continuation-recovery.json` 保存调查恢复账本结果。
- `continuation-blocked-credentials.json`、`continuation-held-stale.json`、`continuation-coordination-window.json` 和 `continuation-dispatch-expired.json` 保存四类守卫记录。
- `continuation-recovered-proof.json` 保存正常收敛样本和事故槽位结果。
- `continuation-failed-probes.json`、`continuation-reflow-coordination.json` 和 `continuation-reflow-budget.json` 保存对账、失败探针、再调查准入与输入预算数据。

面试可以展示审查调用已经入账时发生进程中断，重启后复用持久结果，保留原预算并避免重复调用。同一事故中，审批权限和执行 Worker 的当前可信绑定分别校验，配置缺失时会留下明确的阻断原因。
