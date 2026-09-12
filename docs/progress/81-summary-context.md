# 81 - 工作摘要进入版本化上下文

## 目标

让综合模型实际消费第 80 步保存的工作摘要，并能回溯每次输入使用的版本和业务来源。基线为 main 的 `bb58588`，开始时工作区干净；先核对 AGENTS、项目状态、第 64/65 篇和第 80 步设计、进度及实现。

## 设计与边界

见 [专项设计](../architecture/81-summary-context.md)。新策略 `summary-context-v1` 需显式选择，固定摘要策略并要求 v2 与 function_calling 模型预算。综合接收三份完整 WorkingSummaryRecord 替换独立 reports 段，完整有界证据仍保留。报告中的反证和未解问题原文、调用失败或未知状态、目录与实际已读范围不被重新解释。

专业仍使用原证据读取提示和完整配对历史。综合本来就不接收专业聊天历史，因此本片不宣称历史压缩、减少模型调用或 Token 下降。旧策略输入、默认选择、图状态和 checkpoint 语义保持兼容。

## 实际改动

- 分离纯来源核对与规范散列，保持旧 ContextBuilder 的公开散列导入和组装行为。
- 新摘要构建器处理图传入报告与预留时实际摘要输入两种互斥形态，从持久来源重建规范输入；仅最终完整输入参与预算检查，超限明确拒绝。
- 材料清单以摘要引用替换结果引用，仍为十项，保留任务、证据、摘要版本及散列。综合无新增业务工具。
- 加载器及预算预留事务均重算摘要来源；输入或清单篡改、摘要丢失、源调用损坏时不新增预留。
- 专业循环按新策略保留原提示、消息与工具；运行固定摘要策略。迁移 0040 扩展约束，要求新策略非空摘要版本，旧行不回填，存在新策略或调用提示审计时禁止降级。
- 开发子智能体按纯构建器、迁移及只读审查、集成测试三个独立边界协作；主 Agent 核对实现、修正边界断言并统一执行验证和提交。

## 测试方法与结果

验收日期：2026-09-12。测试使用真实 LangChain/SDK 序列化与假传输、Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，不调用外部模型，不升级用户 Compose 业务库。

统一相关回归 **633 项通过，耗时 486.05 秒**；此前 40 项定向测试和提示模块调整后的 27 项复核均与统一范围重叠，不重复计数。Ruff 检查通过，601 个文件格式检查通过，mypy 检查 242 个源码文件无问题；迁移唯一 head 为 `0040_summary_context`。9 篇更新文档及 342 个本地链接验证通过，`git diff --check` 通过。覆盖冻结/现场双事故隔离、SDK 实际摘要与完整证据、未读目录、反证属性、未知调用引用、摘要提交后恢复、输入/清单/源调用篡改与缺失摘要、预算拒绝、策略 API 和迁移保护。

独立兼容性核对从基线 Git 读取原构建器，对相同材料比较 task-context-v1 专业/综合及 evidence-read-v1 综合，在三种结构化输出方式下的 9 组完整 packet 序列化完全一致；新旧专业首轮模型输入与 allocation 也相同。保留原单元和集成回归作为可重复验证入口。

首轮定向测试有三处新增断言问题：预算异常应检查稳定 code 而非中文异常文本，长中文测试材料尚未达到预期长度。修正并重跑后通过。Docker 启动和本机命令启动较慢期间未把未完成测试记为通过。

```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation tests/integration/test_summary_context_graph.py tests/integration/test_summary_context_reservations.py tests/integration/test_summary_context_migration.py tests/integration/test_working_summary.py tests/integration/test_working_summary_migration.py tests/integration/test_working_summary_graph.py tests/integration/test_investigation_task_store.py tests/integration/test_task_result_transactions.py tests/integration/test_evidence_loop.py tests/integration/test_loop_graph.py tests/integration/test_loop_ledger.py tests/integration/test_loop_migration.py tests/integration/test_repeated_calls.py tests/integration/test_call_identity_migration.py tests/integration/test_call_budget_store.py tests/integration/test_budgeted_call_runner.py tests/integration/test_budgeted_graph.py tests/integration/test_model_budget_store.py tests/integration/test_model_budget_migration.py tests/integration/test_model_budget_graph.py tests/integration/test_context_migration.py tests/integration/test_context_reservations.py tests/integration/test_context_graph.py tests/integration/test_fixed_task_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_migration.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_live_task_migration.py tests/integration/test_live_task_graph.py tests/api/test_working_summary_api.py tests/api/test_call_budget_api.py tests/api/test_model_budget_api.py tests/api/test_context_manifest_api.py tests/api/test_context_evidence_api.py tests/api/test_loop_api.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py tests/api/test_task_evidence_api.py tests/api/test_live_task_api.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

完整摘要与证据可能使输入更大；必要材料超过窗口或预算直接停止，不隐式裁剪。没有独立反证/未解问题字段、动态协调或 MCP，也没有新真实模型/现场发布验收。

下一片为第 82 步动态调查任务计划与确定性分派校验，先明确协调器计划契约、角色权限、资源上限和持久化边界。专业交互历史裁剪如有需要，应另行定义完整配对替换规则。

## 面试表达

> 我把已完成专业任务的不可变摘要通过独立策略接入综合模型。摘要包含原报告、已读范围、失败调用引用和来源散列；综合还保留完整证据，避免遗漏反证。模型请求前的预留事务会重新核对材料，并把实际输入与清单一起保存。测试在 SDK 接收端证明模型确实收到了摘要，覆盖崩溃恢复和篡改拒绝；旧策略保持原输入，不把摘要消费误称为已经节省 Token。
