# v3 动态首轮任务分派与专业结果恢复

## 目标与边界

本片让 v3 在首轮协调计划落库后，只执行计划实际选择的 1～3 个专业任务。每个任务继续使用既有任务尝试、冻结取证快照、LangChain 专业工具循环、运行级共享预算和不可变工作摘要；首轮专业结果完成仍不是最终调查结果。

综合、独立审查、停止条件、第二轮补查和父运行终态结算不在本片。通用 Worker 在这些节点及最终业务校验完成前继续只领取 v1/v2。

## 图版本与状态

第 86 步的 `dynamic-investigation-graph-v1` 已固定为 `START → coordinate → END`，其终态 checkpoint 不包含专业分支。直接给该版本追加节点会让旧 checkpoint 获得不兼容含义。因此本片保留 v1 协调图及恢复工厂，新运行改为固定 `dynamic-investigation-graph-v2`；数据库同时允许已有 v1 与新 v2 事实，但默认只创建 v2。

v2 图使用以下拓扑：

```mermaid
flowchart LR
    start([START]) --> coordinate[coordinate]
    coordinate -->|Send 1～3 个已持久任务| specialist[investigate_specialist]
    specialist --> stop([END])
```

协调节点仍通过既有规划服务创建或复用计划。路由按计划内持久任务键排序后生成 LangGraph `Send`，每个分支携带稳定的轮次、任务和分派身份；它不枚举三个固定角色，也不使用 LangGraph 内部任务标识充当业务身份。图的最大并发数为 3。

checkpoint 保存请求、工作流版本、图版本、协调计划以及已完成专业结果的有界投影，不保存父租约或任务尝试令牌。恢复前同时检查 metadata 和 state 的双版本；最终返回前重新从业务库复核计划、任务、结果、证据和摘要。

## 任务执行与恢复

协调计划已经在一个业务事务中创建轮次和任务，动态任务运行器不得再次调用固定图的 `prepare()`。它以 `round_id + task_key + 完整 assignment` 解析业务任务，然后按以下规则推进：

1. `pending/running` 任务在当前父租约下创建新尝试；旧租约的运行中尝试由现有任务仓储收敛为过期。
2. 任务尝试的作用域注入共享调用预算、取证保存和专业循环，成功结果与工作摘要在同一任务结算事务中提交。
3. 已成功任务直接读取并复核原结果、证据和摘要，不创建新尝试，不再次查询来源或调用模型。
4. 专业失败按既有可重试规则结算。并行 superstep 失败后，新父租约恢复同一 checkpoint；LangGraph 复用成功分支的 pending writes，业务库幂等检查再提供第二层保护。
5. 业务结果已提交但节点 checkpoint 尚未写入时，恢复即使重新进入该分支，也只能返回原结果。

checkpoint 与业务 PostgreSQL 仍不组成分布式事务。该设计保证已提交业务事实不会重复；请求已发出但未成功结算的外部模型结果仍可能未知，不能表述为 exactly-once。

## 取证范围与上下文审计

每个专业节点根据持久 `TaskAssignment.window_start/window_end` 计算现有来源端口所需的分钟或小时参数，并在返回后按该 UTC 窗口和专业角色再次过滤。首版 v3 仍只接受冻结来源；现场动态查询和 MCP 属于后续切片。

专业模型只接收当前任务的持久证据目录，并继续通过 `read_evidence` 读取授权详情。上下文来源新增真实 `workflow_version`，所有上下文清单显式保存 v2 或 v3；预算预留时从业务运行重建并比较，避免 v3 专业调用沿用默认 v2 审计标识。综合上下文中固定三任务的旧规则暂不调用，也不在本片放宽。

## 持久化与接口

迁移 `0044_dynamic_graph_fanout` 只扩展 `investigation_runs.graph_version` 的允许集合，不改表结构。降级到 0043 前若存在 v2 图运行则拒绝，以免把已保存恢复契约变成非法数据。旧 v1/v2 工作流、v3 图 v1 运行及其 checkpoint 保持可读和可恢复。

本片不增加 HTTP API。对外运行仍保持 `running` 且没有最终 result；首轮完成只表示协调选择的专业任务均形成了可复核结果和摘要。
