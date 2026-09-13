# 独立动态工作流版本与运行策略冻结

## 目标与边界

本片为后续动态 LangGraph 图增加独立 `v3` 运行版本。创建运行时一次固定协调策略、任务上下文、工作摘要、证据、调用预算、模型预算和图恢复版本，并通过原运行查询返回完整启动契约。这样后续 Worker 重启只能按创建时的版本恢复，不会把新图套到既有 v1/v2 checkpoint。

本片只交付启动与调度隔离。v3 运行会进入 `queued`，持久任务保持 `pending`；当前调查 Worker 只声明支持 v1/v2，因此不会领取 v3，也不会创建 checkpoint。第 84 步首轮协调服务尚未接入 Worker，不能把可创建的 v3 描述为已经运行的动态调查。

## 启动契约

v3 当前只接受 `multi_agent + frozen`，必须显式提供 `model_budget`，且模型 profile 的结构化输出方式必须是 `function_calling`。服务端固定或补齐以下字段：

| 字段 | v3 规则 |
| --- | --- |
| `workflow_version` | 固定为 `v3` |
| `graph_version` | 固定为 `dynamic-investigation-graph-v1`，表示图拓扑和状态恢复契约，不表示 LangGraph PostgreSQL 表结构版本 |
| `coordination_policy_version` | 固定为 `dynamic-coordination-v1` |
| `coordination_policy` | 可由请求在领域上限内指定，省略时使用 `CoordinationPolicy` 默认值；完整内容和 SHA-256 写入不可变策略表 |
| `context_policy_version` | 固定为 `summary-context-v1`；请求指定其他策略时拒绝 |
| `working_summary_policy_version` | 固定为 `task-working-summary-v1` |
| `evidence_policy_version` | 固定为 `task-evidence-v1`；现场证据模式留给后续独立切片 |
| `call_budget` | 请求可提供受限值，省略时使用现有默认策略 |
| `model_budget` | 必填，完整 profile、Token 和估算费用上限随运行保存 |
| `graph_thread_id` | 继续由运行 UUID 确定性生成；运行 UUID、`workflow_version` 和 `graph_version` 共同确定恢复资格 |

`POST /api/v1/incidents/{incident_id}/actions/start-investigation` 在原事务内迁移 Incident、插入运行、投递工作项并保存协调策略。运行与工作项先刷新以满足外键，再写协调策略，所有步骤仍由同一数据库事务提交；策略写入失败会一起回滚，不会留下处于 investigating 的孤立 Incident。

旧 v1/v2 运行的 `graph_version`、`coordination_policy_version` 和启动响应中的 `coordination_policy` 保持空值。第 83～84 步测试使用的 v2 内部协调策略仍可存在于策略表，但不会被误表示成 v2 的公开启动契约。

## 数据库约束与查询

迁移 `0043_dynamic_workflow_start` 为 `investigation_runs` 增加可空 `graph_version` 和 `coordination_policy_version`，并扩展上下文、证据和预算约束以识别 v3。新增动态策略 CHECK 强制 v3 同时满足冻结来源、全部固定版本、非空预算、摘要提示和 function_calling；所有可空字符串都使用 `COALESCE` 比较，避免 PostgreSQL 将 `NULL` CHECK 结果视为通过。非 v3 行必须保持两个新版本字段为空。

完整协调策略继续使用 `investigation_coordination_policies`，不在运行表复制 JSON。运行查询依据非空启动策略版本读取该表，并复核 Pydantic 契约、版本和内容散列后返回；缺行或版本不一致时失败关闭。存在 v3 运行或任一新版本字段时，迁移拒绝降级。

## Worker 隔离与失败路径

领取接口增加 Worker 支持的工作流版本集合，并把它放进 `FOR UPDATE SKIP LOCKED` 的 SQL 条件。现有 Worker 明确传入 v1/v2：队列前方即使有更早的 v3，它仍可领取后面的兼容任务，v3 的尝试次数、开始时间和租约保持不变。未来动态图 Worker 必须显式声明 v3 才能领取。

SQL 过滤之外还有两道防线：旧多 Agent 处理器对 v1/v2 做穷举路由，遇到 v3 会在访问 checkpoint 前返回稳定错误；固定任务图工厂也只接受 v2。缺模型预算、非 function_calling、单 Agent、现场来源、不兼容上下文、空 Worker 版本集合或不完整数据库快照均明确拒绝。

## 后续接入

下一片新增只接受 `workflow_version=v3` 与 `graph_version=dynamic-investigation-graph-v1` 的动态图装配，接入第 84 步首轮协调服务，并把工作流和图版本写入、读取及核对 checkpoint。完成该图的首次运行和跨 Worker 恢复后，v3 Worker 才能声明领取能力。第二轮摘要输入、定向补查和停止条件继续独立交付。
