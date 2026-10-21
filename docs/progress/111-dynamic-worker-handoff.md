# 111 - v3 Worker 与终端路由持久交接

## 目标

第 110 步已解决三轮来源重建的运行上界，但通用 Worker 仍跳过 v3，手工运行图后父运行保持 running。本片将当前 graph-v15 接入实际 Worker，把两类终端路由交给持久业务阶段，让调查任务释放租约并可查询、重试和审计。

基线为 main `9aefa7a`。开始时 Git 仅有未跟踪用户目录 `output/`；未读取或提交该目录。主 Agent 实现、整合与验收；一个边界明确的子 Agent 先只读审查，再独占实现迁移及其测试，并独立复核主实现。所有提交由主 Agent 完成。

## 设计与边界

架构来源见 [终端交接设计](../architecture/111-dynamic-worker-handoff.md)，并同步第 4/5/64/65 篇。

| 图路由 | 父运行 | 工作项 | 当前父尝试 |
| --- | --- | --- | --- |
| await_human | await_human | completed、租约清空 | handed_off |
| candidate_evaluation | await_candidate | completed、租约清空 | handed_off |

两种父运行均保持 result/metrics/completed_at 为空，Incident 仍 investigating，占用活动事故唯一槽位。独立终端 outcome 只包含路由 ID/散列和模型名；旧固定根因成功契约保留，旧 complete 与数据库明确拒绝 v3 succeeded。候选评估交接不是根因证明、旧根因映射、规划或执行授权。

每运行唯一且不可变的交接记录分别绑定原路由和本次接管尝试，允许新租约复用旧尝试路由。锁定父工作项后重建路由全部业务来源，拒绝跨运行、错误散列、follow_up、较早轮次、未完成任务和 reserved 外部请求。在来源核验后及最后 flush 后检查实际时钟，写入阶段超租约也整体回滚。交接、尝试用量、运行/任务状态和可重放 run.handed_off 事件同事务提交。

同尝试、同来源、同模型和用量的命令重复返回原记录；不同命令拒绝。查询重建来源，不依据 checkpoint 推断业务成功。共享预算不重置，未知用量不记为已知零费用。

## 实际改动

- 新增 handoff 契约、映射和事务仓储；提取父尝试用量结算供旧成功和新交接共用，旧计算规则不变。
- 新增 DynamicInvestigationProcessor 与工作流分派；沿用原图 has_checkpoint/investigate/resume，未增加图版本或另一套调度。
- claim_next 增加动态图版本能力过滤；默认 Worker 保持 v1/v2，显式开启后只额外领取冻结 graph-v15，历史 v3 图不被新图恢复。
- 实际进程装配 CurrentDynamicGraphFactory。端到端发现固定日期基线无法服务新事故，故新增 release-500-worker-v1 演练来源：最新模拟观察锚定事故开始，保留证据相对时间和正文，完整散列包含实际事故请求，无旧身份别名。限 order-service 发布故障；不同来源快照拒绝混用，旧基线不改。
- 新增 GET /api/v1/investigations/{run_id}/terminal-handoff，未交接 null、未知运行 404、来源冲突 409；现有状态/尝试/事件接口反映新状态。前端仅补充状态类型和“已交接”标签，没有新增页面。
- 新增 0067_terminal_handoff：复合来源外键、运行唯一、不可变触发器、有效来源与租约准入、双向延迟跨表一致性及活动事故索引。降级遇到交接、等待、handed_off 或交接事件会拒绝，不丢弃业务事实。旧迁移不修改，既有测试仅更新正式 head 断言。
- .env.example、Compose 和 README 提供显式开关与时间边界；Compose 只向冻结池传入动态开关，停止宽限可配置。

## 测试方法与结果

