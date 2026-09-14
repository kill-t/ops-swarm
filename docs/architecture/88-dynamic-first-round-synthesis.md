# v3 首轮协调综合与开放假设持久化

## 目标与边界

本片在动态首轮专业任务全部成功后执行一次根级协调综合。综合只读取首轮协调计划实际选择的 1～3 个任务及其已提交证据、结果和不可变工作摘要，输出一个尚待独立审查的开放假设。

开放假设保存自由文本陈述、模型置信度、支持证据、反证和未解问题，不包含 `RootCause`、处置建议或执行资格。旧 `hypotheses`、`hypothesis_evidence` 与确定性质疑继续只服务已完成的 v1/v2 Evidence Board；本片不改写其枚举和历史 API。

独立审查、竞争假设、补查路由、停止条件、父运行终态和通用 Worker 接入不在本片。首轮综合完成后父运行仍保持 `running`。

## 图版本与恢复边界

`dynamic-investigation-graph-v1` 已固定为仅协调，图 v2 已固定为协调加首轮专业任务。两者都可能存在终态 checkpoint，因此不能追加综合节点。本片保留旧工厂和拓扑，新运行默认固定图 v3：

```mermaid
flowchart LR
    start([START]) --> coordinate[coordinate]
    coordinate -->|Send 1～3 个已持久任务| specialist[investigate_specialist]
    specialist --> synthesize[synthesize_open_hypothesis]
    synthesize --> stop([END])
```

checkpoint metadata 和 state 同时记录并核对工作流 v3、图 v3。首轮计划、专业结果和开放假设都只是有界投影；最终返回前必须从业务库重建计划、任务材料、综合调用和假设记录。

## 综合输入与输出

v2 综合继续精确要求日志、指标、变更三个固定任务。v3 综合改为精确匹配首轮持久协调计划的 1～3 个任务，不能枚举未选择角色，也不能读取 checkpoint 中自报的额外任务。

`summary-context-v1` 仍是运行固定的上下文策略，但 v3 根级综合使用独立的 `open-hypothesis-synthesis-v1` 提示和 `dynamic-synthesis-v1` 角色版本。上下文清单包含事故输入以及每个已选任务的分派、证据快照和工作摘要；完整证据继续参与输入，未选任务不进入材料清单。预算预留事务重新加载并比较同一输入与清单。

模型输出先进入 `OpenHypothesisProposal`：

- `statement`：自由文本原因描述，不映射旧根因枚举；
- `confidence`：0～1 的模型判断，不代表审查通过；
- `supporting_evidence_ids`：至少一条当前首轮证据；
- `counter_evidence_ids`：当前材料中的反证，可为空，且不能与支持集合重叠；
- `unresolved_questions`：至少一个后续需审查或补查的问题。

证据引用必须唯一并属于首轮已选任务。确定性代码注入 `pending_review`，模型不能选择审查状态。

## 持久化与幂等

新增不可变表 `investigation_open_hypotheses`，每轮最多一条，保存稳定 UUID、运行、轮次、提交它的父尝试、成功综合调用来源、版本、`pending_review` 状态、完整假设 JSONB、内容散列和 UTC 创建时间。复合外键保证轮次、父尝试和调用都属于同一运行；更新或删除由数据库触发器拒绝。

根级 `synthesize` 调用继续共享运行次数、时限、Token 与估算费用预算。v3 成功调用在账本中保存 `open-hypothesis-synthesis-result-v1`，成为开放假设的可复核来源；v2 原综合调用仍不附加该结果。

读取假设时重建首轮协调计划和实际任务材料，复核调用输入、上下文清单、模型分配、规范 JSON、输出、证据范围、内容散列与时间顺序。任何漂移都返回稳定事实冲突。

## 失败路径

- 外部请求没有可确认结果时，调用按现有规则记为失败或未知；新父尝试消耗新的调用额度。
- 模型结果已成功写入调用账本而假设尚未提交时，新父尝试复用同一成功结果，不再次请求模型。
- 假设已提交而节点 checkpoint 尚未写入时，恢复直接复核并返回原假设，不增加调用或新记录。
- 同一运行出现多个成功综合结果、相同轮次出现不同假设、证据越界、任务范围变化、租约失效或图版本不匹配时拒绝推进。

业务数据库与 checkpoint 不组成分布式事务。本片实现可恢复的业务幂等，不宣称模型请求物理上恰好一次。

## 迁移与接口

迁移 `0045_dynamic_synthesis` 创建开放假设表，扩展调用账本成功综合结果约束，并允许图 v1/v2/v3 共存。存在图 v3、开放假设或其综合结果时拒绝降级；旧运行不回填。

本片不新增 HTTP API。开放假设先作为内部可查询业务事实交给后续审查切片，不能经旧 Evidence Board 或规划接口获得执行资格。
