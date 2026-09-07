# 75 - 任务上下文组装与材料清单

## 目标

把已有任务分派、取证快照和专业报告变成可解释的模型输入：不仅能查到模型收到了什么，也能追溯输入来自哪一个任务、哪一版材料。

## 设计与边界

先更新 [任务上下文专项设计](../architecture/75-task-context-builder.md)、数据库及 API 设计，再实现。ContextBuilder 只做确定性组装；加载组件读取业务材料，预算事务组件复核归属，模型适配器仅执行 LangChain 结构化调用和用量处理。运行固定上下文与提示策略，历史运行不升级。

本片只处理固定图的当前完整有界证据：专业节点只见本任务；综合节点需要三个已提交报告，不共享完整聊天。当前没有业务工具循环、上下文裁剪、工作摘要、经验检索或人工补充；清单的业务工具集合及裁剪记录均为空，不虚构目标能力。

## 实际改动

- 新建 context 包，分离输入/清单契约、版本化提示、纯组装、材料加载、用例组织和事务校验。
- 新建且配置 model_budget 的 v2 固定 task-context-v1 与 fixed-task-context-v1，兼容冻结和现场来源；默认无模型预算路径仍保留原行为。
- 专业输入增加任务目标、服务、时间窗、证据快照范围和完成要求。综合输入采用持久结果；重复、越界或与图输入冲突的材料明确失败。
- 清单记录运行、任务、角色、提示、取证和材料版本、ID/散列/证据引用以及实际 ModelInput 的散列和预算估算；保留原 ModelInput 格式与哈希算法。
- 模型请求前，在原租约/预算事务中重新读取并校验材料，连同输入和预留保存清单。崩溃后旧预留变为 unknown，重试新增调用记录且预算累计，不能覆盖旧清单。
- 迁移 0036_task_context_manifest 新增两列和 PostgreSQL 约束，不回填历史策略；已存在策略或清单时拒绝降级。
- 增加按运行和调用读取 context-manifest 的只读 API；历史与工具调用为 null，跨运行查询 404，不暴露租约或添加修改接口。

## 测试方法与结果

验收日期 2026-09-07。最终统一回归 **458 passed，耗时 307.08 秒**，其中本片新增 29 项验收用例。使用真实 PostgreSQL、独立 checkpoint 数据库和假 SDK/HTTP 传输，不调用外部模型，也不把假遥测描述为现场实测。

```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation tests/unit/evaluation/test_dataset.py tests/unit/evaluation/test_runner.py tests/integration/test_context_reservations.py tests/integration/test_context_graph.py tests/integration/test_context_migration.py tests/integration/test_investigation_task_store.py tests/integration/test_task_result_transactions.py tests/integration/test_checkpoint_recovery.py tests/integration/test_fixed_task_graph.py tests/integration/test_budgeted_graph.py tests/integration/test_budgeted_call_runner.py tests/integration/test_call_budget_store.py tests/integration/test_model_budget_graph.py tests/integration/test_model_budget_store.py tests/integration/test_model_budget_migration.py tests/integration/test_async_evidence_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_evidence_snapshot_migration.py tests/integration/test_live_task_graph.py tests/integration/test_live_task_migration.py tests/integration/test_frozen_process_recovery.py tests/integration/test_frozen_identity_scope.py tests/integration/test_migrations.py tests/api/test_context_manifest_api.py tests/api/test_model_budget_api.py tests/api/test_call_budget_api.py tests/api/test_collaboration_api.py tests/api/test_task_evidence_api.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

Ruff 检查、534 个文件格式检查和 mypy（214 个源文件）通过；9 篇 Markdown 的结构及本地链接检查通过，`git diff --check` 通过。

新增验收覆盖三种结构化输出方式、输入排序稳定性、任务/事故隔离、未提交报告、图输入冲突、恶意文本的数据边界、预算前置、清单篡改、组装后材料变化、预留后崩溃与重试、只读 API 和历史迁移。两个事故并发在冻结和现场两条路径分别验证；现场路径使用真实适配器加假 HTTP 响应，断言来源窗口和模型请求前的清单提交。

回归时发现旧无预算测试夹具仍保留后来新增的取证策略，违反现有 PostgreSQL 约束；同步把历史策略置空后继续测试，没有放宽业务约束。首次并发现场测试遗漏关闭冻结真值评测，数据库正确拒绝把冻结指标保存到现场运行；修正测试装配后通过现场并发专项。

## 限制与下一步

- 尚未运行新的真实模型或本地遥测现场演示；本片数据库迁移只在隔离测试环境执行。
- 清单是可追溯材料目录，不证明模型判断正确；输入预算沿用保守估算，不是供应商精确计数或扣费上限。
- 下一片增加证据目录与受控按需读取；随后接入结构化工作摘要，再推进动态调查。完整分层记忆、MCP 和人工介入仍按总路线交付。

## 面试表达

> 我为多 Agent 调查增加任务上下文边界，专业节点只拿自己分派范围内的证据，综合节点只用已提交的报告。每次调用把材料 ID、版本和内容散列与实际模型输入、预算预留一起保存，并在提交前核对业务事实。Worker 重试后仍能解释每一次调用看到了哪份材料；跨事故输入、未提交报告和超预算输入会在模型请求前被拒绝。
