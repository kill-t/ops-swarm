# OpsSwarm 项目状态与开发交接

核对日期：2026-10-03。最新完成切片为第 96 步 v3 第二轮补查路由的第三轮计划持久化，基线为 main 的 `5da470d`（第 95 步）。本文随第 96 步独立提交保存；实际提交与工作区须以 Git 核对，统一验收以第 96 步进度记录为准。

## 1. 新会话阅读顺序

1. 阅读 [AGENTS.md](../AGENTS.md)、本文和 [进度索引](progress/README.md)。
2. 先读 [第 96 步设计](architecture/96-third-round-planning.md) 与 [验收记录](progress/96-third-round-planning.md)，再读第 85～95 步设计与记录，需要上下文前置时再读第 79～83 步。
3. 阅读 [完整路线](architecture/64-full-multi-agent-roadmap.md)、[Agent 专项设计](architecture/65-agent-system-design.md) 中本片相关章节；数据库和 API 事实以 [数据库设计](architecture/04-database-design.md)、[API 设计](architecture/05-api-design.md) 和实现为准。
4. 核对 Git、相关代码、迁移与测试，再确定下一片状态、接口、持久化和失败路径。

架构规定目标与契约，代码和迁移展示实现，进度记录提供验收证据。本文是当前事实摘要，不替代专项来源。

## 2. 项目目标与边界

在 MVP 上建设完整多 Agent 事故响应平台，展示任务分解、专业调查、共享事实、分层记忆、上下文管理、动态补查、独立审查、人工介入和持久恢复。多 Agent 是既定方向，按小切片推进；真实环境操作仅限隔离的本地 Docker Compose 演练系统，不读取原 RAG 仓库。

## 3. 当前架构与技术栈

- Python 3.12、FastAPI、Pydantic 2；PostgreSQL 17、SQLAlchemy 异步、Psycopg 3 与 Alembic；独立调查/验证 Worker，业务数据库和 checkpoint 分离。
- LangGraph 管理固定 v1/v2 图、专业并行与 checkpoint；v3 图 v11 已按持久计划动态执行首轮与第二轮专业任务，分别形成按轮开放假设、独立审查和确定性路由，并为第二轮补查创建第三轮计划。图 v1～v10 保留各自历史语义。LangChain 管理模型适配及新策略专业工具循环；内部 Harness 已有任务、共享预算、上下文与逐次交互组件，整体仍未完成。
- v1/v2 仍固定日志、指标、变更三个专业任务和综合节点；v3 已动态选择首轮角色与材料，为首轮 `follow_up` 编译和执行第二轮任务，并只用第二轮材料形成新开放假设、独立审查与路由；第二轮 `follow_up` 已编译第三轮计划和待执行任务，但尚未执行。专业新策略只读取已保存证据，不动态查询现场遥测。
- 来源包括冻结快照及 Loki、Prometheus、本地变更服务。现场 v2 使用固定 UTC 窗口和受限异步执行器；尚无 telemetry-mcp 服务。
- Vue 3/TypeScript 作战室、REST/可重放 SSE、OpenTelemetry/Jaeger 与 Compose 保留。没有新增页面或前端策略选择器。
- 精确依赖见 [uv.lock](../uv.lock)、[web/package.json](../web/package.json)。业务迁移唯一 head 为 `0053_third_round_planning`；第 96 步只在 Testcontainers PostgreSQL 测试数据库执行迁移，未升级用户的 Compose 业务数据库。

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

第 81 步提交为 `e744dd2`，第 82 步提交为 `d524db9`，第 83 步提交为 `5d3d702`，第 84 步提交为 `e4922d3`，第 85 步提交为 `f5287a6`，第 86 步提交为 `778e434`，第 87 步提交为 `8eb4712`，第 88 步提交为 `956dad6`，第 89 步提交为 `3e98d7a`，第 90 步提交为 `e902e0b`，第 91 步提交为 `3e2813c`。第 92 步第二轮专业任务执行对应提交 `cdd9e0d`，第 93 步第二轮综合与开放假设恢复对应提交 `48bef25`，第 94 步第二轮独立审查对应提交 `0115537`，第 95 步第二轮确定性再次路由对应提交 `5da470d`。第 96 步第三轮计划持久化详见 [当前验收记录](progress/96-third-round-planning.md)，对应本片独立提交。

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

