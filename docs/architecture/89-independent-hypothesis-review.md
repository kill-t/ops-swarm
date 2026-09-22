# v3 独立假设审查与审查事实持久化

## 目标与边界

本片在首轮开放假设提交后增加一次独立审查。审查角色只读取该开放假设及其综合调用使用的同源首轮材料，结构化记录支持是否充分、已接受支持、反证、证据缺口、竞争性解释和建议方向。

审查意见不是最终根因、业务审批、确定性路由或执行资格。模型不能创建补查任务，不能设置任务参数、预算或工具，也不能进入处置阶段。本片完成后父调查仍保持 `running`；补查编译、路由循环、停止条件与父运行终态继续由后续切片实现。

## 图版本与恢复边界

图 v3 已可能保存终态 checkpoint，不能追加节点。本片将其固定为历史综合图，新运行固定图 v4：

```mermaid
flowchart LR
    start([START]) --> coordinate[coordinate]
    coordinate -->|Send 1～3 个任务| specialist[investigate_specialist]
    specialist --> synthesis[synthesize_open_hypothesis]
    synthesis --> review[review_open_hypothesis]
    review --> stop([END])
```

图 v1/v2/v3 分别保留仅协调、首轮专业调查、开放假设综合语义。图 v4 的 checkpoint metadata 与 state 同时固定并核对工作流 v3 和图 v4。最终返回前从业务数据库重建计划、专业结果、开放假设与审查事实。

## 审查契约与上下文

模型输出 `HypothesisReviewProposal`，包含：

- `support_is_sufficient`：审查角色对当前支持材料是否充分的意见；
- `accepted_supporting_evidence_ids` 与 `counter_evidence_ids`：当前材料中的支持与反证，必须唯一、互斥且同源；
- `evidence_gaps`：仍缺少的材料；
- `competing_explanations`：当前材料仍允许的有界竞争性解释；
- `suggested_direction`：`evaluate_for_routing`、`follow_up` 或 `human_review`；
- `follow_up_suggestions`：只有建议角色、问题和理由，不含可执行任务身份、窗口、工具或预算。

确定性校验要求“支持充分”至少接受一条支持证据；“支持不足”至少保留反证、缺口或竞争性解释；建议补查时至少提供一条补查建议。上述字段只表达审查意见。

审查使用独立根级 `review` 调用、`independent-review-v1` 角色版本和 `open-hypothesis-review-v1` 提示版本，不授予工具。审查清单必须与开放假设来源综合调用的任务材料完全相同，并只增加一个带内容散列的 `open_hypothesis` 材料，不能根据后来变化的数据库状态重新扩大范围。

## 持久化、预算与失败恢复

新增不可变 `investigation_hypothesis_reviews` 表，每个开放假设和每个成功审查调用最多生成一条事实。记录绑定运行、首轮、开放假设、提交它的父尝试和成功审查调用，并保存固定版本、`recorded` 状态、规范 JSON、内容散列与 UTC 时间。数据库复合外键、唯一约束和不可变触发器共同限制跨运行拼接、覆盖与删除。

审查共享原运行的次数、时限、Token 与费用预算，不创建新的额度。成功调用在账本保存 `hypothesis-review-result-v1`；持久层读取时重建开放假设、综合来源、审查输入和清单，复核引用闭包、规范 JSON、内容散列与时间顺序。

- 模型结果已成功写入账本而审查事实尚未提交时，新父尝试复用同一成功结果。
- 审查事实已提交而 checkpoint 尚未写入时，恢复直接返回并复核原事实。
- 同一运行出现多个成功审查调用、材料清单漂移、证据越界、租约失效或图版本不符时拒绝推进。

业务数据库与 checkpoint 不组成分布式事务，因此保证可恢复的业务幂等，不宣称外部模型请求物理上恰好一次。

## 迁移与接口

迁移 `0046_dynamic_review` 扩展图版本与调用账本约束，创建审查事实表和不可变触发器。存在图 v4 或审查事实时拒绝降级到 `0045`；图 v3 及更早运行继续按原语义恢复。

本片不新增 HTTP API，也不把通用 Worker 开放给 v3。查询能力先作为内部恢复和后续确定性路由的事实入口。
