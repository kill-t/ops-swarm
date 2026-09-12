# OpsSwarm 项目状态与开发交接

核对日期：2026-09-12。最新完成切片为第 83 步动态协调计划持久化，基线为 main 的 `d524db9`（第 82 步）。本文随第 83 步独立提交保存；实际提交与工作区须以 Git 核对，统一验收以第 83 步进度记录为准。

## 1. 新会话阅读顺序

1. 阅读 [AGENTS.md](../AGENTS.md)、本文和 [进度索引](progress/README.md)。
2. 先读 [第 83 步设计](architecture/83-dynamic-coordination-persistence.md) 与 [验收记录](progress/83-dynamic-coordination-persistence.md)，再读 [第 82 步设计](architecture/82-dynamic-coordination-policy.md) 与 [验收记录](progress/82-dynamic-coordination-policy.md)，需要上下文前置时再读第 79～81 步。
3. 阅读 [完整路线](architecture/64-full-multi-agent-roadmap.md)、[Agent 专项设计](architecture/65-agent-system-design.md) 中本片相关章节；数据库和 API 事实以 [数据库设计](architecture/04-database-design.md)、[API 设计](architecture/05-api-design.md) 和实现为准。
4. 核对 Git、相关代码、迁移与测试，再确定下一片状态、接口、持久化和失败路径。

架构规定目标与契约，代码和迁移展示实现，进度记录提供验收证据。本文是当前事实摘要，不替代专项来源。

## 2. 项目目标与边界

在 MVP 上建设完整多 Agent 事故响应平台，展示任务分解、专业调查、共享事实、分层记忆、上下文管理、动态补查、独立审查、人工介入和持久恢复。多 Agent 是既定方向，按小切片推进；真实环境操作仅限隔离的本地 Docker Compose 演练系统，不读取原 RAG 仓库。

## 3. 当前架构与技术栈

- Python 3.12、FastAPI、Pydantic 2；PostgreSQL 17、SQLAlchemy 异步、Psycopg 3 与 Alembic；独立调查/验证 Worker，业务数据库和 checkpoint 分离。
- LangGraph 管理固定 v1/v2 图、专业并行与 checkpoint；LangChain 管理模型适配及新策略专业工具循环；内部 Harness 已有任务、共享预算、上下文与逐次交互组件，整体仍未完成。
- 当前仍固定日志、指标、变更三个专业任务和综合节点。新策略只读取已保存证据，不动态查询遥测或创建补查任务。
- 来源包括冻结快照及 Loki、Prometheus、本地变更服务。现场 v2 使用固定 UTC 窗口和受限异步执行器；尚无 telemetry-mcp 服务。
- Vue 3/TypeScript 作战室、REST/可重放 SSE、OpenTelemetry/Jaeger 与 Compose 保留。没有新增页面或前端策略选择器。
- 精确依赖见 [uv.lock](../uv.lock)、[web/package.json](../web/package.json)。业务迁移唯一 head 为 `0041_dynamic_coordination_plans`；第 83 步只在 Testcontainers PostgreSQL 测试数据库执行迁移，未升级用户的 Compose 业务数据库。

## 4. 已完成内容与本会话成果

MVP 历史交付见 [第 63 步](progress/63-mvp-compose-release.md)：调查、证据、审批、白名单执行、恢复验证、人工关闭、不可变复盘报告、改进行动项与作战室已形成演练闭环。原单 Agent 路径、固定多 Agent 路径、评测数据和历史结果保留。第 81 步没有重新执行完整 MVP 发布演示。

此前工程版记录可由 Git 历史逐项核对：