第 83 步把首次固定的完整协调策略、编译计划、父 Worker 尝试、实际轮次和任务原子持久化。查询从事故、策略、父尝试、轮次和任务重建核对；冲突重放、固定/动态历史混用、来源篡改与租约失效均拒绝。截至该步尚无协调模型、HTTP 查询、v3 启动或动态图。

第 84 步新增根级 `coordinate` 结构化模型调用，使用原运行共享次数、时限、Token 与估算费用预算；上下文只含事故与固定协调策略，没有工具或执行权限。提案通过 Pydantic 与确定性编译后才结算成功，并作为计划来源保存。模型成功后、计划提交前崩溃会复用调用；计划提交后崩溃会直接复核并返回原计划。第 86 步已将该入口兼容接入 v3，原 v2 测试路径和清单版本保持不变。

第 85 步新增隔离的 `workflow_version=v3` 和 `graph_version=dynamic-investigation-graph-v1`。v3 只接受冻结多 Agent 与 function_calling 模型预算，创建时固定完整协调策略、summary-context-v1、工作摘要、task-evidence-v1、调用预算和模型预算；原运行查询返回全部启动契约。现有 Worker 的领取 SQL 只声明 v1/v2，能够越过队首 v3 继续领取兼容任务；旧处理器和固定图在 checkpoint 访问前再次拒绝 v3。截至该步尚未创建或恢复动态图 checkpoint。

第 86 步新增独立 `START → coordinate → END` v3 图和装配工厂。协调节点在当前父租约内调用既有规划服务，checkpoint metadata 与状态都保存并核对工作流/图版本；恢复实例注入新租约，checkpoint 不保存租约令牌。模型成功后计划提交前、计划提交后 checkpoint 前两个窗口都通过业务调用或计划复用收敛。直接集成测试中的 v3 父运行保持 running 且没有最终结果；通用 Worker 仍跳过 v3，因为当前图尚无专业结果、综合和终态校验。

第 87 步保留上述仅协调图 v1，并将新 v3 运行固定到 `dynamic-investigation-graph-v2`。图 v2 从已持久化计划稳定排序并 `Send` 1～3 个专业分支，按每个任务的 UTC 窗口过滤冻结材料，复用已有任务尝试、取证、共享预算、专业循环和工作摘要。并行单节点失败后，新父租约只恢复必要任务；业务结果已提交但 checkpoint 未写时直接复用原结果。上下文清单记录真实 v2/v3 版本。首轮完成仍不结算父运行，通用 Worker 继续跳过 v3。

第 88 步继续保留图 v1/v2，新运行固定到 `dynamic-investigation-graph-v3`。图 v3 在首轮实际选择的 1～3 个任务后，只用其已提交工作摘要和证据执行一次根级综合；输出为自由文本开放假设、支持证据、反证和未解问题，系统注入 `pending_review`。开放假设及成功综合调用来源不可变持久化，模型成功后落库前和落库后 checkpoint 前两个窗口均可跨父尝试恢复。它不含旧 `RootCause`、处置建议或执行资格，父运行仍保持 running，通用 Worker 继续跳过 v3。

第 89 步继续保留图 v1/v2/v3，新运行固定到 `dynamic-investigation-graph-v4`。图 v4 在开放假设后执行无工具的独立审查，只读取原综合调用的同源首轮材料和当前不可变假设，保存支持充分性、接受的支持、反证、证据缺口、竞争性解释与建议方向。成功审查调用和 `recorded` 审查事实均可跨两个提交窗口恢复；建议不创建任务、不代表根因证明或执行资格，父运行仍保持 running，通用 Worker 继续跳过 v3。

第 90 步继续保留图 v1/v2/v3/v4，新运行固定到 `dynamic-investigation-graph-v5`。图 v5 在独立审查后用纯规则检查开放假设反证、审查反证/缺口/竞争解释及冻结轮次和任务容量，保存 `follow_up`、`await_human` 或 `candidate_evaluation` 不可变事实。路由不增加模型/工具调用，不创建第二轮任务，也不改变父运行、假设或审查状态；事实提交与 checkpoint 之间可以跨父租约恢复，通用 Worker 继续跳过 v3。

