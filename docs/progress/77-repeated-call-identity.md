# 77 - 同一尝试内的重复调用身份

## 目标

为模型工具循环补齐持久调用身份：同一专业尝试可以多次预留同一种操作，而不会把合法的第二次调用误判成第一次的重放。

## 设计与边界

详细规则见 [专项设计](../architecture/77-repeated-call-identity.md)。原计划将有界模型读取作为下一片；核对账本后发现其幂等键只包含父尝试、专业尝试和操作，因此先拆出这个可独立验收的数据库与运行器前置切片。LangChain 工具循环、新上下文版本和工具消息配对留在下一片，不把本片称为模型自主读取已完成。

新增 invocation_index 由运行层指定，默认 0 保留原键；非零值形成独立预留，不是模型可以修改的预算。数据库范围与键一致性约束防止无效身份落盘，原父运行行锁仍负责预算竞争。新槽位和技术重试不清空额度，unknown 也不当成零支出。

身份规则、仓储、运行器和表映射保持原职责。没有引入第二套调度器、循环实现或业务结果缓存；没有修改 task-context-v1、旧 ModelInput 或固定图行为。

## 实际改动

- 迁移 0037_call_invocation_index：旧调用补充 0 槽位并保留原事实，存在非零槽位时阻止降级。
- 仓储与 Runner 接收槽位，支持独立预留；重复执行同一槽位仍明确失败。
- 预算只读 API 返回 invocation_index，不开放预留写接口或泄露租约。
- 新增 21 项测试，覆盖相同槽位并发幂等、不同槽位争抢、四类模型预算、重放输入冲突、调用前拒绝、崩溃恢复、非法身份、迁移及只读查询。

## 测试方法与结果

验收日期 2026-09-08。定向测试 25 passed（33.72 秒）；统一回归 **448 passed，耗时 234.30 秒**。数据库使用隔离 PostgreSQL 17.6，模型与外部访问使用假实现；未执行真实模型或生产操作。

```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation tests/integration/test_repeated_calls.py tests/integration/test_call_identity_migration.py tests/integration/test_call_budget_store.py tests/integration/test_budgeted_call_runner.py tests/integration/test_budgeted_graph.py tests/integration/test_model_budget_store.py tests/integration/test_model_budget_migration.py tests/integration/test_model_budget_graph.py tests/integration/test_context_migration.py tests/integration/test_context_reservations.py tests/integration/test_context_graph.py tests/integration/test_fixed_task_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_migration.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_live_task_migration.py tests/integration/test_live_task_graph.py tests/api/test_call_budget_api.py tests/api/test_model_budget_api.py tests/api/test_context_manifest_api.py tests/api/test_context_evidence_api.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

Ruff、格式检查与 mypy（218 个源文件）通过；8 篇 Markdown 结构、258 个本地链接与 `git diff --check` 检查通过。

## 限制与下一步

现有固定图仍默认使用槽位 0。本片只交付调用身份与预算入口，不自动生成连续调用，不改变模型输入，也不保存工具消息配对。工具参数与来源绑定应随实际工具循环接入，不将调用槽位当作查询结果缓存键。

下一片接入专业 Agent 的有界 LangChain 工具循环、按需证据读取和新上下文策略，同时保存完整配对交互及逐次输入清单；之后再处理结构化工作摘要与动态协调。

## 面试表达

> 多步 Agent 调查不能把同一种工具的所有调用当成一条记录。我为每个执行尝试增加明确调用槽位，同槽位并发重放只预留一次，新槽位仍争抢同一运行预算。崩溃后未知调用保留扣账，换一个重试身份也不能重置额度。旧槽位兼容原幂等键，升级不会因此重复执行历史调用。这是后续接入工具循环时可核对、可恢复的调用基础。