| 切片 | 提交 | 已交付边界 |
| --- | --- | --- |
| [64](progress/64-full-multi-agent-roadmap.md)、[65](progress/65-agent-system-design.md) | `7d4a265`、`01469ee` | 总路线、长期约束与 Agent 专项设计落盘；属于设计验收 |
| [66](progress/66-investigation-task-persistence.md) | `01c82b0` | 轮次、专业任务、尝试事实与必要查询 |
| [67](progress/67-fixed-graph-task-integration.md) | `f61386f` | 冻结 v2 固定图接入任务、结果、事件和恢复核对 |
| [68](progress/68-shared-call-budget.md)、[69](progress/69-model-input-budget.md) | `6b5c845`、`14c4b42` | 次数/时限预算；显式模型输入、Token 和估算费用预留，未知用量不补零 |
| [70](progress/70-bounded-async-evidence.md)、[71](progress/71-task-evidence-snapshot.md) | `da72125`、`089ead7` | 有界异步取证、迟到隔离；模型前持久取证快照与重试复用 |
| [72](progress/72-explicit-observation-windows.md)、[73](progress/73-live-task-evidence.md) | `daeda09`、`487ef9f` | 显式观测窗与现场 v2 接入；假传输现场回归不等于新的真实现场验收 |
| [74](progress/74-stable-frozen-identity.md) | `34bcbf9` | 冻结身份跨进程确定性、已发布内置版本的有限历史兼容 |
| [75](progress/75-task-context-builder.md) | `6fc4f67` | 最小 ContextBuilder、任务隔离、不可变材料清单与输入预留原子提交 |
| [76](progress/76-context-evidence-reader.md) | `05844ca` | 已保存调用范围内的证据目录与受控详情读取服务/API |
| [77](progress/77-repeated-call-identity.md) | `090a44e` | 同尝试重复调用槽位、兼容旧幂等键、共享预算和迁移保护 |
| [79](progress/79-bounded-evidence-reading.md) | `9475621` | 专业目录与受限证据读取循环、完整配对交互、逐次预算及恢复验证 |

第 81 步提交为 `e744dd2`，第 82 步提交为 `d524db9`。第 83 步策略与计划持久化详见 [当前验收记录](progress/83-dynamic-coordination-persistence.md)，对应本片独立提交。

## 5. 当前能力与限制

第 79 步通过显式 `context_policy_version=evidence-read-v1` 开启专业按需读取；必须同时提供 v2 与 function_calling 模型预算 profile。省略字段仍按旧规则选择 task-context-v1 或 null，v1/历史 v2 不升级。

- 专业模型先接收本任务目录，最多每轮一个 read_evidence 或 SpecialistReport 请求。工具参数只有证据 ID 与快照散列，运行、任务、尝试和角色由系统注入；报告只能引用本尝试成功读取的证据。
- 每次模型/业务工具调用前预留，返回与结算同事务保存。下一输入从成功且完整配对的账本重新组装并在预算事务内复核。内部循环没有独立 checkpoint；未完成任务技术重试从原快照目录开始，新尝试不清空预算。
- 新增显式 `summary-context-v1`，同样要求 v2 与 function_calling 模型预算，并固定摘要策略。综合接收三份完整工作摘要替代独立报告段，保留完整有界证据；组装和预算预留事务均重算来源。旧策略综合仍接收三个已提交专业报告和完整有界证据。冻结和现场模式均使用假模型/假传输回归，没有新的真实模型或现场发布验收。
- 默认预算仍为 12 次模型、18 次工具、每任务 6 次工具、300 秒。原始取证与按需读取共用额度。默认输出预留 4096、累计 24000 无法支持至少七次模型调用；开启者必须显式配置合适预算，测试使用单次输出 1024，不静默提高上限。
- call-budget 是累计实际调用预留和用量的查询入口。旧 result.tool_call_count / 评测字段仍表示固定图三次取证，不能称作新循环总工具数。
- 输入采用保守字节估算；未知用量保持未知。合法响应也受 32 KiB 规范化 JSON 上限约束，未通过校验的原始正文不保存为历史；失败仍保留状态与可取得的供应商用量。没有证明 Token 或费用下降。
- 工具失败可能被框架转换成本地错误消息，但不完整历史在下一请求前即被拒绝；既有 Worker 技术重试仍有界且消耗原预算。