第 91 步继续保留图 v1～v5，新运行固定到 `dynamic-investigation-graph-v6`。图 v6 只消费 `follow_up` 路由，把全部建议按稳定角色顺序编译为第二轮计划，复用首轮冻结快照和截止时间，并在一个租约事务内创建轮次、待执行任务和事件。计划以 `source_review_route_id` 一一绑定同运行路由；另外两种路由不创建计划。该节点不调用模型或工具、不执行第二轮、不结算父运行，计划提交前后两个 checkpoint 窗口均可恢复，通用 Worker 继续跳过 v3。

第 92 步继续保留图 v1～v6，新运行固定到 `dynamic-investigation-graph-v7`。图 v7 只从已持久第二轮计划稳定分派专业任务，使用独立 `follow_up_results` 隔离两轮同角色结果，并让任务运行器显式核对期望轮次。第二轮复用冻结范围、任务证据、专业循环、工作摘要和原运行共享预算；部分失败接管只重试失败任务，结果落库后 checkpoint 前中断不会重调外部请求。第二轮结算为 `completed`，父运行仍为 `running`；另外两种路由不执行补查，通用 Worker 继续跳过 v3。

第 93 步继续保留图 v1～v7，新运行固定到 `dynamic-investigation-graph-v8`。图 v8 只在 `follow_up` 第二轮任务全部完成后，使用第二轮已提交工作摘要和完整证据执行一次根级综合，并以独立 `follow_up_hypothesis` 保存绑定第二轮计划的 `pending_review` 开放假设。首轮和第二轮 `synthesize` 分别固定调用槽位 0 与 1，第二轮使用独立提示/角色版本；模型成功后事实提交前和事实提交后 checkpoint 前均可跨父租约恢复。另外两种路由不新增综合调用或假设，父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 94 步继续保留图 v1～v8，新运行固定到 `dynamic-investigation-graph-v9`。图 v9 只在第二轮开放假设后执行独立审查，以 `follow_up_hypothesis_review` 保存绑定第二轮假设的 `recorded` 审查事实。首轮和第二轮 `review` 分别固定调用槽位 0 与 1，第二轮使用独立提示/角色版本；上下文清单与第二轮综合材料逐项同源并只追加当前假设。两个提交窗口均可跨父租约恢复，同槽位重复成功调用会在查询和恢复入口被拒绝。另外两种路由不新增第二轮审查，父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 95 步继续保留图 v1～v9，新运行固定到 `dynamic-investigation-graph-v10`。图 v10 从第二轮假设、同轮审查、冻结协调策略及两轮真实任务数重新编译并保存 `follow_up_review_route`。两条路由按业务轮次查询；第二轮 `follow_up` 只表达仍有容量的第三轮意图，不创建计划或任务。事实提交前和 checkpoint 前窗口均可跨父租约恢复，不增加模型、工具或预算调用；父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 96 步继续保留图 v1～v10，新运行固定到 `dynamic-investigation-graph-v11`。图 v11 只在第二轮路由为 `follow_up` 时，把完整建议确定性编译为绑定该路由的第三轮计划，并在有效父租约事务内原子创建第 3 轮和待执行任务；另外两种路由不创建第三轮。来源轮次门禁使图 v10 即使直接调用仓储也不能写入第三轮。两个计划提交窗口均可跨父租约恢复，不增加模型、工具或预算调用；父运行仍为 `running`，通用 Worker 继续跳过 v3。

