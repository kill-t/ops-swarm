# 73 - 现场 v2 持久任务取证

## 目标

让完整工程版的持久专业任务实际使用本地现场来源，并在排队、模型失败和 Worker 重启后保持调查范围及已提交材料一致。固定窗口只确定查询范围，业务快照确定重放内容。

## 设计与边界

先更新 [专项设计](../architecture/73-live-task-evidence.md)、数据库及 API 事实来源。新增运行字段 `observation_cutoff_at` 和取证策略 `task-evidence-live-v1`，创建事务固定 UTC 整秒截止时间；日志、指标回看 15 分钟，历史变更回看 6 小时。旧运行不回填，冻结快照保留原字段和散列。

v2 图仍为固定三个专业节点、综合与确定性质疑。现场来源仅支持 Loki、Prometheus、本地变更 API，使用原有受限线程执行器；GitHub 组合配置在调用前明确拒绝并终止自动重试。历史变更不读取当前运行清单，不把过去发布事件写成当前部署证明。本片不增加 MCP 或动态调查。

## 实际改动

- `live_window.py` 绑定单个运行的服务及窗口，将原图的有限查询参数转换为显式范围；适配器继续负责 HTTP、服务白名单及响应解析。
- 迁移 `0035_live_task_evidence` 增加只读截止时间及路由/策略约束。现场 v2 必须有窗口、策略与调用预算；有现场事实时拒绝降级，无现场事实允许恢复上一版本。
- Worker 从持久运行传递窗口；`TaskGraphFactory` 按模式装配固定来源与任务运行器。冻结路径和 v1 图恢复方式保留。
- 现场材料校验服务、角色、范围、来源与策略；取证查询、快照复用、专业结果提交和最终业务结算都核对运行与材料关系，重新计算散列不能绕过运行范围。
- 继续先结算工具调用，再独立提交快照与事件，再调用专业模型。提交前中断会重查原窗口且增加调用计数；提交后中断复用快照，模型重试继续计费。
- 启动 API 开放 `multi_agent + live + v2`，默认 v1；服务端字段不可由请求覆盖。现有协作 API 读取现场快照和报告；现场成功不生成冻结真值评测指标。

## 测试方法与结果

验收日期：2026-09-07。本片验收完成。使用假模型、固定 HTTP transport、真实 PostgreSQL 和独立 checkpoint 数据库，不调用真实模型、GitHub 或现场遥测服务，不迁移已有业务环境。


```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation tests/integration/test_live_task_graph.py tests/integration/test_live_task_migration.py tests/integration/test_fixed_task_graph.py tests/integration/test_budgeted_graph.py tests/integration/test_model_budget_graph.py tests/integration/test_async_evidence_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_evidence_snapshot_migration.py tests/integration/test_model_budget_migration.py tests/integration/test_migrations.py tests/api/test_live_task_api.py tests/api/test_collaboration_api.py tests/api/test_task_evidence_api.py
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

统一验收 **359 passed，耗时 152.51 秒**。覆盖固定窗口在排队和重建 Worker 后不漂移、三专业实际线程并发、模型失败与快照提交前后中断、共享预算不清零、数据源不可用、GitHub 配置拒绝、快照范围伪造、运行级数据库约束、迁移回退护栏及旧冻结材料升级兼容。原有模型预算、异步取证、冻结图、v1 路由及协作 API 回归通过。

Ruff 检查、510 个文件的格式检查、mypy（205 个源文件）通过；本片 Markdown 结构、本地链接与 `git diff --check` 检查通过。测试采用同进程内重建 Worker、业务仓储和 checkpoint 连接验证恢复；独立进程的冻结身份问题另列于下节，未把自动化结果表述为完整现场演示。

## 限制与下一步

- 单次查询仍保持现有服务白名单、材料条数和体积上限；不声称保存全量原始遥测。
- 固定窗口不能阻止远端迟到写入；只有已提交快照保持确切内容。变更 `recorded_at` 是摄取时间，不是事务提交时间。
- GitHub 历史组合与当前运行清单需要独立契约，当前 v2 明确拒绝前者，不读取后者；v1 保留原行为。使用现场 v2 时 Worker 配置 `OPSSWARM_INVESTIGATION_CHANGE_PROVIDER=simulator`。
- 本轮兼容性检查发现既有冻结 `snapshot_id` 直接散列含 `frozenset` 的场景序列化内容，不同进程的集合顺序可能改变身份。本轮以 `PYTHONHASHSEED=1/2/3` 启动独立解释器读取同一场景，分别得到 `frozen:5b5d8eba…`、`frozen:39736f26…`、`frozen:f6fc0076…`，确认该问题可复现。已有冻结恢复测试使用同进程中的新实例，不能据此声称跨进程身份稳定；本片保留旧算法，现场 ID 不依赖该算法。下一片优先设计冻结身份的确定性生成及历史任务兼容，增加不同哈希种子进程间的恢复测试，再推进 ContextBuilder。前端版本选择与完整现场演示仍在后续阶段。

## 面试表达

> 我把现场调查接入了可恢复的专业任务。运行创建时由服务端固定日志、指标和变更的观测截止时间，三专业通过有界线程并行取证，共享预算。取证成功先保存不可变业务材料，再交给模型，因此模型失败后重启 Worker 不会悄悄换一批证据。测试覆盖材料提交前后崩溃，并验证前者原窗口重查、后者复用快照、预算不重置；历史发布记录也不会被当成当前部署证明。这个阶段仍是固定图，后续再建设上下文与动态协作。
