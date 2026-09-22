# v3 确定性审查路由与路由事实持久化

## 目标与边界

本片把已提交开放假设、独立审查和冻结协调策略编译为一份不可变路由事实，区分 `follow_up`、`await_human` 与 `candidate_evaluation`。路由由领域代码决定，审查模型的 `suggested_direction` 只参与契约一致性校验，不能直接改变业务状态。

`candidate_evaluation` 只表示允许后续候选结论继续评估，不代表根因成立、进入规划、获得审批或具备执行资格。`await_human` 只是一份路由事实，不等于已经进入 LangGraph 人工暂停。`follow_up` 只保留经规则允许的补查意图，不创建轮次、任务、窗口、工具或预算。

## 图版本与恢复边界

图 v4 已可能保存终态 checkpoint，不能追加节点。本片将其固定为历史审查图，新运行固定图 v5：

```mermaid
flowchart LR
    start([START]) --> coordinate[coordinate]
    coordinate --> specialist[investigate_specialist]
    specialist --> synthesis[synthesize_open_hypothesis]
    synthesis --> review[review_open_hypothesis]
    review --> route[determine_review_route]
    route --> stop([END])
```

图 v1/v2/v3/v4 分别保留仅协调、首轮专业调查、开放假设综合和独立审查语义。审查上下文与审查事实允许图 v4/v5 重建；路由事实只允许图 v5。图 v5 的三种决定在本片都进入 `END`，父运行保持 `running`，通用 Worker 继续不领取 v3。

路由没有外部调用，恢复只跨业务事实与 checkpoint 的提交窗口：路由事实提交前失败时重新运行纯规则；事实已提交而 checkpoint 尚未写入时复用并完整复核原事实；终态返回前仍从业务库重建计划、专业结果、开放假设、审查和路由。

## 确定性规则

路由规则版本固定为 `hypothesis-review-routing-v1`，按以下顺序执行：

1. 运行、图版本、轮次、开放假设、审查、协调策略、父租约或内容散列不一致时拒绝落库。
2. 审查认为支持充分时，只有开放假设没有反证，且审查没有反证、证据缺口或竞争性解释，才进入 `candidate_evaluation`；否则进入 `await_human`。
3. 审查认为支持不足且提出补查意图时，当前轮次必须小于 `max_rounds`，全部建议数量不得超过 `max_tasks_per_round`，现有任务数加全部建议不得超过 `max_total_tasks`。全部满足才进入 `follow_up`，不截断或挑选部分建议。
4. 审查明确请求人工、没有合法补查意图或任一轮次/任务额度不足时进入 `await_human`。

开放假设当前强制保留至少一个 `unresolved_questions`，因此该字段不作为 v1 候选路径的硬阻断条件；它继续随假设供后续候选结论判断。路由只检查冻结的轮次和任务容量，实际模型、工具、Token、费用与时限仍由后续调用预留再次强制检查。

路由内容保存有限原因码、决策时的轮次号、现有任务数和被完整接受的补查建议。支持充分但仍有反证、缺口或竞争性解释时安全转人工，不能在没有补查建议的情况下擅自生成任务。

## 持久化与来源复核

新增不可变 `investigation_hypothesis_review_routes` 表。每条记录绑定运行、轮次、开放假设、独立审查、提交它的父尝试、固定协调策略散列和规则版本，并保存固定状态、路由 JSON、内容散列与 UTC 时间。每份审查最多生成一条路由事实；稳定 UUID 与唯一约束保证相同重放复用，冲突内容拒绝覆盖。

读取时从审查继续重建开放假设、综合调用、首轮任务与证据，再读取同运行协调策略和全部轮次任务数量，重新执行 `hypothesis-review-routing-v1`。保存内容、来源散列、策略散列、规范 JSON 或时间顺序任一不一致都视为事实冲突。数据库复合外键阻止跨运行、跨轮或跨假设拼接，触发器拒绝更新和删除。

迁移 `0047_review_routing` 增加图 v5、审查事实复合候选键和路由表。存在图 v5 运行或路由事实时拒绝降级到 `0046`；图 v4 及更早运行仍按原语义恢复。本片不增加模型调用操作、HTTP API、事件或父运行终态。

## 验收标准

- 纯领域测试覆盖候选、补查、明确人工、审查冲突、轮次耗尽、单轮容量不足和总任务耗尽。
- 真实 PostgreSQL 验证来源复核、幂等重放、冲突写入、过期租约、不可变触发器及迁移升降级。
- 图 v5 覆盖三类路由与 checkpoint 提交窗口恢复，证明不增加模型/工具调用、不创建第二轮任务且父运行保持 `running`。
- 图 v1/v2/v3/v4、启动/API、Worker 隔离、Alembic head、全仓测试与静态检查保持通过。
