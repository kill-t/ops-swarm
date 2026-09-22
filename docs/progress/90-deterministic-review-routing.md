# 90 - v3 确定性审查路由与路由事实持久化

## 目标

把已提交开放假设、独立审查与冻结协调策略编译为不可变路由事实，区分继续补查、等待人工与进入候选评估，并证明路由业务事实和 checkpoint 跨提交窗口可以幂等恢复。

## 设计与边界

详细设计见 [v3 确定性审查路由与路由事实持久化](../architecture/90-deterministic-review-routing.md)。图 v4 已有终态 checkpoint，本片把它固定为历史审查图，发布图 v5。路由器是框架无关的纯规则代码，不调用模型或工具，也不直接复制审查模型的建议方向。

候选评估要求审查认为支持充分、开放假设没有反证，且审查没有反证、证据缺口或竞争性解释。补查要求完整建议同时通过冻结的轮次、单轮任务与总任务容量；规则不截断或挑选部分建议。其他情况保存 `await_human`。三种决定都在本片结束图，父运行仍保持 `running`。

## 实际改动

- 新增 `ReviewRoutingDecision`、有限原因码、`ReviewRoutingRecord` 和纯函数编译器。决定保存规则版本、容量快照及完整补查建议，严格拒绝非规范原因顺序、计数或来源。
- 新增不可变 `investigation_hypothesis_review_routes` 表、来源重建、查询与应用服务。复合外键绑定同运行、同轮开放假设、审查事实、父尝试和固定协调策略散列；读取时重新统计既有任务并再次编译。
- 新增 `DynamicRoutingGraph` 与工厂，发布 `dynamic-investigation-graph-v5`。图 v4 固定为历史审查图；审查能力集合允许 v4/v5，综合能力集合允许 v3/v4/v5，旧图恢复语义不变。
- 路由事实保存前中断时恢复重新执行纯规则；事实保存后 checkpoint 前中断时直接复用业务事实。路由不新增调用账本记录、额度消耗、第二轮任务、人工暂停、规划或运行结算。
- 迁移 `0047_review_routing` 创建路由表和不可变触发器，扩展运行图版本约束；存在图 v5 或路由事实时拒绝降级到 `0046`。

## 测试方法与结果

验收日期：2026-09-22。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

新增路由领域单元测试 13 项；图 v5 集成测试 5 项覆盖候选、补查、等待人工、路由保存前中断、路由事实保存后 checkpoint 前中断及数据库不可变触发器。迁移测试覆盖图 v5 降级保护、图 v4 兼容回退链和唯一 Alembic head。图 v1～v4、启动/API 与 Worker 隔离参加定向兼容回归。

最终全仓 1190 项测试通过。Ruff 全仓检查通过，格式检查覆盖 681 个文件；mypy 检查 281 个源码文件无问题；Alembic 唯一 head 为 `0047_review_routing`。文档 UTF-8 与本地链接检查、`git diff --check` 通过。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_review_routing.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_routing_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_routing_graph.py tests/integration/test_dynamic_review_graph.py tests/integration/test_dynamic_synthesis_graph.py tests/integration/test_dynamic_first_round_graph.py tests/integration/test_dynamic_coordination_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py tests/unit/investigation/test_worker_routing.py tests/api/test_control_plane_api.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

`follow_up` 尚未创建第二轮，`await_human` 尚未进入持久等待或 LangGraph interrupt，`candidate_evaluation` 也不是最终根因、规划或执行资格。开放假设继续为 `pending_review`，审查和路由分别保持 `recorded`，父运行仍不结算，通用 Worker 继续跳过 v3。

下一片把 `follow_up` 路由保存的完整建议编译为第二轮协调计划与持久任务，继续共享原运行轮次、任务与调用预算；人工等待、候选结论和规划仍分别后置。

## 面试表达

> 我没有让审查 Agent 的建议直接决定工作流，而是增加独立的确定性路由层。候选路径同时检查开放假设和审查中的反证、缺口及竞争解释；补查则检查冻结轮次与任务容量，不能截断建议绕过上限。决定保存为不可变业务事实，图 v5 的 checkpoint 只承担恢复快照。这样既保留模型提出调查方向的灵活性，又让真正的状态路由可复核、可恢复且受代码约束。