第 80 步新增摘要材料；第 81 步扩展为新建 evidence-read-v1 或 summary-context-v1 运行固定 `working_summary_policy_version=task-working-summary-v1`，任务成功时生成有界不可变摘要，与结果/状态/事件同事务提交。查询、成功结果恢复与父运行完成均复核摘要来源；失败调用按引用保留，不推断问题分类。摘要可通过只读 API 查询，并在 summary-context-v1 中进入综合输入；旧策略不消费摘要。

第 82 步新增 `dynamic-coordination-v1` 策略、结构化提案及编译结果。模型可选范围仅为本轮目标、日志/指标/变更角色、任务目标和回看时长；编译器注入事故服务、UTC 截止时间、证据快照身份和尝试上限，拒绝轮次、角色、窗口或总任务额度越界。该能力目前是纯领域闸门，尚未接入模型、业务数据库或工作流。

第 83 步把首次固定的完整协调策略、编译计划、父 Worker 尝试、实际轮次和任务原子持久化。查询从事故、策略、父尝试、轮次和任务重建核对；冲突重放、固定/动态历史混用、来源篡改与租约失效均拒绝。当前尚无协调模型、HTTP 查询、v3 启动或动态图。

尚未完成：专业交互历史裁剪；动态分派/跨专业补查/独立审查；只读 MCP；Git 调查手册；可信身份与经验提取、审核、停用；图内人工暂停/补充/取消/恢复；规划 Agent、多步骤处置与验证失败回流；复杂场景工作台和完整工程版发布。历史业务审批不等于 LangGraph interrupt/resume。

## 6. 下一步代码导航

| 入口 | 边界 |
| --- | --- |
| [worker/task_graph.py](../src/opsswarm/worker/task_graph.py) | 按固定运行策略装配新旧 Analyst，profile 必须匹配，SDK 重试为 0、n 为 1 |
| [loop/agent.py](../src/opsswarm/investigation/loop/agent.py)、[loop/middleware.py](../src/opsswarm/investigation/loop/middleware.py) | create_agent/ToolStrategy 原生循环及每次请求的预算、返回审计；每个专业尝试独立 middleware |
| [loop/contracts.py](../src/opsswarm/investigation/loop/contracts.py)、[loop/messages.py](../src/opsswarm/investigation/loop/messages.py) | 新输入版本、消息适配与配对，旧 ModelInput 未改变序列化 |
| [loop/context.py](../src/opsswarm/investigation/loop/context.py)、[loop/ledger.py](../src/opsswarm/investigation/loop/ledger.py) | 首轮目录、成功历史、报告读取资格、参数/来源/结果复核；历史重建抽至 loop/history.py 供摘要共用 |
| [context/catalog.py](../src/opsswarm/investigation/context/catalog.py)、[context/evidence_reader.py](../src/opsswarm/investigation/context/evidence_reader.py) | 新旧入口共用目录投影；详情仍绑定真实保存的模型 call_id |
| [context/selection.py](../src/opsswarm/investigation/context/selection.py)、[context/reservation.py](../src/opsswarm/investigation/context/reservation.py) | 启动策略选择及预留事务内按版本重建输入，按策略选择旧报告或新摘要综合输入 |
| [budget/store.py](../src/opsswarm/investigation/budget/store.py)、[budget/runtime.py](../src/opsswarm/investigation/budget/runtime.py) | 有效父租约、原子预留、共享额度、结算幂等与迟到阻断；相同预留不能重发外部请求 |
| [working_summary/builder.py](../src/opsswarm/investigation/working_summary/builder.py)、[working_summary/persistence.py](../src/opsswarm/investigation/working_summary/persistence.py) | 摘要确定性投影、同事务保存与查询来源核对；不调用模型 |
| [tasks/runtime.py](../src/opsswarm/investigation/tasks/runtime.py) | 稳定任务、新尝试、快照复用及专业结果恢复，不把技术重试当成补查 |
| [coordination/contracts.py](../src/opsswarm/investigation/coordination/contracts.py)、[coordination/policy.py](../src/opsswarm/investigation/coordination/policy.py) | 有限协调提案、运行策略及系统范围注入；当前未接入协调模型 |
| [coordination/persistence.py](../src/opsswarm/investigation/coordination/persistence.py)、[coordination/queries.py](../src/opsswarm/investigation/coordination/queries.py) | 策略、计划、业务轮次和父尝试的原子写入及来源重建；当前为内部应用入口 |

