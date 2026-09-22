# 89 - v3 独立假设审查与审查事实持久化

## 目标

让 v3 在首轮开放假设提交后执行一次独立、无工具的结构化审查，保存支持是否充分、接受的支持证据、反证、证据缺口、竞争性解释和建议方向，并证明调用账本、业务事实与 checkpoint 跨提交窗口能够幂等恢复。

## 设计与边界

详细设计见 [v3 独立假设审查与审查事实持久化](../architecture/89-independent-hypothesis-review.md)。图 v3 已有终态 checkpoint，本片把它固定为历史综合图，为审查节点发布图 v4。审查输入必须精确复用开放假设来源综合调用的首轮材料，只增加一个开放假设材料；审查角色没有工具权限。

`support_is_sufficient` 和 `suggested_direction` 都是 Agent 意见。`evaluate_for_routing` 不代表根因已被证明，`follow_up_suggestions` 也不创建任务。本片不实现确定性路由、补查任务、人工等待、规划或父运行结算；审查结束后父运行仍保持 `running`，通用 Worker 继续不领取 v3。

## 实际改动

- 新增 `HypothesisReviewProposal`、系统注入状态后的 `HypothesisReview` 和记录契约。引用必须唯一、互斥且处于首轮证据闭包；接受的支持还必须来自原开放假设。支持不足时必须保留反证、缺口或竞争性解释，补查建议只有角色、问题和理由。
- 新增独立 `review` 根级模型调用、`independent-review-v1` 角色版本、`open-hypothesis-review-v1` 提示和 `hypothesis-review-result-v1` 账本结果。调用共享原运行的次数、时限、Token 与估算费用预算，不获得业务工具。
- 新增审查上下文构建器。它从业务库重建开放假设、原综合调用清单、首轮任务、证据与工作摘要，要求审查材料与综合材料完全一致，再附加开放假设 ID、版本和散列；预留事务会再次重建核对。
- 新增不可变 `investigation_hypothesis_reviews` 表、查询、持久化与 `IndependentHypothesisReviewer`。复合外键绑定同运行、同轮开放假设、父尝试和成功审查调用，每个开放假设及调用至多一条审查事实，数据库拒绝更新和删除。
- 新增 `DynamicReviewGraph` 与工厂，发布 `dynamic-investigation-graph-v4`；旧图 v1/v2/v3 分别保持仅协调、专业首轮和开放假设综合语义。图 v4 在综合后执行 `review_open_hypothesis`，返回前从业务事实重建全部阶段结果。
- 模型成功后审查事实保存前中断时复用成功调用；审查事实保存后 checkpoint 前中断时直接复用业务事实。迁移 `0046_dynamic_review` 同步扩展图版本、调用 CHECK、复合来源和安全降级。

## 测试方法与结果

验收日期：2026-09-22。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

新增审查领域单元测试 11 项；新图集成测试 5 项覆盖 1/2/3 个动态角色、同源清单、审查事实与两个崩溃窗口。迁移测试覆盖图 v4 拒绝降级、图 v3 兼容回退、旧图 v2 降级链和唯一 Alembic head。旧图 v1/v2/v3、启动/API、预算与 Worker 隔离均参加定向及全仓回归。

最终全仓 1171 项测试通过。Ruff 全仓检查通过，格式检查覆盖 668 个文件；mypy 检查 273 个源码文件无问题；Alembic 唯一 head 为 `0046_dynamic_review`。文档 UTF-8 与本地链接检查、`git diff --check` 通过。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_hypothesis_review.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_review_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_review_graph.py tests/integration/test_dynamic_synthesis_graph.py tests/integration/test_dynamic_first_round_graph.py tests/integration/test_dynamic_coordination_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py tests/unit/investigation/test_worker_routing.py tests/api/test_control_plane_api.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

当前审查事实只是可审计意见。系统尚未将建议方向编译为确定性路由，也不会创建第二轮任务、等待人工或进入规划；开放假设继续保持 `pending_review`，是否已有审查由关联事实推导。通用 Worker 仍跳过 v3，当前只在冻结场景和 SDK 假传输中验收。

下一片保存确定性审查路由事实：根据已提交审查意见与固定规则选择继续补查、等待人工或进入后续候选路径，同时保持任务创建、LangGraph 人工暂停和规划为后续独立切片。

## 面试表达

> 我没有把审查节点直接追加到已经发布的图 v3，而是发布图 v4，保留旧终态 checkpoint 的恢复语义。审查 Agent 只有独立提示和结构化输出，没有工具；它的上下文清单必须等于原综合清单再加当前开放假设，因此不能偷偷扩大证据范围。模型只能表达支持充分性、反证、缺口、竞争解释和建议方向，系统把意见保存为独立不可变事实，不修改开放假设，也不把审查通过当成根因证明。调用结果与审查事实分开落库，通过两种恢复窗口测试证明了业务幂等。
