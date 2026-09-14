# 87 - v3 动态首轮任务分派与专业结果恢复

## 目标

让 v3 在首轮协调计划形成后只执行模型选择并通过确定性编译的 1～3 个专业任务，并证明任务尝试、冻结证据、专业工具循环、共享预算、工作摘要和 checkpoint 能在并行失败及跨库提交窗口中一致恢复。

## 设计与边界

详细设计见 [v3 动态首轮任务分派与专业结果恢复](../architecture/87-dynamic-first-round-fanout.md)。第 86 步的图 v1 已固定为仅协调拓扑，本片保留其恢复能力，为新增专业 fan-out 启用图 v2。首轮任务完成仍不生成综合结论、审查意见或父运行终态，通用 Worker 继续不领取 v3。

## 实际改动

- 新增 `DynamicFirstRoundGraph`，协调后按持久计划中稳定排序的任务生成 1～3 个 LangGraph `Send`，同一专业节点受限并发执行；返回前重新校验 Pydantic 阶段契约及业务库中的计划、任务、证据、结果和摘要。
- 提取固定/动态图共用的持久任务执行内核，并新增 `DynamicTaskRuntime`。动态图不调用固定三任务 `prepare()`，而以 `round_id + task_key + 完整 assignment` 获取任务；成功任务只读取原结果，失败任务在新父租约下使用原任务身份和新尝试。
- 动态节点按每个 assignment 的 UTC 窗口调用冻结来源并再次过滤材料，接入既有 `TaskEvidenceStore`、`BudgetedCallRunner`、`EvidenceLoopAnalyst` 和工作摘要同事务结算。未选角色没有任务、取证或模型调用。
- 上下文来源携带真实 v2/v3 工作流版本，旧文本上下文、专业循环和摘要上下文清单都显式写入该版本；预算事务继续从业务来源重建并比较清单。
- 增加 `dynamic-investigation-graph-v2`，新 v3 运行默认固定 v2。迁移 `0044_dynamic_graph_fanout` 允许旧图 v1 与新图 v2 并存，存在 v2 图事实时拒绝降级；协调上下文和计划持久化同时兼容两个图版本。
- 增加真实 PostgreSQL 与 checkpoint 集成测试，覆盖动态 1/2/3 角色、独立回看窗口、窗口外材料过滤、终态恢复、并行单分支失败、新租约只恢复必要节点、业务结果已提交但节点 checkpoint 未写，以及图版本迁移兼容。

## 测试方法与结果

验收日期：2026-09-14。最终全仓 1141 项测试通过；数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。新增动态图 5 个场景、图版本迁移及旧图/固定图/摘要上下文/API 定向回归均包含在全仓结果内，不重复计数。

Ruff 全仓检查和 641 个文件格式检查通过，mypy 检查 256 个源码文件无问题；Alembic 唯一 head 为 `0044_dynamic_graph_fanout`。Markdown 结构、UTF-8、本地链接和 `git diff --check` 均按提交前检查通过。开发中首次并行失败测试发现 SDK 会把假传输异常包装为模型连接异常，测试随后改为断言真实适配器异常类型；这是测试契约修正，不计为验收通过。

独立只读审查另行执行了动态图、旧协调图和图版本迁移共 15 项集成测试，并复核版本隔离、并行恢复、持久任务幂等、上下文版本及迁移降级，未发现阻止提交的问题，也未修改工作区。

```powershell
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_first_round_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_first_round_graph.py tests/integration/test_dynamic_coordination_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_loop_graph.py tests/integration/test_summary_context_graph.py tests/integration/test_summary_context_reservations.py tests/integration/test_checkpoint_recovery.py tests/integration/test_fixed_task_graph.py tests/integration/test_live_task_graph.py tests/api/test_control_plane_api.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

v3 当前只完成首轮计划和所选专业任务，父运行仍保持 running。图 v2 尚未综合工作摘要、形成开放假设、执行独立审查、决定停止或创建第二轮补查；通用 Worker 因而继续只领取 v1/v2。首版动态来源仍是冻结场景，分钟/小时来源参数用于既有端口，现场动态查询和 MCP 后续单独交付。

下一片实现 v3 首轮协调综合与开放假设持久化：让综合只消费本轮实际选择任务的工作摘要和证据，保存可追溯但尚未审查的假设，不复用固定三报告与既有根因枚举来伪造执行资格。独立审查、补查路由和最终结算继续分别验收。

## 面试表达

> 我没有直接修改已经有 checkpoint 的图 v1，而是把动态 fan-out 放到新图 v2。协调计划落库后，LangGraph 按稳定任务键 Send 1～3 个分支，每个分支仍在业务任务尝试、证据快照和共享预算内运行专业工具循环。并行失败时 checkpoint 复用成功分支，业务库又保证已提交结果不会重复；我还测试了结果已提交但 checkpoint 未写的窗口。因为当前只有首轮专业结果，我保留了 v3 Worker 隔离，没有把阶段完成误报成最终调查成功。