第 79 步核对了锁定 LangChain 的 middleware、ToolStrategy 与输出确认契约。工具 schema 必须与框架实际绑定一致；升级依赖时通过真实 SDK 假传输测试核对，不能凭记忆改参数。

## 7. 保留的重要决定

LangGraph/LangChain 复用框架循环、调度与 checkpoint，Harness 专注权限、上下文、预算和审计；checkpoint 不替代业务事实。内部 Agent 不使用 A2A，MCP 只用于后续只读遥测。经验从复盘生成候选，经可信人工确认后启用，不是当前事实或执行授权。新能力固定新策略，旧运行不静默换输入和恢复语义。

开发子智能体按独立边界与难度分配；主 Agent 核实关键结论、统一测试和提交。开发 Agent 的资源偏好不改变 OpsSwarm 业务角色的模型或预算。

## 8. 下一功能切片

第 84 步：**协调模型结构化调用与共享预算账本**。新增根级协调操作和版本化输入/输出，在外部请求前预留运行共享预算，把可恢复模型返回与计划来源关联；继续保持 v1/v2 固定图和 checkpoint 不变。

第 81 步只让综合消费完成任务摘要，没有裁剪专业交互、动态分派或新增独立反证/未解问题字段。后续若需要历史压缩，应单独定义完整配对替换和必要材料保留规则，不以摘要已被消费推断 Token 已下降。

之后仍按第 64/65 篇推进动态协作 → 只读 MCP → 手册、可信身份与经验 → 人工介入/规划 → 多步骤处置与回流 → 复杂场景和工作台 → CI 与完整发布。

## 9. 验证与环境

使用仓库 `.venv/Scripts/`；完整第 81 步命令和结果见其进度记录。数据库测试由 [tests/conftest.py](../tests/conftest.py) 启动独立 PostgreSQL 17.6 与 checkpoint 数据库，需要 Docker。Windows 使用 Selector 事件循环，异步子进程继续参考现有线程桥接。

```powershell
git status --short
git log -5 --oneline
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

第 83 步测试入口为 [计划事务](../tests/integration/test_coordination_plan_store.py)、[计划迁移](../tests/integration/test_coordination_plan_migration.py) 和 [遗留轮次散列](../tests/unit/investigation/test_task_round_identity.py)。第 82 步测试入口为 [协调策略单元测试](../tests/unit/investigation/test_coordination_policy.py)。第 81 步测试入口为 [综合与恢复](../tests/integration/test_summary_context_graph.py)、[预留复核](../tests/integration/test_summary_context_reservations.py)、[策略迁移](../tests/integration/test_summary_context_migration.py)。旧模型预算测试刻意清空上下文策略，不要删除这些历史契约覆盖。正式配置见 README/Compose/变量示例，不读取或提交真实 .env、个人配置和令牌。

## 10. 可直接用于继续开发的任务

> 请阅读 AGENTS.md、项目状态、进度索引和第 83 步设计/验收，核对 Git 与实际代码。动态协调策略和计划已与轮次、任务、父尝试原子持久化；从第 84 步协调模型结构化调用与共享预算推进，扩展根级调用账本、保存可恢复返回并绑定计划来源，完成假模型、真实 PostgreSQL、文档和中文独立提交。不要把当前固定图描述成已有动态协调，也不提前交付 MCP 或图内人工恢复。