尚未完成：专业交互历史裁剪；第三轮执行、综合、审查与完整有界多轮；只读 MCP；Git 调查手册；可信身份与经验提取、审核、停用；图内人工暂停/补充/取消/恢复；候选结论与规划 Agent、多步骤处置及验证失败回流；复杂场景工作台和完整工程版发布。历史业务审批不等于 LangGraph interrupt/resume。

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
| [tasks/runtime.py](../src/opsswarm/investigation/tasks/runtime.py) | 稳定任务、新尝试、快照复用及专业结果恢复；调用方必须显式提供期望轮次，避免跨轮执行 |
| [coordination/contracts.py](../src/opsswarm/investigation/coordination/contracts.py)、[coordination/policy.py](../src/opsswarm/investigation/coordination/policy.py) | 有限协调输入/输出、运行策略及系统范围注入；业务无效提案不能结算为成功 |
| [coordination/context.py](../src/opsswarm/investigation/coordination/context.py)、[coordination/planner.py](../src/opsswarm/investigation/coordination/planner.py) | 首轮最小上下文、根级共享预算调用、成功结果复用与计划提交恢复 |
| [coordination/persistence.py](../src/opsswarm/investigation/coordination/persistence.py)、[coordination/queries.py](../src/opsswarm/investigation/coordination/queries.py) | 策略、计划、业务轮次、父尝试和模型调用来源的原子写入及重建核对 |
| [workflow_versions.py](../src/opsswarm/investigation/workflow_versions.py)、[persistence/repository.py](../src/opsswarm/investigation/persistence/repository.py) | v3 路由、启动策略原子冻结、运行查询及 Worker 支持版本领取过滤 |
| [dynamic_graph.py](../src/opsswarm/investigation/dynamic_graph.py)、[dynamic_first_round_graph.py](../src/opsswarm/investigation/dynamic_first_round_graph.py)、[dynamic_synthesis_graph.py](../src/opsswarm/investigation/dynamic_synthesis_graph.py)、[dynamic_review_graph.py](../src/opsswarm/investigation/dynamic_review_graph.py)、[dynamic_routing_graph.py](../src/opsswarm/investigation/dynamic_routing_graph.py)、[dynamic_follow_up_graph.py](../src/opsswarm/investigation/dynamic_follow_up_graph.py)、[dynamic_second_round_graph.py](../src/opsswarm/investigation/dynamic_second_round_graph.py)、[dynamic_second_round_synthesis_graph.py](../src/opsswarm/investigation/dynamic_second_round_synthesis_graph.py)、[dynamic_second_round_review_graph.py](../src/opsswarm/investigation/dynamic_second_round_review_graph.py)、[dynamic_second_round_routing_graph.py](../src/opsswarm/investigation/dynamic_second_round_routing_graph.py)、[dynamic_third_round_planning_graph.py](../src/opsswarm/investigation/dynamic_third_round_planning_graph.py)、[worker/dynamic_graph.py](../src/opsswarm/worker/dynamic_graph.py) | 图 v1～v10 保留各阶段历史恢复语义；图 v11 为第二轮补查创建第三轮计划，仍不执行第三轮或结算调查 |
| [synthesis/contracts.py](../src/opsswarm/investigation/synthesis/contracts.py)、[synthesis/persistence.py](../src/opsswarm/investigation/synthesis/persistence.py)、[synthesis/service.py](../src/opsswarm/investigation/synthesis/service.py) | 按轮开放假设契约、证据闭包、槽位隔离、成功调用复用、不可变写入和来源重建；不映射旧根因或处置资格 |
| [review/contracts.py](../src/opsswarm/investigation/review/contracts.py)、[review/persistence.py](../src/opsswarm/investigation/review/persistence.py)、[review/service.py](../src/opsswarm/investigation/review/service.py) | 按轮独立审查契约、同源材料、槽位隔离、成功调用复用和不可变事实；建议不创建任务或授予路由与执行资格 |
| [review_routing/contracts.py](../src/opsswarm/investigation/review_routing/contracts.py)、[review_routing/policy.py](../src/opsswarm/investigation/review_routing/policy.py)、[review_routing/persistence.py](../src/opsswarm/investigation/review_routing/persistence.py) | 按轮纯规则路由、有限原因码、冻结容量检查、不可变事实与来源重建；决定尚不执行对应路径 |
| [follow_up_planning/policy.py](../src/opsswarm/investigation/follow_up_planning/policy.py)、[follow_up_planning/service.py](../src/opsswarm/investigation/follow_up_planning/service.py)、[coordination/persistence.py](../src/opsswarm/investigation/coordination/persistence.py) | 完整补查建议的确定性下一轮编译、按来源轮次限制图能力、计划/轮次/任务原子提交与恢复；图 v11 已创建第三轮待执行任务 |
| [worker/processor.py](../src/opsswarm/worker/processor.py)、[worker/task_graph.py](../src/opsswarm/worker/task_graph.py) | 现有 v1/v2 Worker 的显式版本分派与固定图二次拒绝；通用 Worker 尚未接 v3 |

