# v3 动态图首轮协调与 checkpoint 恢复

## 目标与边界

本片把已经验收的 v3 启动契约和首轮协调服务接入独立 LangGraph 图，使一次 v3 运行能够在有效父租约内生成、持久化并恢复第一轮动态计划。图版本固定为 `dynamic-investigation-graph-v1`，不会读取或续跑 v1/v2 checkpoint。

当前图只有 `START → coordinate → END`。它的返回值是已复核的 `CoordinationPlanRecord`，表示首轮分派事实已经形成，不是最终调查结果。专业任务执行、综合、审查、停止条件和父运行终态结算仍属于后续切片。

## 图状态与装配

图状态只保存事故请求、`workflow_version`、`graph_version` 和首轮协调计划。父 Worker 的 `lease_token` 不进入图状态或 checkpoint；每次首次执行或恢复都由装配工厂把当前领取尝试的新租约注入 `CoordinationPlanner`。这样旧 Worker 崩溃后，checkpoint 可以保留流程位置，但迟到实例不能借旧租约继续写业务事实。

`DynamicCoordinationGraphFactory` 只接受满足以下条件的运行快照：

- `multi_agent + frozen + v3 + dynamic-investigation-graph-v1`；
- 运行 ID、稳定 `graph_thread_id` 和当前租约均存在且匹配；
- 完整协调策略、调用预算和 function_calling 模型预算已经固定；
- 上下文、工作摘要和证据策略保持 v3 启动时的固定版本；
- 模型名称、结构化输出方式、SDK 重试和候选数与持久 profile 一致。

冻结来源的内容身份和最大证据时间作为首轮计划的 `evidence_snapshot_id` 与 `observation_cutoff_at`。模型只能提出受限角色、目标和回看时长，实际服务、时间范围、快照和任务尝试上限继续由确定性编译器注入。

## checkpoint 兼容契约

每次图调用都在 LangGraph config metadata 写入 `workflow_version=v3` 和固定 `graph_version`，初始状态也保存同一组字段。恢复前同时核对 metadata 与 channel state；节点尚未开始时从 `__start__` 输入读取状态版本，节点完成后从普通 channel values 读取。任一字段缺失或不一致都在协调服务调用前失败。

首次入口发现同线程已有 checkpoint 时要求改走 `resume`。恢复入口只接受由运行 UUID 确定性生成的线程 ID，不能把其他运行或旧图状态移植到当前 v3 运行。计划从 checkpoint 反序列化后重新通过 Pydantic 契约校验，再用当前租约从业务库重建原计划并要求完全相等；这一复核不会再次请求模型。

## 业务事实与恢复

业务 PostgreSQL 是策略、模型调用、计划、轮次和任务的事实来源，checkpoint 只保存图推进位置。二者不组成分布式事务，恢复依靠第 84 步已经建立的调用和计划幂等规则收敛：

1. 模型调用已成功结算、计划尚未提交时崩溃，新尝试复用相同输入和材料清单对应的成功调用，再原子创建唯一计划、轮次和任务。
2. 计划已提交、协调节点 checkpoint 尚未推进时崩溃，新尝试从业务库复核并返回原计划，不再次请求模型，也不重复创建任务。
3. 调用请求已经发出但尚未成功结算时仍可能成为未知结果；本片不宣称对任意网络中断提供模型 exactly-once。

协调上下文和共享调用预算现在兼容 v2/v3。清单保存运行的真实工作流版本；旧 v2 输入与清单继续序列化为 `v2`。v3 还额外复核图版本、协调策略版本和摘要上下文策略，防止只修改工作流字符串绕过启动契约。

## Worker 隔离

通用 `InvestigationWorker` 仍只领取 v1/v2。它的成功路径要求处理器返回完整 `ProcessorOutcome` 并立即结算父运行，而首轮协调图只有计划，没有证据、专业报告、综合结论或质疑结果。此时开放 v3 领取会把阶段完成误当成调查成功。

集成测试通过显式 `supported_workflow_versions=(v3,)` 领取一次 v3 来验证真实租约和恢复，但这不是生产 Worker 路由。后续需要先完成动态专业任务、综合与 v3 终态事实核对，再让对应 Worker 声明 v3 能力。

## 接口与数据结构

本片没有增加 HTTP API、业务表或迁移。首轮计划继续通过既有业务表持久化和内部查询服务复核；v3 运行的公开状态仍保持非终态，不能把协调计划存在描述为动态调查已经完成。
