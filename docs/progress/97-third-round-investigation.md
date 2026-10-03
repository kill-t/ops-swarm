# 97 - v3 第三轮专业任务执行与恢复

## 目标

执行第 96 步已经持久化的第 3 轮协调计划，使冻结策略允许的最大调查轮次具备可恢复的专业结果和工作摘要，并保持前两轮事实、原运行共享预算与父调查状态不变。

## 设计与边界

详细设计见 [v3 第三轮专业任务执行与恢复](../architecture/97-third-round-investigation.md)。图 v11 冻结为只创建第三轮计划的历史图，新发布图 v12 在同一计划节点后按稳定任务键分派第 3 轮专业分支；每个分支显式要求 `expected_round_number=3`，任务运行层继续核对轮次、角色、冻结快照和父租约。

第三轮使用独立 `third_round_results`，避免同一角色在三轮之间覆盖。它复用现有任务尝试、证据、调用账本、专业工具循环、结果、工作摘要和事件，没有新增业务表、字段或公开 API。第二轮路由不是 `follow_up` 时，第三轮计划与结果都为空。

本片不执行第三轮综合、独立审查或路由，不进入 LangGraph 人工暂停、候选结论、规划或处置，也不结算父调查。第三轮专业报告只是已提交调查材料，不是根因证明或执行资格。

## 实际改动

- 新增 `ThirdRoundExecutionResult`，严格校验第三轮结果与计划角色、冻结快照一一对应；没有计划时禁止携带结果。
- 新增 `DynamicThirdRoundGraph` 与 v12 装配工厂，从已重建计划稳定 `Send` 第三轮任务，并把 checkpoint state 与 metadata 固定到 v12。
- 图 v11 改用冻结版本常量，旧拓扑、结果边界和恢复语义保持不变；新启动运行固定到 v12。
- 第三轮复用 `DynamicTaskRuntime`。同轮部分失败后，新父租约只为失败任务创建新尝试；结果和摘要已提交但 checkpoint 未写时直接读取数据库事实，不重复取证或调用模型。
- 第三轮全部成功后只结算第 3 轮为 `completed`，父调查继续保持 `running`。默认预算沿用原运行额度，测试中的多分支场景显式扩大额度，没有为新轮次隐式重置预算。
- 迁移 `0054_third_round_investigation` 只扩展运行图版本约束。存在图 v12 运行，或存在第 3 轮及以后任务尝试时拒绝降级；后者保证改写运行图版本也不能绕过执行事实保护。

## 测试方法与结果

验收日期：2026-10-03。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 业务数据库。

图 v12 的 6 项集成测试覆盖第二轮三种路由、第三轮双专业分支的部分失败接管、结果提交后 checkpoint 前恢复，以及运行图版本被改回 v11 后仍由第三轮任务尝试阻止迁移降级。断言第三轮任务、尝试、证据、结果和工作摘要均绑定第 3 轮，成功分支不重跑，共享预算只增加失败任务的新调用，第 3 轮完成而父运行保持 `running`。

兼容回归覆盖冻结图 v11 的 8 项测试、v3 启动 3 项、动态图迁移 16 项和完整迁移链 3 项，共 30 项真实 PostgreSQL 测试；当前控制面图版本目标测试通过。全仓 702 项单元测试和 127 项 API 测试通过。

最终验收由以下命令复现：

```powershell
uv run pytest tests/unit -q
uv run pytest tests/api -q
uv run pytest tests/integration/test_dynamic_third_round_graph.py -q
uv run pytest tests/integration/test_dynamic_third_round_planning_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py -q
uv run pytest tests/api/test_control_plane_api.py::test_start_v3_investigation_returns_frozen_dynamic_contract -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run alembic heads
git diff --check
```

实际结果：Ruff 检查通过，723 个文件符合格式；Mypy 检查 291 个源文件无问题；Alembic 唯一 head 为 `0054_third_round_investigation`。199 份项目 Markdown 的 UTF-8 解码、597 个本地链接和代码块闭合检查通过，`git diff --check` 无错误。两个只读范围审查在实现前独立核对了状态隔离、预算、恢复和迁移保护边界，结论一致；主 Agent 仍以实际代码和真实 PostgreSQL 结果完成最终验收。

## 限制与下一步

第三轮结果与工作摘要已持久化，但尚未形成第三轮开放假设、独立审查或最大轮次停止决定。`await_human` 与 `candidate_evaluation` 仍未进入各自图内流程，通用 Worker 仍跳过 v3。

下一片只使用第三轮已提交工作摘要和完整证据执行根级综合，保存绑定第 3 轮计划的新开放假设。第三轮独立审查、人工介入、候选结论、规划和父运行结算继续后置。

## 面试表达

> 多轮调查不能只把同一个结果列表继续追加，否则相同专业角色会在恢复时混轮。我为第三轮保留独立状态，并让每个分支同时核对持久计划和业务轮次。成功分支先落库，另一个分支失败后只重试失败任务；即使结果落库后 checkpoint 尚未写入，恢复也直接复用业务事实。迁移保护还检查第三轮尝试，因此改写运行图版本不能掩盖已经发生的执行。
