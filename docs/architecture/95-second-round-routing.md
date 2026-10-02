# v3 第二轮独立审查的确定性再次路由

## 目标与边界

第 94 步已经把第二轮开放假设及其独立审查保存为可恢复业务事实。本片从第二轮假设、第二轮审查和运行创建时冻结的协调容量策略重新编译路由，保存第 2 轮的 `follow_up`、`await_human` 或 `candidate_evaluation` 事实，使两轮调查拥有相同的确定性决策边界。

本片不调用新模型或工具，不创建第三轮计划或任务，不进入 LangGraph 人工暂停、候选结论、规划或处置，也不结算父调查。第二轮再次路由为 `follow_up` 只表示在固定轮次与任务容量内仍可补查，第三轮的计划编译和执行由后续切片处理。

## 图版本与结果契约

图 v9 已存在终态 checkpoint，因此固定为历史第二轮审查图。新运行发布图 v10，只在 `follow_up` 路径的第二轮独立审查后增加确定性路由节点：

```mermaid
flowchart LR
    A[图 v9 已有两轮路径] --> B[review_follow_up_hypothesis]
    B --> C[route_follow_up_review]
    C --> D[END]
```

首轮路由不是第二轮路由的替代品：`review_route` 继续绑定首轮假设和首轮审查，新增的 `follow_up_review_route` 只绑定第二轮假设及第二轮审查。补查路径必须携带第二轮路由；首轮直接进入 `await_human` 或 `candidate_evaluation` 时该字段必须为空。图最终返回前从业务数据库重新读取两轮路由，checkpoint 只保存恢复快照。

图 v1～v9 保留各自拓扑、状态和恢复语义。图 v10 的 metadata 与 state 同时固定 `workflow_version=v3`、`graph_version=dynamic-investigation-graph-v10`，不能用新图恢复旧 checkpoint。

## 确定性策略与容量

第二轮继续使用 `deterministic-review-routing-v1`，不新增模型判断或另一套路由规则。策略从目标轮次的开放假设、独立审查、冻结 `dynamic-coordination-v1` 策略和截至该轮的真实任务总数重建：

- 无反证、缺口或竞争解释且支持充分时进入 `candidate_evaluation`；
- 支持不足、补查建议完整且仍有轮次、单轮任务数和总任务数容量时进入 `follow_up`；
- 支持不足但缺少建议、容量不足，或支持充分但仍有实质异议时进入 `await_human`；
- 第 2 轮路由的 `remaining_rounds=max_rounds-2`，任务容量按第 1～2 轮已经持久化的任务计数，不从 checkpoint 或模型文本推断。

路由结果保留完整建议与固定原因码。它不授予执行权限，也不把审查意见描述为已证明根因。

## 持久化、查询与恢复

现有 `investigation_hypothesis_review_routes` 已以审查和业务轮次唯一，外键同时绑定假设、审查、轮次和运行，可直接保存第二条路由，无需新增业务表、字段或公开 API。路由服务抽取按轮内核，首轮公开入口保持兼容，第二轮入口只接受已绑定第二轮假设的审查事实。

持久化入口必须：

- 只允许图 v10 保存第二轮路由，图 v5～v9 继续只能产生各自已有事实；
- 从不可变审查、假设、协调策略、连续完成轮次和真实任务行重新编译决定；
- 在有效父租约事务内按审查稳定生成身份，已有事实只复核复用，冲突或来源漂移立即拒绝；
- 查询按业务轮次和稳定身份排序，不能依赖跨尝试的写入时间碰巧递增；
- 事实提交前中断时可在新父租约重新编译，事实提交后 checkpoint 前中断时复用原记录，两种情况都不增加模型、工具或预算调用。

租约失效、图能力不符、非连续轮次、未完成轮次、跨运行来源、策略或审查散列漂移、同轮重复路由以及 checkpoint 自报事实与数据库不一致时拒绝推进。

## 迁移与兼容

迁移 `0052_second_round_routing` 只把运行图版本约束扩展到 v10，不修改路由表结构。存在图 v10 运行，或存在绑定第 2 轮及以后轮次的路由事实时，拒绝降级到 `0051_second_round_review`；允许降级时恢复图 v1～v9 约束。旧路由、图 v1～v9 checkpoint 和 v1/v2 工作流不回填、不改写。

## 验收标准

- 首轮进入 `follow_up` 后，第二轮三种审查结果分别保存 `follow_up`、`await_human` 和 `candidate_evaluation`，两条路由按轮次稳定查询并绑定各自假设与审查。
- 首轮直接进入 `await_human` 或 `candidate_evaluation` 时不产生第二轮路由，父运行继续为 `running`。
- 第二轮容量从两轮真实任务与冻结策略重建；达到轮次或总任务上限时不会保存可自动执行的补查资格。
- 路由事实提交前和提交后 checkpoint 前两个窗口均可跨父租约恢复，且模型、工具和预算账本不变化。
- 图 v10 运行和第二轮路由事实分别触发降级保护；图 v1～v9 checkpoint、v3 启动和 Worker 隔离保持兼容，Alembic 唯一 head 为 `0052_second_round_routing`。
