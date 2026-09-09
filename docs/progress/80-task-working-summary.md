# 80 - 已完成专业任务的结构化工作摘要

## 目标

让已完成专业调查拥有可持久查询、可追溯的任务工作材料，为下一片上下文消费建立可信来源。沿第 79 步交接核对 AGENTS、项目状态、第 64/65 篇与 Git，起点 main 9475621，工作区干净。

## 设计与边界

见 [专项设计](../architecture/80-task-working-summary.md)。本片生成摘要材料，不同时引入新的模型上下文策略或历史裁剪。新建 evidence-read-v1 运行固定独立摘要策略，迁移前运行及其他策略保持 null。摘要从已经持久化的任务报告与调用交互确定性生成，不增加模型调用。

摘要完整保留报告与 findings，包括其中原有的反证、未解问题表达；不把现有自由文本分类成不存在的专门字段。证据目录和当前成功尝试的已读 ID 分开；失败/unknown 调用保留顺序、槽位、类型、状态、错误码及调用 ID，已保存的按需读取参数和交互按引用查询，原始取证调用只保留现有账本状态与任务范围。它不是长期经验、当前根因证明或执行授权。

## 实际改动

- 新增摘要契约、规范编码/散列、纯投影规则、持久服务和不可变表，各自承担清晰职责。
- 运行固定材料策略；成功专业结果、任务状态、摘要与事件同事务提交。任何缺失、错引用或超限都回滚成功状态。
- 成功结算幂等重放、成功结果恢复及父运行完成均核对摘要来源；查询不写入，也不为旧运行回填。
- 抽出账本配对历史读取供原循环与摘要共用，旧 ledger 导入保持兼容，原输入/提示/工具协议不变。
- 迁移 0039 绑定任务/尝试，限制摘要版本、体积、散列，数据库阻止更新/删除并保护降级。
- 新增 working-summary GET；合法任务尚无材料或旧策略返回 null，跨任务返回 404，来源冲突返回 409。启动/运行响应包含只读摘要策略。
- 两名子智能体按独立边界处理设计审查/单元测试初稿与数据库映射/迁移；后续测试由主 Agent 收回补齐、独立核查并统一验收。

## 测试方法与结果

验收日期：2026-09-09。使用真实 LangChain/SDK 与假传输、Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库；不调用真实模型，不迁移用户 Compose 业务库。

统一验收 **599 项测试通过，耗时 342.45 秒**。此前 29 项定向测试与统一范围重叠，不额外相加。Ruff 检查通过，589 个文件格式检查通过，mypy 检查 238 个源码文件无问题；迁移唯一 head 为 `0039_task_working_summary`。9 篇更新文档及 330 个本地链接验证通过，`git diff --check` 通过。测试包括完整报告/目录已读区别、失败尝试后成功、并发精确结算、原子回滚、失效租约、摘要提交后 checkpoint 前崩溃恢复、跨事故隔离、来源损坏、旧策略、API 只读和迁移保护。测试准备曾遇到 Docker 后端不可用；重新启动并确认连接后重跑，不计作通过。Windows 大字段参数化测试使用短用例 ID，避免超过环境变量上限。验证中修复运行响应遗漏摘要策略映射，并通过提取配对历史读取消除循环导入；失败记录不计入最终通过数。

```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation tests/integration/test_working_summary.py tests/integration/test_working_summary_migration.py tests/integration/test_working_summary_graph.py tests/integration/test_investigation_task_store.py tests/integration/test_task_result_transactions.py tests/integration/test_evidence_loop.py tests/integration/test_loop_graph.py tests/integration/test_loop_ledger.py tests/integration/test_loop_migration.py tests/integration/test_repeated_calls.py tests/integration/test_call_identity_migration.py tests/integration/test_call_budget_store.py tests/integration/test_budgeted_call_runner.py tests/integration/test_budgeted_graph.py tests/integration/test_model_budget_store.py tests/integration/test_model_budget_migration.py tests/integration/test_model_budget_graph.py tests/integration/test_context_migration.py tests/integration/test_context_reservations.py tests/integration/test_context_graph.py tests/integration/test_fixed_task_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_migration.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_live_task_migration.py tests/integration/test_live_task_graph.py tests/api/test_working_summary_api.py tests/api/test_call_budget_api.py tests/api/test_model_budget_api.py tests/api/test_context_manifest_api.py tests/api/test_context_evidence_api.py tests/api/test_loop_api.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py tests/api/test_task_evidence_api.py tests/api/test_live_task_api.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

没有测量压缩率、Token 或费用下降。摘要尚未进入模型输入，也没有裁剪交互或新增独立反证/问题/人工约束字段。原报告与完整交互仍是来源；失败查询只有原调用事实，不自动解释为查询结论。下一片为第 81 步摘要进入版本化上下文，按新策略验证材料清单、完整配对与输入预算。

## 面试表达

> 我先把专业 Agent 的完成结果沉淀为可追溯的工作材料。摘要与成功结果同事务提交，固定策略版本，并绑定最终模型报告、快照和历次调用；恢复时重新核对来源，不能只凭 checkpoint 声称完成。摘要保存原报告而不虚构反证分类，已读证据与目录分开。测试覆盖摘要落库后进程中断、并发重放和来源损坏。后续再通过独立上下文策略让模型消费摘要，避免把材料存在误称为已经降低 Token。
