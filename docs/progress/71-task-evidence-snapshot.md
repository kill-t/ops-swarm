# 71 - 模型调用前的专业取证快照与恢复复用

## 目标

解决专业模型失败后重复取证的问题。原 v2 只在专业报告成功时持久化证据，模型失败会连同未保存材料一起重试。现在先提交独立取证快照，再分析；即使报告尚未形成，也能查询当次实际取得的材料。

## 设计与边界

先更新 [专项设计](../architecture/71-task-evidence-snapshot.md)、数据库和 API 设计，再接入已有冻结 v2。检查现场适配器后发现，它们按调用当时的时钟查询，变更来源还混合当前运行清单。因此将原下一步拆开：本片交付快照提交与恢复复用，下一片明确现场时间窗和部署状态采集语义，再开放现场 v2。

每个专业任务最多保存一份有界快照，绑定任务分派范围、角色、时间窗、首次采集尝试和成功工具调用。范围 ID 与内容 SHA-256 分别回答“哪组调查材料”和“这次读到了什么”。事件时间、工具请求时间和业务保存时间分别保留。

新 v2 由服务端固定 `evidence_policy_version=task-evidence-v1`；迁移前运行保持 null，不从历史报告反向生成推测快照，不改变旧 checkpoint 的恢复语义。技术重试可复用同任务已提交快照，新任务和新观测窗不能复用。模型重试继续占用原运行预算。

## 实际改动

- `tasks/evidence.py` 定义材料范围与散列契约，拒绝重复证据、错误角色、窗外时间、缺失来源和超大结果；空数组表示查询完成且没有材料。
- `tasks/evidence_tables.py` 定义快照表映射；`tasks/evidence_store.py` 负责有效租约、尝试归属、成功工具绑定、不可变提交和读取校验。快照与 `task.evidence_saved` 事件同事务提交。
- `FixedTaskRuntime` 组织快照读取、必要取证和保存，Worker 按运行固定策略装配。任务结果提交、已成功报告复用与父运行完成时均核对材料，checkpoint 不能替代缺失的业务事实。
- 迁移 `0034_task_evidence_snapshot` 增加运行策略、快照表、复合外键和事件类型；有策略或快照事实时拒绝破坏性降级。
- 新增只读 `GET /api/v1/investigations/{run_id}/collaboration/tasks/{task_id}/evidence`。未采集返回 null，已采集返回记录；列表不内嵌完整材料，跨运行读取返回 404，启动请求不能自行指定取证策略。

## 测试方法与结果

验收日期：2026-09-07。使用假模型与固定取证材料，数据库使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 库，不调用付费模型，不迁移现有业务数据库。

```powershell
uv run python -u -m pytest tests/unit/investigation/test_task_evidence.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_recovery.py tests/api/test_task_evidence_api.py tests/integration/test_evidence_snapshot_migration.py tests/integration/test_model_budget_migration.py tests/integration/test_migrations.py tests/integration/test_budgeted_graph.py tests/integration/test_fixed_task_graph.py tests/api/test_collaboration_api.py tests/integration/test_async_evidence_graph.py tests/integration/test_model_budget_graph.py tests/integration/test_task_result_transactions.py tests/integration/test_checkpoint_recovery.py tests/integration/test_multi_agent_worker.py tests/unit/investigation/test_worker_routing.py tests/unit/investigation/test_worker_main.py tests/unit/investigation/test_parallel_graph.py -v --tb=short

uv run ruff check .
uv run ruff format --check .
uv run mypy
git diff --check
```

最终验收 **75 passed，耗时 144.76 秒**。本片新增 22 个测试用例，覆盖范围与散列校验、同内容并发幂等、冲突拒绝、事务回滚、空结果 API、取证提交前后中断、模型重试、旧策略兼容、缺失快照终态拒绝和迁移往返。原有预算、异步取证、checkpoint、专业结果与 Worker 回归均通过。

故障注入中，日志模型失败后总模型预留从 3 次增至 5 次，新策略工具预留保持 3 次，旧策略为 4 次。取证提交前中断时重新取证并继续计数；提交后中断则保留原快照、采集尝试和工具引用。超时、取消或租约失效后的迟到材料不会落成快照。

首次补充回归因 Docker 未启动产生 16 项数据库初始化错误，启动 Docker 后完成上述验收，初始化失败不计为业务测试结果。Ruff 检查通过，格式检查覆盖 490 个文件，mypy 检查覆盖 200 个源文件；本片 9 篇 Markdown 的结构与 220 个本地链接检查通过，`git diff --check` 通过。

## 限制与下一步

- 工具成功结算与快照保存是两个事务。若在快照提交前中断，重试仍需重新请求并占用工具额度；若已提交，恢复只重试模型。不能宣称外部请求恰好一次。
- 本片保存工具取得的结构化证据，未保存遥测系统的全部原始响应，也未建立 LLM 摘要、任务上下文清单、动态工具循环或长期经验。
- 现场 v2 继续关闭。下一片固定日志、指标与变更的查询截止时间，明确历史事件和当前部署清单的时间含义，再接入现场持久任务；现场失败不得回退冻结材料。

## 面试表达

> 我把一次专业调查拆成取证提交和模型分析两个可恢复边界。模型失败后，系统从 PostgreSQL 读取同任务已提交的证据，避免重新查询导致调查依据变化；模型费用继续累计。快照绑定任务范围、成功工具调用和内容散列，业务结果与 checkpoint 分别保存。通过故障注入验证提交前要重新取证、提交后只重试分析，再验证租约失效、跨事故误用和快照缺失都不能推进结果。目前这套机制已在冻结 v2 验证，现场接入还需要显式观测窗口。
