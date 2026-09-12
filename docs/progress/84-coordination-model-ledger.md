# 84 - 协调模型结构化调用与共享预算账本

## 目标

让协调 Agent 的首轮结构化提案经过现有共享预算和持久调用账本，并把成功调用作为动态计划的可恢复、可审计来源。基线为 main `5d3d702`，开始时工作区干净。

## 设计与边界

见 [专项设计](../architecture/84-coordination-model-ledger.md)。协调调用是根级第 0 槽位，无业务工具和执行权限；运行策略在外部请求前固定，模型提案在成功结算前还必须通过确定性编译。当前入口只供 v2 冻结运行的内部测试与后续图接入使用，不改变现有 Worker 路由和公开 API。

## 实际改动

- 新增 `coordinate` 调用操作、版本化协调输入/返回和最小上下文清单；预留事务从事故、固定策略和运行模型预算重建全部输入。
- 扩展结构化模型调用器，在保持旧调用接口兼容的同时保存协调结果和预留身份；协调调用继续消费现有次数、时限、Token 与估算费用额度。
- 新增首轮规划应用服务。业务无效提案在成功结算前拒绝；模型成功后、计划提交前崩溃可复用调用结果，计划提交后崩溃可直接返回原计划。
- 计划新增可空调用来源，并在查询时复核调用范围、状态、规范输入、上下文、分配、提案、时间和快照；旧无来源计划继续可读。
- 迁移 `0042_coordination_model_call` 增加复合来源外键及操作、范围、序号和 JSON 版本约束，存在新事实时拒绝降级；同步更新旧迁移保护测试对当前 head 的断言。

## 测试方法与结果

验收日期：2026-09-12。统一相关回归 162 项通过；审查修正后又执行 94 项最终定向回归并全部通过。测试使用 Testcontainers PostgreSQL 17.6 与假 OpenAI SDK 传输，不调用外部模型，也不升级用户 Compose 数据库。覆盖结构化调用、共享预算和用量、无工具上下文、计划来源、业务无效提案、跨父尝试调用复用、已提交计划复用、调用序号、旧计划兼容、来源篡改、租约、数据库约束、升级/降级以及全部既有迁移保护。

Ruff 全仓检查和格式检查通过，mypy 检查 252 个源码文件无问题；迁移唯一 head 为 `0042_coordination_model_call`，文档链接与 `git diff --check` 通过。

```powershell
$migration = rg --files tests/integration | rg 'migration\.py$'
uv run pytest -q tests/unit/investigation/test_call_budget_policy.py tests/unit/investigation/test_budgeted_model_caller.py tests/unit/investigation/test_context_builder.py tests/unit/investigation/test_coordination_policy.py tests/unit/investigation/test_task_round_identity.py tests/integration/test_coordination_plan_store.py tests/integration/test_coordination_planner.py tests/integration/test_call_budget_store.py tests/integration/test_model_budget_store.py tests/integration/test_context_reservations.py tests/integration/test_loop_ledger.py $migration
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run alembic heads
git diff --check
```

## 限制与下一步

当前只支持第一轮协调，运行仍以 v2 创建且旧图不会调用规划服务；没有第二轮摘要、定向补查、动态停止条件或新 checkpoint 版本。成功调用复用避免已知成功结果的重复请求，但失败或未知请求仍按至少一次语义处理并继续占用预算。

下一片增加独立动态工作流版本和运行创建时的完整策略冻结，再接入新的 LangGraph 图；之后以任务结果摘要驱动第二轮有界补查。

## 面试表达

> 我先把协调模型接入统一 Harness，而没有直接改旧图。协调调用在请求前固定策略并预留共享预算，结构化结果还要通过确定性业务编译才能记为成功。成功结果和计划用复合外键及来源重建绑定，所以 Worker 在模型返回后或计划提交后崩溃都能安全恢复，同时不会把越权提案永久复用。