使用仓库 .venv、正式迁移创建的真实 PostgreSQL 17.6、独立 checkpoint 数据库和 SDK 假传输；不是生产或真实模型验收。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit tests/api --tb=short
$migrationTests = @(rg --files tests/integration -g '*migration*.py')
.venv/Scripts/python.exe -m pytest @migrationTests --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_worker_handoff.py -x --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_worker_handoff.py::test_dynamic_worker_retries_model_failure_without_resetting_run_budget --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_worker_handoff.py::test_handoff_rejects_bad_sources_and_late_leases_then_replays_once --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_investigation_store.py tests/integration/test_fixed_task_graph.py tests/integration/test_live_task_graph.py tests/integration/test_budgeted_graph.py --tb=short
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/python.exe -m opsswarm.worker.main --help
docker compose --env-file .env.example config --quiet
npm --prefix web run typecheck
npm --prefix web run test -- InvestigationAttemptHistory
git diff --check
```

实际结果：

| 验收组 | 结果 |
| --- | --- |
| 全量单元/API | 1,493 通过，635.98 秒；含新增动态处理器、冻结来源和配置的 16 项单元测试 |
| 全部 migration 文件 | 238 通过，329.47 秒；含本片 10 项迁移测试、空库往返、metadata 对齐和历史版本守卫 |
| 新端到端主组 | 当时收集的 8 项通过，238.56 秒 |
| 后补真实 SDK 假传输失败重试 | 1 项通过，67.07 秒；保留首次失败调用、原预算和 deadline |
| 不同快照恢复补充断言 | 所属来源/幂等用例再次通过，37.39 秒；在模型请求前拒绝同图不同快照，此项不重复计数 |
| 旧 Worker/仓储四文件 | 46 通过，141.13 秒；覆盖 v1/v2、现场任务、预算和父尝试用量结算 |
| Ruff / 格式 / mypy | 通过；656 个 Python 文件格式通过，360 个源文件类型检查通过 |
| 前端 | typecheck 通过，尝试历史组件 3 项测试通过 |
| CLI / Compose / Git | Worker help、唯一 head 0067_terminal_handoff、显式 .env.example 的 Compose config、diff --check 均通过 |

去重验收集为 **1,786 项 Python 测试 + 3 项前端测试**。初步迁移/路由 22 项和新增单元/配置 20 项也通过，已包含在最终验收组，不累加。未调用真实外部模型或访问生产环境。

主要覆盖两种终端、三轮停止、查询与规划拒绝、并发幂等、前/后交接崩溃、新父尝试恢复、不重发 SDK、不重置预算、过期租约及写入阶段迟到回滚、原生状态防伪与保守降级。独立审查提出最后 flush 后超租约窗口，已修复并增加整事务回滚断言。

## 限制与下一步

本片只在 Testcontainers 执行迁移，未升级用户的 Compose 数据库。默认不启用动态 Worker；开启需 frozen、处理至少 180 秒、租约至少多 30 秒，建议处理 240/租约 300/停止宽限 5m。运行建议显式 max_model_calls=16、max_duration_seconds=900，另配足够模型预算；已固定的默认 300 秒总时限不会自动延长，恢复也不延长 deadline。

未加入心跳、现场 v3、人工命令、候选评估器或图内 interrupt/resume。交接等待态暂时不可自动恢复，后续能力必须设计命令版本与新业务任务，不可把已到 END 的 checkpoint 当作人工暂停。历史 graph-v15 的其他冻结快照不能静默切换为新来源。未重跑所有动态历史图的慢速降级场景，本轮全迁移组含版本守卫，不能替代全部历史图回归。

下一切片 112：v3 人工等待的可信命令与恢复语义。先明确人工补充/取消/恢复的身份、精确等待版本、幂等审计、后续任务与预算延续，以及旧 checkpoint 和图版本的兼容边界，再实现最小闭环。

## 面试表达

“我把图终端与业务成功分开。LangGraph 完成调查后只返回路由引用，控制面用有效租约重新验证持久来源，再把交接、父尝试用量、等待状态和事件原子落库。Worker 崩溃后复用成功事实与 checkpoint；交接后任务释放租约，既不重复调用，也不把候选或等待人工误当作根因证明和执行授权。”
