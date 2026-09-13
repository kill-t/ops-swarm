# 85 - 独立动态工作流版本与运行策略冻结

## 目标

为动态图建立不复用 v1/v2 checkpoint 的 v3 启动边界，让协调、上下文、证据、预算和图恢复策略在运行创建时原子固定并可查询，同时阻止现有 Worker 误领或回落到旧图。

## 设计与边界

详细设计见 [独立动态工作流版本与运行策略冻结](../architecture/85-dynamic-workflow-start.md)。v3 当前只允许冻结多 Agent 和 function_calling 模型预算；启动成功只代表运行与待处理任务已持久化，当前 Worker 不执行 v3。`graph_version` 表示图拓扑及状态恢复契约，LangGraph 自身的 PostgreSQL checkpoint schema 仍由原初始化流程管理。

## 实际改动

- 增加 `WorkflowVersion.V3` 和 `dynamic-investigation-graph-v1`。启动请求可提交受限协调策略；服务端固定摘要上下文、工作摘要、冻结证据、调用预算和完整模型预算。
- 在同一启动事务写入运行、工作项与不可变协调策略，运行响应和 GET 查询返回策略版本、图版本及复核后的完整协调策略。
- 迁移 `0043_dynamic_workflow_start` 增加两个运行版本字段，扩展 v3 的上下文、证据和预算约束，并以动态策略 CHECK 阻止缺字段、错误提示、非 function_calling 或新字段污染旧行；存在新事实时拒绝降级。
- 领取查询增加 Worker 支持版本过滤。现有 Worker 固定声明 v1/v2，可越过不兼容的 v3 继续领取；旧多 Agent 处理器和固定任务图工厂在 checkpoint 访问前再次拒绝 v3。
- 增加 API、持久化、队列隔离、处理器路由、数据库约束及升级/降级测试，并保留 v1/v2 启动、领取、固定图和 checkpoint 恢复回归。

## 测试方法与结果

验收日期：2026-09-13。最终全仓 1130 项测试通过；数据库测试使用 Testcontainers PostgreSQL 17.6，没有调用外部模型，也没有升级用户 Compose 数据库。此前定向启动/API/迁移场景 11 项、相关调查与 Worker 回归 81 项及迁移回归 7 项均通过，它们已包含在最终全仓范围内，不重复计数。

Ruff 全仓检查和 630 个文件格式检查通过，mypy 检查 252 个源码文件无问题；Alembic 元数据一致性、完整降级再升级、迁移唯一 head、174 份 Markdown 的结构及 494 个本地链接和 `git diff --check` 均通过。首次全仓运行暴露 19 个历史迁移保护仍断言旧 head；所有降级拒绝本身均正确，统一更新为 `0043_dynamic_workflow_start` 后第二次全仓运行全部通过，首次失败不计入验收。

```powershell
.venv/Scripts/pytest.exe -q tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/api/test_control_plane_api.py -k "dynamic_workflow or v3 or migration_preserves_v2 or start_v3"
.venv/Scripts/pytest.exe -q tests/integration/test_investigation_store.py tests/integration/test_fixed_task_graph.py tests/integration/test_checkpoint_recovery.py tests/integration/test_coordination_planner.py tests/api/test_control_plane_api.py tests/unit/investigation/test_worker_routing.py tests/unit/investigation/test_worker_main.py
.venv/Scripts/pytest.exe -q tests/integration/test_migrations.py tests/integration/test_dynamic_workflow_migration.py
.venv/Scripts/pytest.exe -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

## 限制与下一步

v3 运行会保持 queued，当前没有动态图节点、v3 checkpoint 或终态结算；第 84 步协调服务仍只在内部 v2 测试路径运行。现场 v3、第二轮上下文、定向补查、独立审查和人工暂停恢复均未交付。

下一片装配 `dynamic-investigation-graph-v1` 的首轮协调节点，严格核对运行/图版本后调用已有规划服务，并验证首次执行、崩溃恢复与旧 checkpoint 隔离；验收后才让对应 Worker 声明 v3 领取能力。

## 面试表达

> 我没有把动态图直接塞进已有 v2，因为 checkpoint 状态契约一旦变化，旧运行可能被错误恢复。我新增 v3 和独立 graph_version，在创建事务里固定协调、上下文、证据及预算策略，再让 Worker 通过 SQL 能力过滤领取。现有 Worker 会跳过 v3，处理器和旧图还有两道显式拒绝，因此新版本可先安全创建和查询，等动态图完成后再开放执行。