第 79 步核对了锁定 LangChain 的 middleware、ToolStrategy 与输出确认契约。工具 schema 必须与框架实际绑定一致；升级依赖时通过真实 SDK 假传输测试核对，不能凭记忆改参数。

## 7. 保留的重要决定

LangGraph/LangChain 复用框架循环、调度与 checkpoint，Harness 专注权限、上下文、预算和审计；checkpoint 不替代业务事实。内部 Agent 不使用 A2A，MCP 只用于后续只读遥测。经验从复盘生成候选，经可信人工确认后启用，不是当前事实或执行授权。新能力固定新策略，旧运行不静默换输入和恢复语义。

开发子智能体按独立边界与难度分配；主 Agent 核实关键结论、统一测试和提交。开发 Agent 的资源偏好不改变 OpsSwarm 业务角色的模型或预算。

## 8. 下一功能切片

第 97 步：**第三轮专业任务执行与恢复**。只从图 v11 已持久化的第三轮计划稳定分派专业任务，复用冻结证据范围、任务尝试、共享预算、专业工具循环和工作摘要。本片不提前综合、审查、进入图内人工等待、候选结论或规划，也不结算父运行。

第 81 步只让综合消费完成任务摘要，没有裁剪专业交互、动态分派或新增独立反证/未解问题字段。后续若需要历史压缩，应单独定义完整配对替换和必要材料保留规则，不以摘要已被消费推断 Token 已下降。

之后仍按第 64/65 篇推进动态协作 → 只读 MCP → 手册、可信身份与经验 → 人工介入/规划 → 多步骤处置与回流 → 复杂场景和工作台 → CI 与完整发布。

## 9. 验证与环境

使用仓库 `.venv/Scripts/`；完整第 96 步命令和结果见其进度记录。数据库测试由 [tests/conftest.py](../tests/conftest.py) 启动独立 PostgreSQL 17.6 与 checkpoint 数据库，需要 Docker。Windows 使用 Selector 事件循环，异步子进程继续参考现有线程桥接。

```powershell
git status --short
git log -5 --oneline
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

第 96 步测试入口为 [v3 第三轮计划图](../tests/integration/test_dynamic_third_round_planning_graph.py)、[阶段结果契约](../tests/unit/investigation/test_dynamic_contracts.py) 和 [补查计划规则](../tests/unit/investigation/test_follow_up_planning.py)；图 v10 的 [第二轮路由回归](../tests/integration/test_dynamic_second_round_routing_graph.py) 还直接验证旧图不能绕过存储层创建第三轮。图版本迁移、v3 启动和 Worker 隔离见 [v3 迁移](../tests/integration/test_dynamic_workflow_migration.py)、[v3 启动](../tests/integration/test_dynamic_workflow_start.py) 和 [Worker 版本拒绝](../tests/unit/investigation/test_worker_routing.py)。旧模型预算测试刻意清空上下文策略，不要删除这些历史契约覆盖。正式配置见 README/Compose/变量示例，不读取或提交真实 .env、个人配置和令牌。

## 10. 可直接用于继续开发的任务

> 请阅读 AGENTS.md、项目状态、进度索引和第 96 步设计/验收，核对 Git 与实际代码。v3 图 v11 已从第二轮 `follow_up` 路由原子保存第三轮计划、轮次和待执行任务；旧图 v1～v10 保留，父运行仍不结算。从第 97 步第三轮专业任务执行推进，只消费已持久化第三轮计划并复用现有冻结范围、任务运行层和共享预算。完成真实 PostgreSQL、checkpoint 兼容回归、文档和中文独立提交。不要提前综合审查、实现图内人工恢复、进入规划、开放 v3 Worker 或接入 MCP。
