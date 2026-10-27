# OpsSwarm

当线上系统报警后，OpsSwarm 自动拉取日志、指标和最近变更，组织多个 Agent 并行调查，输出带证据的根因判断和修复方案；经工程师审批后执行受控操作，并验证系统是否恢复。

## 当前状态

新会话继续开发先读 [项目状态与交接入口](docs/PROJECT_STATE.md)：包含已验收边界、代码定位、下一切片与验证方式；临时进度不放入 AGENTS.md。

MVP 已完成发布验收；第 64 步确定总方向，第 65 步形成 [Agent 系统最终设计](docs/architecture/65-agent-system-design.md)，并同步 [完整工程版开发顺序](docs/architecture/64-full-multi-agent-roadmap.md)。多 Agent 是既定架构方向，采用 LangGraph、LangChain 与内部轻量 Harness，逐步建设动态调查、分层记忆、上下文管理、只读遥测 MCP 和人工介入，当前不引入 A2A。

第 66～68 步已交付冻结 v2 的任务事实、专业结果、恢复核对与次数/时限预算。第 69 步增加显式启用的 [模型输入与费用预算](docs/architecture/69-model-input-budget.md)：在完整应用输入形成后原子预留 Token 和估算费用，保存输入快照及报告用量；各专业、综合与重试共享额度，未知用量不补零。动态协作、完整 Harness、历史裁剪、经验与 MCP 继续按计划建设。验证结果见 [当前推进位置](docs/progress/README.md) 和 [第 69 步记录](docs/progress/69-model-input-budget.md)。

第 70 步加入 [受限异步取证](docs/architecture/70-bounded-async-evidence.md)：多 Agent Worker 用最多 3 个未完成查询桥接现有同步适配器，取证不再阻塞事件循环。取消或等待超时后，运行中的请求继续占用槽位直至真正结束，迟到材料不能进入专业报告，验证结果见 [第 70 步记录](docs/progress/70-bounded-async-evidence.md)。

第 71 步加入 [模型调用前的专业取证快照](docs/architecture/71-task-evidence-snapshot.md)：新冻结 v2 在模型分析前保存有界证据、任务范围、来源、散列与成功工具引用，模型失败后复用已提交材料。运行由服务端固定 `evidence_policy_version=task-evidence-v1`，历史运行保持原恢复方式。按任务的只读 evidence 接口可在报告形成前查询取证结果，验证见 [第 71 步记录](docs/progress/71-task-evidence-snapshot.md)。这是冻结路径的交付记录，现场路径由第 72～73 步补齐。

第 72 步增加 [显式观测窗口](docs/architecture/72-explicit-observation-windows.md)：日志、指标和本地变更适配器可按传入的固定 UTC 范围查询；历史变更按发生时间和记录截止过滤，不请求当前运行清单。原相对查询兼容保留。来源侧能力与验证见 [第 72 步记录](docs/progress/72-explicit-observation-windows.md)，现场任务接入由第 73 步完成。

第 73 步接入 [现场 v2 持久任务取证](docs/architecture/73-live-task-evidence.md)：创建时固定 UTC 截止时间，日志、指标回看 15 分钟，历史变更回看 6 小时；并行查询经过共享预算，模型失败和 Worker 重启后复用同任务已提交材料。现场证据严格校验来源与范围，验证见 [第 73 步记录](docs/progress/73-live-task-evidence.md)。该步验收时使用直接只读适配器，后续由第 101 步开始迁移 MCP。

第 101 步交付 [只读 telemetry MCP 日志查询](docs/architecture/101-telemetry-mcp-log-query.md)：新建现场 v2 的日志专业任务通过独立 Streamable HTTP MCP 服务读取本地 Loki，运行固定新取证策略，历史运行继续直连恢复。服务只暴露固定服务与显式 UTC 窗口，不接收 URL、LogQL、业务身份或写权限；指标与变更仍沿用原适配器，MCP 失败不会回退冻结证据。验证见 [第 101 步记录](docs/progress/101-telemetry-mcp-log-query.md)。

第 102 步交付 [只读 telemetry MCP 指标查询](docs/architecture/102-telemetry-mcp-metric-query.md)：新建现场 v2 的指标专业任务通过独立单工具 MCP 服务读取本地 Prometheus，日志继续使用冻结的第 101 步 MCP 协议。新组合固定为 `task-evidence-live-mcp-v1`，两种历史现场策略仍按原路径恢复；指标工具不接受 URL、PromQL、指标名或查询控制参数，MCP 失败不会回退直连或冻结材料。验证见 [第 102 步记录](docs/progress/102-telemetry-mcp-metric-query.md)。

第 103 步交付 [版本化调查手册最小读取链路](docs/architecture/103-versioned-runbook-read.md)：固定 v2 可通过独立 `runbook-read-v1` 策略，为具有近期已完成部署事实的 `order-service` 变更任务提供首个 Git 管理手册。模型只能按 ID、版本和内容散列读取；成功正文进入配对工具结果，后续调用清单记录实际读取的材料版本与散列。手册不属于现场证据，不授予审批或执行权限；历史策略和 v3 不变。验证见 [第 103 步记录](docs/progress/103-versioned-runbook-read.md)。

第 104 步交付 [可信操作者身份与最小权限](docs/architecture/104-trusted-operator-identity.md)：显式 Bearer 模式从环境凭据验证主体和角色，统一保护控制面 API，拒绝越权和人工冒名，写命令先保存授权准入快照。默认 `legacy_local` 仍为兼容演练，不提供可信身份；历史审批不追认为可信，浏览器认证适配与经验生命周期留待后续。验证见 [第 104 步记录](docs/progress/104-trusted-operator-identity.md)。

第 74 步修复 [冻结场景身份与历史兼容](docs/architecture/74-stable-frozen-identity.md)：新任务使用确定性内容身份，旧任务经已发布版本兼容表核对后沿用原 ID、材料散列和预算。不同哈希种子的独立 Worker 进程用于验证恢复，详见 [第 74 步记录](docs/progress/74-stable-frozen-identity.md)。

第 75 步接入 [最小 ContextBuilder](docs/architecture/75-task-context-builder.md)：专业节点按任务组装目标、时间窗和持久证据，综合节点读取已提交报告；实际模型输入、材料 ID/版本/散列清单及预算预留一起保存。新建且启用 `model_budget` 的 v2 固定 `context_policy_version=task-context-v1`，历史运行保持原输入策略；该步尚未接入的专业按需读取由第 79 步独立策略交付，工作摘要材料由第 80 步交付，第 81 步通过独立策略接入综合消费。验证见 [第 75 步记录](docs/progress/75-task-context-builder.md)。

第 76 步增加 [证据目录与受控详情读取](docs/architecture/76-context-evidence-reader.md)：按调用查询完整摘要目录，通过任务、证据 ID 和快照散列读取详情；越界和过期引用明确失败。此为只读应用服务与控制面 API，第 79 步专业工具复用该证据读取服务；本步原始验收详见 [第 76 步记录](docs/progress/76-context-evidence-reader.md)。

第 77 步增加 [重复调用身份](docs/architecture/77-repeated-call-identity.md)：同一专业尝试可独立预留多次操作，每次仍计入共享预算；默认槽位兼容旧幂等键。该步仅完成调用身份，专业工具循环由第 79 步接入；见 [本次记录](docs/progress/77-repeated-call-identity.md)。

第 79 步交付 [专业 Agent 有界按需证据读取](docs/architecture/79-bounded-evidence-reading.md)：显式选择 `context_policy_version=evidence-read-v1`、v2 和 function_calling 模型预算 profile 后，专业模型先看本任务目录，通过唯一 read_evidence 工具读取详情并提交报告。每次模型/工具调用前预留，完整配对交互与结算同事务保存；技术重试从原快照重新开始且保留累计预算，综合保持原结构化调用。未指定新策略的请求行为保持兼容。验收和下一步工作摘要见 [第 79 步记录](docs/progress/79-bounded-evidence-reading.md)。

第 80 步增加 [已完成任务的工作摘要](docs/architecture/80-task-working-summary.md)：新建 evidence-read-v1 运行固定摘要策略，专业结果成功结算时，同事务保存完整报告、快照引用、实际已读范围和历次调用引用。只读 working-summary 接口提供不可变版本；旧运行不回填，摘要尚未进入模型上下文，不宣称压缩或节省 Token。验证见 [第 80 步记录](docs/progress/80-task-working-summary.md)。

第 81 步增加 [版本化摘要上下文](docs/architecture/81-summary-context.md)：显式选择 `summary-context-v1` 后，综合实际接收三份不可变工作摘要及完整有界证据，材料清单和预算预留绑定实际输入；组装和预留事务都核对来源。专业继续保留完整配对历史，旧策略不换输入，不宣称 Token 下降。验证见 [第 81 步记录](docs/progress/81-summary-context.md)。

第 82 步增加 [动态调查计划与确定性分派校验](docs/architecture/82-dynamic-coordination-policy.md)：协调模型只能提出本轮目标、有限专业角色、任务目标和回看秒数；系统注入事故服务、UTC 截止时间、证据快照身份与尝试上限，并校验轮次、角色权限和运行总任务额度。该闸门尚未接入模型、持久化或图路由，旧 v1/v2 保持原行为。验证见 [第 82 步记录](docs/progress/82-dynamic-coordination-policy.md)。

第 83 步增加 [动态协调计划持久化](docs/architecture/83-dynamic-coordination-persistence.md)：首次动态计划写入时固定运行策略，计划与父 Worker 尝试、实际轮次和任务原子提交，并在查询时从业务来源重建复核；固定轮次历史不能中途切换为动态语义。当前仍无协调模型、v3 启动或动态图路由。验证见 [第 83 步记录](docs/progress/83-dynamic-coordination-persistence.md)。

第 84 步增加 [协调模型结构化调用与共享预算账本](docs/architecture/84-coordination-model-ledger.md)：根级协调调用在请求前固定策略并预留运行共享预算，结构化提案通过确定性编译后才结算成功，成功调用与首轮计划建立可复核来源关系；模型返回后或计划提交后崩溃均可恢复复用。当前仍未新增 v3 启动、动态图或公开接口。验证见 [第 84 步记录](docs/progress/84-coordination-model-ledger.md)。

第 85 步增加 [独立动态工作流版本与运行策略冻结](docs/architecture/85-dynamic-workflow-start.md)：v3 创建时固定协调、摘要上下文、工作摘要、冻结证据、调用/模型预算和图恢复版本，并通过原运行接口查询完整启动契约。该步验收时现有 Worker 只领取 v1/v2，处理器和固定图也显式拒绝 v3。验证见 [第 85 步记录](docs/progress/85-dynamic-workflow-start.md)。

第 86 步增加 [v3 动态图首轮协调与 checkpoint 恢复](docs/architecture/86-dynamic-coordination-graph.md)：独立图在有效父租约内调用协调模型并提交首轮计划，checkpoint 的 metadata 和状态同时固定工作流/图版本；新 Worker 用新租约恢复时复用已成功调用或已提交计划。该图还没有专业调查与最终结论，通用 Worker 继续跳过 v3。验证见 [第 86 步记录](docs/progress/86-dynamic-coordination-graph.md)。

第 87 步增加 [v3 动态首轮任务分派与专业结果恢复](docs/architecture/87-dynamic-first-round-fanout.md)：新图 v2 按首轮计划实际选择的 1～3 个角色动态扇出，复用持久任务、冻结取证、专业循环、共享预算和工作摘要，并在部分失败及业务/checkpoint 提交窗口中只恢复必要节点。旧协调图 v1 保留；综合和终态完成前通用 Worker 继续跳过 v3。验证见 [第 87 步记录](docs/progress/87-dynamic-first-round-fanout.md)。

第 88 步增加 [v3 首轮协调综合与开放假设持久化](docs/architecture/88-dynamic-first-round-synthesis.md)：新图 v3 只读取首轮实际选择任务的工作摘要与证据，保存带成功模型调用来源的自由文本开放假设、支持证据、反证和未解问题。状态由系统固定为 `pending_review`，不映射旧根因枚举，也不生成处置建议或执行资格；旧图 v1/v2 继续按原拓扑恢复。验证见 [第 88 步记录](docs/progress/88-dynamic-first-round-synthesis.md)。

新入口通过原启动调查 API 显式提交 `"mode": "multi_agent", "data_source_mode": "frozen", "workflow_version": "v2"`，并保留原有 `expected_version`、`actor_id` 与 `reason` 字段；默认仍为 v1。也可将 `data_source_mode` 设为 `live`；现场 Worker 必须配置 `OPSSWARM_INVESTIGATION_CHANGE_PROVIDER=simulator`，GitHub 组合配置会明确失败。前端尚无版本选择器。现场运行的 `observation_cutoff_at` 与取证策略由服务端固定，不能在请求中指定；历史变更不提供当前部署证明，现场结果不生成冻结真值评测指标。运行迁移后使用原独立调查 Worker 即可路由，v1/v2 checkpoint 禁止混用。

v3 启动另需显式提交 function_calling `model_budget`，可选提交 `coordination_policy`；服务端固定 `summary-context-v1`、`task-working-summary-v1`、`task-evidence-v1` 和当前 `dynamic-investigation-graph-v15`。图按计划动态执行并恢复最多三轮专业任务、综合、独立审查和确定性路由；历史图保持版本隔离。当前冻结 v3 可由下文显式开关启用 Worker 接管，终端进入人工或候选等待。

新 v2 运行默认最多 12 次模型调用、18 次工具调用、每专业任务 6 次工具调用和 300 秒总时限，可在启动请求的 `call_budget` 中提交受限配置。`GET /api/v1/investigations/{run_id}/call-budget` 返回固定策略、截止时间与逐次预留记录；重试不重置额度。旧运行的策略与预留计数为 null，保留历史语义；预留数不代表供应商最终请求数或费用。

Token/费用预算通过启动请求的 `model_budget` 单独启用，默认不改变现有 v2 行为。Worker 需配置 `LLM_BUDGET_PROFILE_ID`、`LLM_CONTEXT_WINDOW_TOKENS` 和模型单价，并与请求中的 `profile` 完全匹配；默认单次输入估算上限 24000、输出上限 4096、累计费用估算上限 1 美元。估算覆盖消息和输出 Schema，采用保守字节规则，不能当成供应商精确计数或硬扣费上限。调用输入可按运行和调用 ID 单独查询，具体配置与边界见第 69 篇设计。未显式选择新策略的预算运行固定上下文提示版本 `fixed-task-context-v1`，清单通过 `GET /api/v1/investigations/{run_id}/calls/{call_id}/context-manifest` 读取；工具调用和历史调用返回 null，清单不出现在预算列表中。

按需读取的新交互通过 `GET /api/v1/investigations/{run_id}/calls/{call_id}/interaction` 查询；新专业输入版本为 `evidence-loop-input-v1`。默认输出预留 4096 × 七次调用超过累计 24000，开启者需显式配置合适预算（例如单次输出 1024），Worker 的 profile 仍须完全匹配。原取证与按需读取共用每任务六次工具额度；累计调用以 call-budget 为准，旧结果/评测中的 tool_call_count 保留三次原始取证语义。

MVP 阶段交付六十三个小切片：前五十一步完成了三个确定性故障的调查、审批、白名单执行、恢复验证、作战室、评测体系、统一现场数据源、跨进程 trace、常驻调查工作池、逐次尝试审计、成本展示、GitHub 只读变更来源、部署 Commit 关联、受限 CI/CD 摄取、不可变镜像 digest、单实例运行核对和严格滚动发布判定；第 52～53 步建立短时 Ed25519 运行证明和独立签发控制器，第 54～57 步依次增加本地审批中心、受控执行中心、恢复验证中心和人工关闭入口，第 58～62 步完成不可变复盘快照、生成查看与打印页面、改进行动项前后端闭环及跨事故治理中心，第 63 步完成控制面与前端的一键 Compose 交付和真实 MVP 发布验收。工程师可以复核计划后批准，单独确认并执行数据库中的白名单步骤，观察 Worker 验证结果，人工关闭 Incident、生成可复现报告并跟踪后续改进；浏览器不能提交动作参数、命令、验证阈值、观测值或报告结论。Evidence 与页面继续明确区分已验签和未签名来源。冻结模式继续提供公平评测，现场模式使用真实模型与真实遥测，但不对固定答案计算准确率。

## 为什么做 OpsSwarm

OpsSwarm 面向已经使用 Docker/Kubernetes、Prometheus/Grafana、Loki/ELK 和 GitHub/GitLab，但缺少成熟 SRE 平台的中小型研发团队。它希望解决三个问题：

- 缩短从告警到恢复的时间，降低 MTTR。
- 把分散的日志、指标和变更整理成可验证的事故证据。
- 通过风险分级、人工审批和白名单执行避免 LLM 误操作。

OpsSwarm 与原 Enterprise AI Assistant 的边界明确：原项目聚焦文档摄取、检索、引用与 RAG 评测；本项目聚焦实时工具调用、状态化 Agent 编排、安全执行、失败恢复和 Agent 评测。

## 核心流程

```text
告警 → Incident → 调查任务 → Evidence Board → 证据质疑与验证
     → 根因与处置计划 → 人工审批 → 白名单执行 → 恢复验证 → 事故报告
```

## 架构概览

控制面采用模块化单体：FastAPI API 和独立 Worker 共享领域代码及业务数据库；故障演练环境作为独立被管系统。LangChain 提供模型与工具抽象，LangGraph 负责需要持久化、并行、循环和人工暂停的 Agent 工作流。

完整设计见：

- [项目章程](docs/architecture/01-project-charter.md)
- [系统架构](docs/architecture/02-system-architecture.md)
- [技术栈选型](docs/architecture/03-technology-stack.md)
- [数据库设计](docs/architecture/04-database-design.md)
- [API 设计](docs/architecture/05-api-design.md)
- [单 Agent 调查基线](docs/architecture/06-single-agent-baseline.md)
- [持久调查任务与独立 Worker](docs/architecture/07-persistent-investigation-worker.md)
- [LangGraph 并行调查基线](docs/architecture/08-langgraph-parallel-baseline.md)
- [LangGraph PostgreSQL checkpoint 恢复](docs/architecture/09-postgresql-checkpoint-recovery.md)
- [多 Agent 持久 Worker 路由](docs/architecture/10-multi-agent-worker-routing.md)
- [Evidence Board 持久化](docs/architecture/11-evidence-board.md)
- [根因假设与确定性质疑](docs/architecture/12-hypothesis-challenge.md)
- [版本化处置计划与风险分级](docs/architecture/13-action-plan-and-risk-policy.md)
- [精确版本审批请求与原子状态迁移](docs/architecture/14-approval-request.md)
- [审批决定、过期收敛与审计](docs/architecture/15-approval-decision.md)
- [确定性白名单执行器与幂等操作](docs/architecture/16-deterministic-operation-executor.md)
- [逐次恢复验证与持久样本](docs/architecture/17-persistent-recovery-verification.md)
- [可重放事故时间线 SSE](docs/architecture/18-replayable-sse-timeline.md)
- [Incident 关闭与告警复发](docs/architecture/19-incident-closure-and-recurrence.md)
- [Incident 作战室前端](docs/architecture/20-incident-workroom-ui.md)
- [Incident 作战室只读聚合投影](docs/architecture/21-incident-workroom-projection.md)
- [版本化调查评测集与公平对照](docs/architecture/22-versioned-investigation-evaluation.md)
- [评测结果持久化与只读查询](docs/architecture/23-evaluation-persistence-and-query.md)
- [只读评测面板](docs/architecture/24-evaluation-dashboard-ui.md)
- [评测实验身份、重复运行与遥测](docs/architecture/25-evaluation-experiments-and-telemetry.md)
- [真实模型单场景冒烟与尝试生命周期](docs/architecture/26-real-model-smoke-attempts.md)
- [真实模型批量评测与聚合报告](docs/architecture/27-real-model-batch-evaluation.md)
- [处置建议契约与评测集 v2](docs/architecture/28-recommendation-contract-v2.md)
- [确定性处置计划能力映射](docs/architecture/29-action-plan-capability-mapping.md)
- [数据库连接池配置回滚计划模板](docs/architecture/30-database-pool-configuration-plan.md)
- [数据库连接池确定性故障演练](docs/architecture/31-database-pool-fault-lab.md)
- [配置回滚受控执行与恢复验证](docs/architecture/32-configuration-execution-and-verification.md)
- [持久恢复验证调度 Worker](docs/architecture/33-persistent-verification-scheduler.md)
- [Redis 不可用确定性处置闭环](docs/architecture/34-redis-dependency-recovery.md)
- [真实 Redis 与受控故障代理](docs/architecture/35-real-redis-fault-proxy.md)
- [Prometheus 指标与恢复验证](docs/architecture/36-prometheus-recovery-verification.md)
- [Prometheus 只读调查数据源](docs/architecture/37-prometheus-investigation-data-source.md)
- [结构化日志与 Loki 只读调查数据源](docs/architecture/38-structured-logs-and-loki-investigation.md)
- [持久变更记录模拟器与只读调查数据源](docs/architecture/39-change-record-simulator-and-investigation.md)
- [统一现场数据源与调查 Worker 安全切换](docs/architecture/40-unified-live-investigation-worker.md)
- [OpenTelemetry 跨进程调查追踪](docs/architecture/41-opentelemetry-investigation-tracing.md)
- [冻结与现场常驻调查工作池](docs/architecture/42-persistent-investigation-worker-pools.md)
- [调查尝试审计与模型成本](docs/architecture/43-investigation-attempt-audit.md)
- [作战室调查尝试与成本展示](docs/architecture/44-investigation-attempt-workroom.md)
- [GitHub 只读变更调查数据源](docs/architecture/45-github-readonly-change-source.md)
- [部署版本与 GitHub Commit 确定性关联](docs/architecture/46-deployment-commit-provenance.md)
- [作战室部署代码来源展示](docs/architecture/47-deployment-provenance-workroom.md)
- [受限 CI/CD 部署事件摄取](docs/architecture/48-cicd-deployment-event-ingestion.md)
- [部署构建产物摘要](docs/architecture/49-deployment-artifact-digest.md)
- [运行实例构建产物核对](docs/architecture/50-runtime-artifact-verification.md)
- [多实例运行清单与滚动发布判定](docs/architecture/51-runtime-inventory-rollout.md)
- [Ed25519 签名运行清单证明](docs/architecture/52-signed-runtime-inventory-proof.md)
- [独立部署控制器与动态证明刷新](docs/architecture/53-deployment-controller-runtime-proof-refresh.md)
- [本地审批中心与待办队列](docs/architecture/54-approval-center-ui.md)
- [已批准计划的受控执行中心](docs/architecture/55-controlled-execution-ui.md)
- [恢复验证中心与采样进度展示](docs/architecture/56-recovery-verification-center.md)
- [已恢复 Incident 的人工关闭入口](docs/architecture/57-incident-closure-ui.md)
- [确定性事故复盘报告快照](docs/architecture/58-persistent-incident-report.md)
- [事故复盘报告生成、查看与打印](docs/architecture/59-incident-report-ui.md)
- [复盘改进行动项与完成审计](docs/architecture/60-report-action-items.md)
- [复盘改进行动项前端闭环](docs/architecture/61-report-action-items-ui.md)
- [跨事故改进行动项治理中心](docs/architecture/62-follow-up-governance-center.md)
- [MVP 一键 Compose 交付与发布验收](docs/architecture/63-mvp-compose-release.md)
- [完整多 Agent 工程版规划与基础约束](docs/architecture/64-full-multi-agent-roadmap.md)
- [Agent 系统最终设计：轻量 Harness、分层记忆与受控工具](docs/architecture/65-agent-system-design.md)
- [调查轮次、专业任务与尝试持久化](docs/architecture/66-investigation-task-persistence.md)
- [固定调查图接入任务事实、专业结果与进度事件](docs/architecture/67-fixed-graph-task-integration.md)
- [运行级共享调用预算与预留账本](docs/architecture/68-shared-call-budget.md)
- [模型输入、Token 与估算费用预算](docs/architecture/69-model-input-budget.md)
- [受限异步取证与迟到结果隔离](docs/architecture/70-bounded-async-evidence.md)
- [模型调用前的专业取证快照与恢复复用](docs/architecture/71-task-evidence-snapshot.md)
- [现场来源的显式观测窗口](docs/architecture/72-explicit-observation-windows.md)
- [现场 v2 持久任务取证](docs/architecture/73-live-task-evidence.md)
- [冻结场景身份与历史兼容](docs/architecture/74-stable-frozen-identity.md)
- [开发进度与复盘](docs/progress/README.md)

## 实施路线

以下保留 MVP 建设路线。下一阶段按 [最终开发顺序](docs/architecture/64-full-multi-agent-roadmap.md) 逐片推进任务持久化、最小 Harness、上下文、动态调查、MCP、手册与经验、人工介入、处置、现场工作台和发布验收；可信身份先于经验审核交付。专项规则统一保存在第 65 篇设计中，不一次生成整个工程。

1. 项目章程、架构边界和最小目录。
2. 确定性故障模拟服务与 Incident 状态机，不接 LLM。
3. 单 Agent 调查基线。
4. 持久调查任务、租约重试和独立 Worker。
5. 协调 Agent 与日志、指标、变更并行调查节点。
6. Evidence Board、质疑节点和结构化处置计划。
7. 人工审批、白名单执行器和幂等操作。
8. 恢复验证、失败恢复与时间线 UI。
9. 评测集以及单 Agent、多 Agent 对比。
10. 组合三类真实现场数据源并安全切换持久 Worker。
11. 接入 OpenTelemetry，贯通 API、持久任务、Agent 节点和工具请求。
12. 把调查 Worker 改为可优雅停机的冻结/现场常驻工作池。
13. 持久每次调查尝试的耗时、结果、Token 与费用。
14. 在 Incident 作战室展示调查尝试与成本。
15. 接入固定仓库、最小权限和失败关闭的 GitHub 只读变更工具。
16. 使用私有仓库凭据完成无模型冒烟，并验证现场 Worker 的组合变更来源。
17. 建立服务、代码仓库、部署版本与 Commit SHA 的确定性映射。
18. 在 Incident 作战室只读展示部署代码来源和匹配状态。
19. 设计受限 CI/CD 部署事件摄取，自动写入可信代码引用。
20. 补充构建产物 digest，让 Commit、镜像与运行版本形成确定性来源链。
21. 增加只读运行身份，并确定性核对最新部署声明与当前进程启动身份。
22. 增加有界多实例运行清单和严格滚动发布判定，不扩大 Agent 权限。
23. 建立最小权限、带时效和签名的部署控制器运行证明契约。
24. 增加独立本地部署控制器模拟器和受限动态证明刷新。
25. 增加有界待审批队列和本地审批中心，保持审批与执行分离。
26. 增加最新调查限定的受控执行中心和稳定浏览器幂等键。
27. 增加恢复验证中心，只读展示持久 Worker 的采样进度和最终恢复判定。
28. 在作战室增加带乐观锁和复发保护的人工关闭入口。
29. 为已关闭 Incident 生成带结构版本和内容摘要的确定性复盘快照。
30. 在作战室生成、查看并打印不可变事故复盘报告。
31. 为冻结报告建立有负责人、截止时间、乐观锁和追加式审计的改进行动项。
32. 在事故复盘页面读取、创建并完成改进行动项，保留幂等和版本冲突语义。
33. 增加跨事故 open 待办、服务器逾期判定和负责人筛选的只读治理中心。
34. 把控制面和前端纳入一键 Compose 交付，并完成跨模块 MVP 发布验收。

阶段 1 的版本回归、数据库连接池配置错误和 Redis 不可用三个场景都已具备本地确定性故障、计划、审批、白名单执行和恢复验证链路。Redis 场景通过固定 TCP 故障代理访问独立真实 Redis 容器；三种恢复信号由项目内 Prometheus 真实抓取，验证 Worker 只执行固定查询。调查 Worker 已能显式选择冻结评测数据或完整现场数据，选择随运行持久化且不会自动回退混用。API 产生的 W3C Trace Context 随 `work_items` 持久化，Worker 重试仍接回同一 trace；Jaeger 只保存本地诊断数据，不替代任何业务审计。评测集快照、运行、逐场景结果和聚合指标已经持久化到 PostgreSQL，并通过只读接口提供给评测面板。所有环境读写仍只允许本地故障演练环境。

## 单 Agent 与多 Agent 任务模式

开始调查接口的 `mode` 可取 `single_agent` 或 `multi_agent`，`data_source_mode` 可取 `frozen` 或 `live`；两者省略时分别使用单 Agent 和冻结数据。模式不是模型决定的，而是作为业务运行和持久任务的一部分写入 PostgreSQL。

```json
{
  "expected_version": 1,
  "actor_id": "engineer-001",
  "reason": "使用并行调查模式",
  "mode": "multi_agent",
  "data_source_mode": "live"
}
```

Worker 校验运行模式、任务类型和数据源模式一致：单 Agent 调用 LangChain 基线；多 Agent 查询对应线程是否已有 checkpoint，存在则恢复，不存在才启动新图。冻结 Worker 不会领取现场任务，现场 Worker 也不会领取冻结任务。

## LangGraph PostgreSQL checkpoint

业务事实仍保存在 `opsswarm`，LangGraph 节点快照保存在同一 PostgreSQL 容器中的独立数据库 `opsswarm_checkpoints`。它们通过业务库的 `investigation_runs.graph_thread_id` 关联，不建立跨库外键。

本地首次初始化：

```powershell
docker compose up -d --wait control-db
uv run python -m opsswarm.checkpoints.setup
```

全新数据卷会通过 Compose 初始化脚本创建 checkpoint 数据库；LangGraph 自己的表由上述幂等命令管理，不归 Alembic 管理。默认 URL 可通过 `OPSSWARM_CHECKPOINT_DATABASE_URL` 覆盖。

## LangGraph 并行调查基线

`LangGraphParallelInvestigator` 使用显式状态图协调三个并行专业节点，并在全部完成后运行综合 Agent。专业报告只能引用本分支证据，最终结论只能引用全图已收集证据。

当前三故障冻结契约中，单 Agent 和并行图的 Top-1、证据召回、安全违规率和工具调用数相同，尚不能证明多 Agent 更优。定向运行：

```powershell
uv run pytest tests/unit/investigation/test_parallel_graph.py -q
```

## 版本化调查评测集

`opsswarm-mvp-investigation-v1` 固定了版本回归、数据库连接池配置错误和 Redis 不可用三个场景。数据集版本、评分规则版本和完整内容 SHA-256 会写入报告；场景执行顺序交替，避免一种模式永远占用冷启动或热缓存位置。

运行不访问网络的契约评测：

```powershell
uv run python -m opsswarm.evaluation.main
```

该命令使用冻结假模型真实经过 LangChain 工具循环和 LangGraph 并行图，用于验证数据、证据护栏、评分和 JSON 报告契约。输出的满分与本地耗时不能代表真实模型质量；真实模型评测必须记录模型、提示、Token、成本、失败和多次重复运行。

显式保存一轮完整报告到业务库：

```powershell
uv run alembic upgrade head
uv run python -m opsswarm.evaluation.main `
  --persist `
  --idempotency-key fixture-contract-exp01-r01 `
  --experiment-key fixture-contract-exp01 `
  --repetition-index 1
```

相同实验的后续重复使用同一 `--experiment-key`、新的幂等键和递增的 `--repetition-index`。不带 `--persist` 时不会连接数据库。评测历史只能通过 `GET /api/v1/evaluation-runs` 和 `GET /api/v1/evaluation-runs/{run_id}` 读取；控制面不提供启动评测或改写报告的 HTTP 接口。

## 单 Agent 调查基线

`opsswarm.investigation` 使用 LangChain 1.x 组装单 Agent 工具循环，但只暴露日志、指标和变更三个有界只读工具。结论必须符合 `InvestigationConclusion`，并且只能引用本轮工具实际返回的证据 ID。三类快照全部取得后，中间件会移除调查工具并只保留结构化结论工具，避免模型重复查询；单次运行默认最多调用 6 次调查工具。固定场景用于离线计算根因 Top-1、证据召回、建议准确率、安全违规率和工具调用数。

默认测试使用假模型和冻结工具结果，不需要模型密钥或网络：

```powershell
uv run pytest tests/unit/investigation -q
```

真实 OpenAI 兼容模型仅通过 `LLM_API_KEY`、`LLM_MODEL`、可选 `LLM_BASE_URL`、`LLM_TEMPERATURE`、`LLM_THINKING_MODE` 和 `LLM_STRUCTURED_OUTPUT_METHOD` 显式配置。自动化测试不读取这些变量，也不会产生模型费用。供应商只支持普通 JSON Output 时选择 `json_mode`；此时适配器会把 Pydantic JSON Schema 放入提示并继续在本地严格校验。

第一次真实模型测试使用独立的单场景冒烟命令。先把 `.env.example` 复制为 `.env`，填写模型配置、该模型每百万输入/输出 Token 的实际美元价格以及费用上限，再执行：

```powershell
uv run alembic upgrade head
uv run python -m opsswarm.evaluation.real_main `
  --experiment-key real-model-smoke-exp01 `
  --idempotency-prefix real-model-smoke-exp01-r01 `
  --repetition-index 1
```

命令会先分别登记单 Agent、多 Agent 尝试，再调用模型；重放同一幂等前缀不会重复付费。尝试历史通过 `GET /api/v1/evaluation-attempts` 和详情接口只读查询，控制面不能启动模型评测。

三场景批量冒烟默认各运行一次，并额外受 `EVALUATION_MAX_BATCH_COST_USD` 约束：

```powershell
uv run python -m opsswarm.evaluation.real_batch_main `
  --experiment-key real-model-three-scenario-exp01 `
  --idempotency-prefix real-model-three-scenario-exp01 `
  --dataset-version 2 `
  --repetition-start 1 `
  --repetitions 1
```

批量命令默认仍选择历史 v1，避免旧命令静默改变实验含义；新实验必须显式传入 `--dataset-version 2`。批量报告从已持久化尝试派生，不创建第二份可变业务事实；相同命令重放会复用原序号和终态，不再次调用模型。

## 持久调查 Worker

开始调查接口会在同一事务中保存 Incident 状态变化、`investigation_runs` 和 `work_items`。Worker 使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 领取任务，并通过到期时间和唯一租约令牌阻止迟到实例覆盖结果。

默认入口是常驻轮询进程，会显式读取项目根目录 `.env` 中的模型配置；真实 `.env` 已被 Git 忽略。推荐直接启动两个独立 Compose 工作池：

```powershell
docker compose up -d investigation-worker-frozen investigation-worker-live
```

冻结工作池只领取 `frozen` 任务，现场工作池只领取 `live` 任务。它们在空闲时低频轮询，收到停机信号后不再领取新任务。默认配置允许当前任务在 100 秒处理超时内结算，Compose 给予 2 分钟宽限；动态池可按下文调整。手工调试可使用 `--once` 至多处理一个任务后退出：

```powershell
$env:LLM_API_KEY = "本地模型服务密钥"
$env:LLM_MODEL = "模型名称"
$env:LLM_BASE_URL = "可选 OpenAI 兼容地址"
$env:LLM_THINKING_MODE = "provider_default、enabled 或 disabled"
$env:LLM_STRUCTURED_OUTPUT_METHOD = "json_schema、json_mode 或 function_calling"
$env:OPSSWARM_WORKER_DATA_SOURCE_MODE = "frozen 或 live"
uv run python -m opsswarm.worker.main --once
```

第 111 步已将冻结 v3 的当前 graph-v15 接入 Worker；默认仍只领取 v1/v2。先执行最新迁移，显式设置以下变量启用动态演练池（模型结构化输出须为 `function_calling`）：

```powershell
$env:OPSSWARM_WORKER_DATA_SOURCE_MODE = "frozen"
$env:OPSSWARM_WORKER_ENABLE_DYNAMIC_WORKFLOW = "true"
$env:LLM_STRUCTURED_OUTPUT_METHOD = "function_calling"
$env:OPSSWARM_WORKER_PROCESSING_TIMEOUT_SECONDS = "240"
$env:OPSSWARM_WORKER_LEASE_DURATION_SECONDS = "300"
$env:OPSSWARM_WORKER_STOP_GRACE_PERIOD = "5m"
```

启动 v3 三轮演练时建议显式设置调用预算 `max_model_calls=16, max_duration_seconds=900`；总时限从首次开始计算，需为租约过期后的恢复留出窗口，恢复不会重置预算或延长 deadline。

Compose 仅将动态开关传入冻结池。动态演练使用 `release-500-worker-v1`，把固定模拟材料的最新观察锚定事故开始时间，按完整内容计算稳定快照；限 order-service 发布故障，不是真实现场。历史 v3 图不会被新 Worker 领取，其他冻结来源的旧 graph-v15 也不能混入新快照。终端交接后父运行是 `await_human` 或 `await_candidate`，父尝试 `handed_off`、工作项释放租约；没有旧根因成功结果，也不授予执行资格。`GET /api/v1/investigations/{run_id}/terminal-handoff` 可查精确路由来源。详见 [终端交接设计](docs/architecture/111-dynamic-worker-handoff.md)。

第 112 步提供 [可信人工补充与取消](docs/architecture/112-trusted-human-wait-commands.md)。先配置第 104 步的有效工程师 Bearer 凭据；即使控制面使用 `legacy_local`，以下入口也要求可信认证。以已有 await_human 运行 ID 为例，令牌由本地环境变量读取：

```powershell
$waitHeaders = @{ Authorization = "Bearer $env:OPSSWARM_OPERATOR_TOKEN" }
$waitUrl = "http://localhost:8000/api/v1/investigations/$runId/human-wait"
$waitState = Invoke-RestMethod -Uri $waitUrl -Headers $waitHeaders
$waitCommand = @{
  handoff_id = $waitState.handoff_id
  source_sha256 = $waitState.source_sha256
  expected_wait_version = $waitState.state_version
  idempotency_key = [guid]::NewGuid().ToString()
  reason = "补充演练调查约束"
  items = @(@{ kind = "constraint"; text = "只读调查，暂不允许执行回滚。" })
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "$waitUrl/supplements" -Headers $waitHeaders -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($waitCommand))
Invoke-RestMethod -Uri "$waitUrl/commands?limit=20" -Headers $waitHeaders
```

取消调用 `POST $waitUrl/cancel`，正文保留交接、散列、最新等待版本、幂等键和原因，去掉 items。首次成功为 201，同主体同键完整命令重放为 200；版本或内容冲突为 409。最多 15 次补充，每次 1～5 项，每项最多 1000 字。人工陈述不变成工具证据，也尚未进入模型上下文。取消后 run 为 cancelled、Incident 仍 investigating，旧成功命令和交接仍可追溯。实际模型重评、图内 interrupt/resume 与候选评估器尚待后续切片，原预算和 deadline 保留。

第 113 步增加 [可信人工重评输入准备](docs/architecture/113-human-input-preparation.md)。在至少一次补充后，可以按最新等待版本创建独立准备任务。Worker 实际生成完整待重评 ModelInput、来源清单和原预算诊断；必要材料过大或预算过期会明确 blocked，不静默裁剪人工约束。准备阶段没有调用模型，ready 也不是调用预留或恢复授权。

```powershell
$waitState = Invoke-RestMethod -Uri $waitUrl -Headers $waitHeaders
$prepareCommand = @{
  handoff_id = $waitState.handoff_id
  source_sha256 = $waitState.source_sha256
  expected_wait_version = $waitState.state_version
  idempotency_key = [guid]::NewGuid().ToString()
  reason = "核对人工约束和待重评预算"
} | ConvertTo-Json
$preparation = Invoke-RestMethod -Method Post -Uri "$waitUrl/preparations" -Headers $waitHeaders -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($prepareCommand))
uv run python -m opsswarm.investigation.human_prepare.main --once
Invoke-RestMethod -Uri "$waitUrl/preparations/$($preparation.job.id)" -Headers $waitHeaders
Invoke-RestMethod -Uri "$waitUrl/preparations/commands?limit=20" -Headers $waitHeaders
```

使用 Compose 时可启动 `human-preparation-worker`，它仅使用业务数据库；需要先构建当前运行镜像并执行最新迁移。每等待版本最多一个准备任务、每任务最多三次尝试，租约默认 600 秒且不会延长原运行 deadline。后续补充或取消使旧输入 stale，历史工件仍可查询。实际模型重评由下述第 114 步独立 Worker 完成，不能重跑原 graph-v15 代替恢复。

第 114 步增加 [实际人工重评](docs/architecture/114-human-input-reassessment.md)。工程师必须另行提交显式重评命令；准备就绪本身不会调用模型。准备 GET 响应提供 `artifact_sha256` 完整工件散列，命令须精确绑定该散列与最新等待版本：

```powershell
$preparedView = Invoke-RestMethod -Uri "$waitUrl/preparations/$($preparation.job.id)" -Headers $waitHeaders
$waitState = Invoke-RestMethod -Uri $waitUrl -Headers $waitHeaders
$reviewCommand = @{
  action = "reassess"
  handoff_id = $waitState.handoff_id
  source_sha256 = $waitState.source_sha256
  expected_wait_version = $waitState.state_version
  preparation_sha256 = $preparedView.artifact_sha256
  idempotency_key = [guid]::NewGuid().ToString()
  reason = "按完整人工约束进行独立重评"
} | ConvertTo-Json
$reviewUrl = "$waitUrl/preparations/$($preparation.job.id)/reassessments"
$reviewJob = Invoke-RestMethod -Method Post -Uri $reviewUrl -Headers $waitHeaders -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($reviewCommand))
uv run python -m opsswarm.investigation.human_review.main --once
Invoke-RestMethod -Uri "$reviewUrl/$($reviewJob.job.id)" -Headers $waitHeaders
Invoke-RestMethod -Uri "$reviewUrl/commands?limit=20" -Headers $waitHeaders
```

Worker 模型、输出方式、价格和 profile 必须匹配原运行的固定配置，SDK 重试关闭；新调用继续使用原共享账本与原 deadline。CLI 从环境变量加载配置。Compose 可显式启用 `human-reassessment` profile 并启动 `human-review-worker`；需先构建当前镜像、执行最新迁移并配置相同模型。调用前预留累计次数、Token 与费用，返回后再次校验；未知或超预留用量不归零。补充、取消或租约失效后的结果不会发布，失联预留记 unknown；每任务最多三次尝试，成功调用可跨尝试复用。结果独立保存，原假设、审查、图和审批保持历史；第 115 步提供下述后续意图，仍无执行资格。

第 115 步增加 [重评后续路由](docs/architecture/115-human-reassessment-routing.md)。成功意见同事务保存唯一、不可变的确定性意图；历史第 114 步成功意见由同一重评 Worker 有界补齐，不额外调用模型。查询使用可信 Bearer：

```powershell
Invoke-RestMethod -Uri "$reviewUrl/$($reviewJob.job.id)/route" -Headers $waitHeaders
```

响应 `record.decision.route` 区分 `await_human`、`follow_up`、`candidate_evaluation`，固定 `dispatch_status=not_dispatched`。原轮次/任务容量、反证、未解决人工约束、原时限及共享预算都参与判断；完整约束和预算快照可追溯。`availability` 区分 pending/current/stale，`current_blocking_reason_codes` 单独诊断当前预算/约束，不改写历史决定。current 只表示输入仍适用，既不代表已分派，也不授权执行。补充/取消后旧路由保留为 stale；原 run/work/attempt 和 checkpoint 保持，候选评估由下述第 116 步显式消费，预算追加仍待后续。

第 116 步增加 [受限候选评估](docs/architecture/116-candidate-evaluation.md)。原终端交接或当前重评候选路由均可作为来源，工程师须另行提交命令；`source_sha256` 绑定**完整来源记录**散列，而非旧 `source_sha256` 字段。原终端来源为 `source_kind=terminal_handoff`、`expected_wait_version=null`；重评来源为 `human_route` 和精确等待版本。其余字段为 `action=evaluate_candidate`、`source_id`、`idempotency_key`、`reason`。

```powershell
$candidateUrl = "http://127.0.0.1:8000/api/v1/investigations/$runId/candidate-evaluations"
# 正文以待评估的完整来源及其规范 JSON 散列构造，使用工程师 Bearer。
$candidateJob = Invoke-RestMethod -Method Post -Uri $candidateUrl -Headers $waitHeaders -ContentType "application/json" -Body $candidateBody
uv run python -m opsswarm.investigation.candidate.main --once
Invoke-RestMethod -Uri "$candidateUrl/$($candidateJob.job.id)" -Headers $waitHeaders
Invoke-RestMethod -Uri "$candidateUrl/commands" -Headers $waitHeaders
```

独立 Worker 实际消费完整调查材料、当前审查、人工前缀及固定三项能力目录。模型只提能力与证据，参数、风险与验证规则由候选明确引用的结构化证据推导；不支持或缺参时等待人工，安全候选仅形成 `planning_review`，始终 `execution_eligible=false`。它不创建 ActionPlan/审批，不把开放假设转成既有根因。来源变化、事故关闭、过期或失租后拒绝发布；新阶段真实尝试与旧父来源分别记录，继续共享原预算，成功调用可复用。Compose 可显式启动 `candidate-evaluation` profile 的 `candidate-evaluation-worker`；模型配置要求同原运行，SDK 重试关闭，需使用最新镜像与迁移。未升级用户业务库。

第117步增加 [候选草案可信准入](docs/architecture/117-candidate-plan-admission.md)。可信工程师可从当前成功且为 `planning_review` 的候选生成独立单步骤 `ActionPlan` 草案；服务重算原证据、参数和风险，不接受客户端计划正文，不发送模型请求或追加预算。

```powershell
$candidate = Invoke-RestMethod -Uri "$candidateUrl/$($candidateJob.job.id)" -Headers $waitHeaders
$planUrl = "$candidateUrl/$($candidateJob.job.id)/action-plan"
# incidentVersion 必须取当前 Incident.version，不能沿用过期版本。
$planBody = @{
    action = "create_plan"
    candidate_result_sha256 = $candidate.result.content_sha256
    expected_wait_version = $candidate.wait_version
    expected_incident_version = $incidentVersion
    idempotency_key = "candidate-plan-001"
    reason = "生成待审草案，保留前置条件与未解问题"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri $planUrl -Headers $waitHeaders -ContentType "application/json" -Body $planBody
Invoke-RestMethod -Uri $planUrl -Headers $waitHeaders
Invoke-RestMethod -Uri "$planUrl/commands" -Headers $waitHeaders
```

草案存入独立 `candidate_action_plans`，与旧根因计划表隔离；保留开放假设ID、候选完整结果散列、证据引用、前置条件和未解问题，固定 `draft`，`approval_eligible=false`、`execution_eligible=false`。旧审批或执行接口不能消费其ID。取消、补充、事故版本变化或原截止时间经过后，历史内容保留并返回 stale，精确重放仍为200；新命令不沿用过期来源。可信审批绑定留待下一片；不把候选可行性描述为根因证明，也未升级用户Compose业务库。

任务创建时会把 `data_source_mode` 写入 `investigation_runs`。默认 `frozen` 保持历史行为并计算版本化评测指标；新建 `live + v2` 的日志与指标来源由 `LiveInvestigationDataSource` 分别委托独立单工具 MCP，变更仍委托原适配器，任一来源失败都会进入既有任务重试，不回退为直连或冻结证据。历史 `task-evidence-live-v1` 恢复时仍直连 Loki 和 Prometheus，`task-evidence-live-mcp-log-v1` 仍只有日志走 MCP。变更来源默认仍是本地模拟器，显式选择 `simulator_and_github` 后才会补充固定 GitHub 仓库证据。现场运行仍保存结论、Evidence Board 和 `result.tool_call_count`，但 `metrics` 固定为空，因为现场没有冻结真值。

每次领取都创建 `investigation_attempts`，成功、失败或租约过期后结算。只读查询：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/investigations/{run_id}/attempts
```

已有 `.env` 中的 `EVALUATION_INPUT_COST_PER_MILLION_USD` 与 `EVALUATION_OUTPUT_COST_PER_MILLION_USD` 作为当前模型价格兼容来源。如果供应商没有返回完整 usage，API 保留已观察 Token，但费用返回空，不会误记为免费。

作战室会读取当前调查的该接口，展示每次领取的状态、耗时、重试、错误和模型资源消耗。运行中、用量不完整与价格未配置都有不同提示；点击六阶段区域的“同步”会同时刷新尝试历史。

不调用模型、一次验证三个 Agent 工具与现场系统的统一只读链路：

```powershell
uv run python -m opsswarm.investigation.live_smoke
```

不调用模型、直接验证 Agent 指标工具与真实 Prometheus 的只读链路：

```powershell
uv run python -m opsswarm.investigation.prometheus_smoke --lookback-minutes 15
```

不调用模型、直接验证 Agent 日志工具与真实 Loki 的只读链路：

```powershell
uv run python -m opsswarm.investigation.loki_smoke --lookback-minutes 15
```

不调用模型、直接验证 Agent 变更工具与持久变更服务的只读链路：

```powershell
uv run python -m opsswarm.investigation.change_smoke --lookback-hours 6
```

GitHub 默认关闭。需要补充固定私有仓库时，在不提交的 `.env` 中把 `OPSSWARM_INVESTIGATION_CHANGE_PROVIDER` 改为 `simulator_and_github`，设置 `OPSSWARM_GITHUB_REPOSITORY=kill-t/ops-swarm`，并填写只授予 `Contents: read` 与 `Pull requests: read` 的细粒度令牌。先运行不调用模型的独立冒烟：

```powershell
uv run python -m opsswarm.investigation.github_change_smoke --lookback-hours 6
```

GitHub 文本只作为不可信证据数据输入 Agent，不能改变工具、角色或执行规则。当前新调查使用 `single-agent-v3` 和 `multi-agent-parallel-v3`；历史提示版本不迁移。

本机私有仓库无模型冒烟已经完成，现场 Worker 也已使用 `simulator_and_github` 常驻运行。部署事实现在可以携带固定仓库和完整 Commit SHA；组合来源只有在两者与 GitHub Commit Evidence 精确相等时才标记 `matched`。没有声明来源的历史部署保持 `not_declared`，当前窗口未观察到提交时标记 `commit_not_observed`，不会把任意最近提交误判为已经上线。作战室通过当前调查的持久 Evidence Board 展示这些状态，并再次校验关联 Evidence 与 GitHub Commit URL 白名单；页面不会重新查询 GitHub。

变更服务作为 Compose 常驻容器运行在 `127.0.0.1:18002`。本地管理接口只接受发布、数据库连接池配置和 Redis 维护三种固定演练记录；默认关闭的 CI/CD 入口只接受 HMAC 认证、代码与镜像双白名单、完整 Commit SHA、SHA-256 镜像 digest 和成功状态的部署事实。查询接口仅支持 `order-service` 与 1～24 小时时间窗。记录保存在业务 PostgreSQL，重启 `change-service` 不会丢失。

订单服务保留 `GET /api/v1/runtime-metadata` 单实例端点，并在 `GET /api/v1/runtime-inventory` 提供默认关闭的有界运行清单。部署环境可注入当前实例、最多 63 个其他实例和严格发布窗口；现场调查 Worker 只读取一次，只把最新部署与实例集合比较，并且只接受紧邻上一部署作为滚动发布旧版本。稳定期混跑、窗口过期、目标错误和未知 digest 都会明确告警。可选签名模式由独立部署控制器用 Ed25519 私钥签完整清单，订单服务只持有最多 4 把轮换公钥；动态模式中订单服务每次查询都从固定地址刷新，限制 3 秒超时和 64 KiB 响应并不缓存过期证明。证明最长有效 5 分钟，刷新失败、篡改、未知密钥、过期或没有精确绑定当前进程时均失败关闭。订单容器没有私钥、Docker Socket 或 Kubernetes 凭据；当前证明认证控制器声明，不等同于服务发现、实例存活或镜像供应链签名。

## OpenTelemetry 调查追踪

追踪默认关闭，避免测试和普通开发流程产生额外网络依赖。启动本地 Jaeger 并发送不含业务内容的冒烟：

```powershell
docker compose up -d jaeger
uv run python -m opsswarm.observability.smoke
```

Jaeger UI 位于 `http://127.0.0.1:16686`。需要追踪控制面和调查 Worker 时，在 `.env` 或当前终端显式设置：

```powershell
$env:OPSSWARM_OTEL_ENABLED = "true"
$env:OPSSWARM_OTEL_TRACES_ENDPOINT = "http://127.0.0.1:14318/v1/traces"
```

控制面返回 `X-Trace-ID`，开始调查时只把 W3C `traceparent`/`tracestate` 保存到 `work_items.trace_context`，不保存 baggage。Worker 每次尝试作为原 API span 的子跨度；模型 HTTP、LangGraph 节点和三个只读工具继续形成子跨度。日志 MCP 失败码也只以固定枚举写入调用账本，不保存地址、响应体或 traceback。追踪只记录有限标识、模式、计数和稳定错误类型，不记录密钥、提示词、模型响应或原始证据。Jaeger 当前使用内存存储，容器重启会丢失 trace，这是可观测数据而不是业务事实。

## Evidence Board

Worker 只有持有有效任务租约时，才能在同一 PostgreSQL 事务中完成调查运行、持久任务、规范化证据和专业报告。单 Agent 保存工具实际返回的证据；多 Agent 额外保存日志、指标和变更专业报告及其引用关系。数据库复合外键禁止报告跨调查引用证据。

查询已提交的 Evidence Board：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/investigations/{run_id}/evidence-board
```

运行中或重试中的 LangGraph 节点输出只属于 checkpoint，不会被该接口提前作为业务事实返回。

## 根因假设质疑

多 Agent 图在综合结论后运行 `challenge_hypothesis`，单 Agent 处理器复用相同纯领域函数。该节点不调用模型，而是把每条证据标记为 `supports`、`refutes` 或 `context`：只有至少两类独立证据支持且没有反驳时，主根因才进入 `supported`；否则保持 `contested` 或 `insufficient_evidence`。

质疑规则使用 `hypothesis-challenge-v1` 版本并随结果持久化。Worker 和仓储都会重新计算质疑结果，模型不能自行把假设标记为通过。Evidence Board 查询响应中的 `hypotheses` 会返回状态、规则版本和逐条理由。

## 版本化处置计划

`GET /api/v1/investigations/{run_id}/action-plans/capability` 会先把调查根因与建议成对映射为 `available`、`unsupported` 或 `not_applicable`。当前版本回归使用 `rollback-release-v1`，数据库连接池配置错误使用 `rollback-configuration-database-pool-v1`，Redis 不可用使用 `restore-redis-dependency-v1`。

`POST /api/v1/investigations/{run_id}/action-plans` 只消费已经成功提交的调查、`supported` 主假设、匹配的可用模板和规范化证据。回滚模板从变更证据提取当前值和目标值；Redis 模板还要求指标证据明确显示当前不可用。相同假设并发重复请求只会生成一个版本。

新计划使用 `action-risk-v3` 把版本回滚、配置回滚和 Redis 依赖恢复固定判为 `high` 且必须人工审批，读取时仍兼容历史 `action-risk-v1`、`action-risk-v2` 计划。Pydantic 限制动作和参数形状，风险策略在写入及读取时重算，PostgreSQL 还禁止高风险计划把审批要求设为假。三个动作都必须经过精确版本审批，配置和依赖动作还会在环境写入前复核计划中的当前状态。

## 精确版本审批请求

`POST /api/v1/action-plans/{plan_id}/approval-requests` 创建固定 30 分钟有效的 `pending` 请求。它绑定计划 ID、版本和内容 SHA-256，并在同一事务中把计划及 Incident 迁移为 `awaiting_approval`、追加时间线事件。同计划重试返回原请求，不重复迁移。

批准和拒绝使用独立语义化接口；终态不可覆盖，同方向重试幂等，相反决定返回冲突。到达时已过期的请求由系统收敛为 `expired`，计划和 Incident 返回调查语义。`actor_id` 仍只是本地审计声明，不是可信登录身份。

批准后计划为 `approved`，Incident 为 `mitigating`。随后只能通过具体步骤 ID 和幂等键调用白名单执行接口；执行器重新校验当前计划、批准记录、Incident 版本和固定动作参数，成功后进入 `verifying`，失败后返回 `investigating`。

## 确定性白名单执行

`POST /api/v1/action-steps/{step_id}/executions` 不接受 Shell、URL 或动作参数，只接受幂等键和期望 Incident 版本。适配器把 `rollback_release` 映射为关闭固定版本故障，把 `rollback_configuration + database_pool_size` 与 `restore_dependency + redis` 分别映射为固定管理端点的比较并设置；状态已经漂移时拒绝覆盖，已经位于目标值时按安全重放成功。

执行记录先以 15 秒租约写入 PostgreSQL，再调用最长 3 秒的本地 HTTP 适配器，最后以有效租约提交成功或失败终态。同一幂等键重复请求不会重复执行；进程中断后只能用相同键在租约过期后接管。外部动作使用“设置为关闭”而非“切换”，因此物理重放仍保持相同效果。

这是一种可恢复的至少一次执行设计，不宣称跨 PostgreSQL 和 HTTP 实现分布式事务或物理上的恰好一次。执行成功只表示动作已完成，Incident 会进入 `verifying`；恢复结论由独立验证运行判断。

## 逐次恢复验证

`POST /api/v1/operation-executions/{execution_id}/verification-runs` 为成功操作创建唯一验证运行。版本回滚冻结 `http_5xx_ratio < 0.01`，配置回滚冻结 `database_pool_saturation < 0.8`，Redis 恢复冻结 `redis_unavailable_ratio < 0.01`；三个规则都要求 60 秒窗口内至少 3 个成功样本，不会让单个 HTTP 请求阻塞 60 秒。

验证运行创建时会在同一事务中投递 `verification_work_items`。常驻 Worker 默认按冻结窗口查询 Prometheus 中动作绑定的固定指标；三样本、60 秒规则安排在约 0、30、60 秒。客户端与 LLM 不能提供 PromQL，查询结果还必须通过唯一时间序列、标签、范围和新鲜度校验。每个调度轮次使用稳定幂等键，样本已落库但 Worker 尚未结算时，租约到期重领不会重复探测。

`POST /api/v1/verification-runs/{run_id}/samples` 继续保留为本地诊断和幂等重放接口，客户端仍不能提交观测数值。`GET /api/v1/verification-runs/{run_id}/work-item` 可以只读查看下一次采样时间、领取状态、尝试次数和有限错误，但不暴露租约令牌。测试使用可注入服务器时钟推进 60 秒窗口，不降低业务规则，也不让测试真实等待。

## Incident 领域状态机

`opsswarm.incidents` 是独立于 FastAPI、SQLAlchemy 和 Agent 框架的纯领域模块。调用方提交当前 Incident、业务动作、期望版本、发生时间和操作者，状态机返回新的不可变 Incident 快照与一条审计事件。它不直接写数据库；后续持久化层必须在同一事务内保存两者。

这种边界让状态迁移规则可以快速进行纯单元测试，也保证未来无论由 REST API、Worker 还是 LangGraph 节点触发，都会经过同一套确定性规则。

## PostgreSQL 持久化

控制面业务库使用 PostgreSQL 17。SQLAlchemy 异步仓储负责把领域行为映射为数据库事务，Alembic 是表结构的唯一迁移入口。完全重复告警由 `(source, external_event_id)` 唯一约束保证幂等；相同指纹的未关闭 Incident 由部分唯一索引保证并发下最多一个。

启动本地业务库并执行迁移：

```powershell
docker compose up -d --wait control-db
uv run alembic upgrade head
uv run alembic current
```

默认连接只服务本地开发，可通过 `OPSSWARM_DATABASE_URL` 覆盖。日常停止使用 `docker compose stop control-db`，下次执行 `docker compose start control-db` 复用原容器。`docker compose down` 会删除容器但保留命名卷；不要执行会删除数据卷的 `docker compose down -v`。

## 运行控制面 API

需要单独调试后端时，可在业务库迁移完成后启动只绑定本机的控制面：

```powershell
uv run python -m opsswarm.control_plane.main --host 127.0.0.1 --port 8000
```

接口文档位于 `http://127.0.0.1:8000/docs`。接收一条演练告警：

```powershell
$alertBody = @{
  source = "fault-simulator"
  external_event_id = "alert-001"
  fingerprint = "order-service:http-500"
  service_name = "order-service"
  severity = "p1"
  title = "订单接口大量返回 500"
  payload = @{ status_code = 500 }
  occurred_at = "2026-01-01T08:00:00+08:00"
} | ConvertTo-Json -Depth 4

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/alerts `
  -ContentType "application/json" `
  -Body $alertBody
```

当前控制面没有登录和权限系统，仅允许本地演练使用。请求中的 `actor_id` 只是审计声明，不是可信身份凭据。

## 运行 Incident 作战室

前端开发时可先启动控制面 API，再在另一个 PowerShell 窗口启动 Vite：

```powershell
cd web
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173/` 查看 Incident 作战室，访问 `http://127.0.0.1:5173/evaluations` 查看只读评测面板。Vite 将同源 `/api` 请求转发到 `127.0.0.1:8000`，因此本地开发不需要放宽 FastAPI CORS。默认 Compose 则使用非 root Nginx 托管生产构建，并通过相同的同源 `/api` 路径访问控制面。

## 可重放事故时间线

SSE 接口先补发 `incident_events` 中游标大于 `Last-Event-ID` 的事件，再以短轮询等待新事件。每个事件的 `id` 就是 PostgreSQL 全局事件号；心跳不带 `id`，不会误推进客户端游标。首次连接省略请求头即可读取完整事故时间线：

```powershell
curl.exe --no-buffer http://127.0.0.1:8000/api/v1/incidents/{incident_id}/stream
```

断线后使用浏览器自动携带的游标，或手工验证续传：

```powershell
curl.exe --no-buffer -H "Last-Event-ID: 12" `
  http://127.0.0.1:8000/api/v1/incidents/{incident_id}/stream
```

轮询间隔、心跳间隔、批量大小和重试提示分别通过 `OPSSWARM_SSE_POLL_INTERVAL_SECONDS`、`OPSSWARM_SSE_HEARTBEAT_INTERVAL_SECONDS`、`OPSSWARM_SSE_BATCH_SIZE` 和 `OPSSWARM_SSE_RETRY_MILLISECONDS` 配置，并有安全范围校验。

## Incident 关闭与告警复发

工程师只能关闭已经通过恢复验证的 `resolved` Incident，并必须携带当前乐观锁版本和审计原因：

```powershell
$closeBody = @{
  expected_version = 12
  actor_id = "engineer-001"
  reason = "恢复窗口已经通过，确认关闭"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/incidents/{incident_id}/actions/close `
  -ContentType "application/json" `
  -Body $closeBody
```

正式关闭前收到同一指纹的新告警时，系统保存告警后自动把 `resolved` 事故迁回 `investigating`；多个并发告警最多产生一次状态迁移。正式关闭后，`closed` 历史事故保持终态，同一指纹的新外部事件创建新的 Incident。完全相同的来源事件号仍返回原接收结果，不受事故是否关闭影响。

## 确定性事故复盘报告

关闭后使用最终 Incident 版本冻结一次报告；请求体不能提交或改写报告内容：

```powershell
$reportBody = @{
  expected_incident_version = 13
  actor_id = "engineer-001"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/incidents/{incident_id}/reports `
  -ContentType "application/json" `
  -Body $reportBody

Invoke-RestMethod `
  http://127.0.0.1:8000/api/v1/incidents/{incident_id}/reports/current
```

首次生成返回 `201`，重复请求返回 `200` 和同一快照。报告只包含 PostgreSQL 已持久化事实，保存 `incident-report.v1` 结构版本与 SHA-256；读取时会重新校验内容完整性。

在事故作战室选择 `closed` Incident 后，页面会自动读取报告；尚未生成时只要求填写本地审计操作者。报告区域提供“打印 / 保存 PDF”，打印样式只保留冻结文档，不包含导航、列表或操作控件。

报告生成后，可以通过报告 ID 登记并读取结构化改进行动项；创建和完成都由人工接口提交，Agent 没有组织任务写权限：

```powershell
$actionItemBody = @{
  idempotency_key = "follow-up-001"
  title = "补充 Redis 故障演练"
  description = "把依赖恢复和验证步骤加入每月演练"
  owner_id = "sre-001"
  due_at = "2026-10-01T08:00:00Z"
  actor_id = "engineer-001"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/incident-reports/{report_id}/action-items `
  -ContentType "application/json" `
  -Body $actionItemBody
```

相同幂等键和内容可安全重试；完成时必须携带当前 `version` 与完成说明。报告本身不会因行动项推进而改写。作战室在报告生成后自动显示“复盘改进行动项”面板，浏览器草稿保存稳定幂等键，并在版本冲突后同步服务端最新状态而不自动重试。

主导航“改进治理”集中读取跨事故未完成行动项，可按负责人精确筛选并只看服务器判定的逾期项。治理页面保持只读，完成任务必须返回来源事故复盘，核对报告上下文和当前版本后提交。

## 运行故障模拟切片

### 可选持久发布演练与CAS

第123步增加[持久发布演练状态](docs/architecture/123-lab-release-state-cas.md)，默认关闭。开启前先执行正式迁移，再启动order-service；以下命令仅供本地演练，不会由开发助手自动升级用户数据库：

```powershell
docker compose --env-file .env.example up -d --wait control-db
docker compose --env-file .env.example run --rm control-migrate
$env:OPSSWARM_LAB_RELEASE_ENABLED = "true"
docker compose --env-file .env.example up -d --build --wait order-service
$release = Invoke-RestMethod http://127.0.0.1:18001/__admin/simulated-release
$releaseCommand = @{
  expected_version = $release.version
  expected_revision = $release.revision
  target_version = "2026.01.01-rc1"
  idempotency_key = "demo-release-" + [guid]::NewGuid().ToString()
} | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:18001/__admin/simulated-release -ContentType 'application/json' -Body $releaseCommand
```

固定演练版本为stable（健康）和2026.01.01-rc1（模拟500）。CAS同时比较版本和修订号，精确重放保留历史command并另返current/effect_current；旧开关也写入同一持久状态，重启不清零，存储故障503。恢复stable仍须先GET并使用当前修订和新命令键。旧默认内存模式保留；此接口是隔离演练管理入口，不消费候选审批，不改变真实runtime-metadata或签名清单，也不代表候选已经执行。

第125步增加[发布命令只读回执](docs/architecture/125-release-receipt-reconciliation.md)：使用原命令的idempotency_key查询`GET /__admin/simulated-release/commands/{idempotency_key}`。存在返回200与完整历史命令/current/effect_current；不存在返回带演练来源的404封套，查询不会产生写入。它不能证明尚在途的请求已经停止，不能据404自动重发；effect_current只表示模拟版本与修订仍匹配，不是恢复健康证明。

第126步增加[远端受限派发](docs/architecture/126-bounded-release-dispatch.md)：`POST /__admin/simulated-release/dispatches`注册operation_id、完整rc1→stable CAS和不超过30秒的not_before/expires_at；随后GET/PUT/DELETE同一路径下的`/{operation_id}`分别查询、执行原命令和取消未应用操作。窗口不可续期，数据库时钟在锁后及提交约束校验，结果丢失使用原ID/键只读查询。此处是隔离演练管理协议，控制面通过第127步独立Worker接入；生产环境不得使用。

第127步增加[控制面可信执行任务与Worker](docs/architecture/127-candidate-release-worker.md)。对精确124请求调用`POST .../release-execution-request/execution-job`只入队，正文包含request_id、request_content_sha256、expected_incident_version、idempotency_key和reason；GET该路径及`/commands`查询状态、最多3个尝试、原始审计及执行结果。当前配置必须有同版本executor凭据，旧authorization不会自动成为长期委派。运行独立Worker：

```powershell
uv run python -m opsswarm.execution.release_jobs.main --once
# 持续处理时省略 --once；仍须显式配置本地数据库、演练地址和操作者认证。
```

Worker首次写前持久标记，远端窗口不超过原准备/凭据/租约期限，注册后再核验才执行。响应未知或重领只GET，不自动换键/重发；unknown终态当前需人工调查，无自动重开接口。结果表示模拟操作历史，尚未客观验证恢复，也未结算父运行。新迁移仅在隔离测试库验收；本片未自动启用Compose Worker服务或升级用户数据库。

### 候选执行后的业务恢复验证

第128步增加[独立只读恢复验证](docs/architecture/128-candidate-recovery-verification.md)。启动Worker后自动发现第127步终态执行，为每个原执行保存唯一验证任务及不可变结果：

```powershell
uv run python -m opsswarm.verification.candidate.main --once
uv run python -m opsswarm.verification.candidate.main
```

使用`OPSSWARM_VERIFIER_FAULT_LAB_BASE_URL`配置本地订单服务（默认`http://127.0.0.1:18001`），单请求总超时由`OPSSWARM_VERIFIER_REQUEST_TIMEOUT_SECONDS`控制，默认3秒、最多10秒。Worker只GET固定订单业务探针；每次尝试最多3份、间隔至少2秒，最多3次尝试。采样窗口从原执行完成开始120秒且不超过原运行期限，重启不会续期。版本已回退但订单仍500会记为未恢复，来源不可用记为source_failed；unknown不会自动重发或被标成健康。

可信Bearer可查询`GET .../release-execution-request/execution-job/recovery-verification`，包含任务、尝试、样本和结果；未被Worker发现时为空视图。此接口不触发探针，没有新增HTTP写命令。恢复结果不修改父运行或Incident，不能将有限演练样本表述为生产恢复或因果证明；默认Compose尚未自动启用此Worker。

### 候选恢复结论与父运行收尾

第129步增加[父运行持久收尾](docs/architecture/129-candidate-parent-settlement.md)。独立进程发现已完成的候选恢复验证，完整重建原事实、确认当前依据后，在同一事务保存唯一收尾记录、进度事件并将父run置为`settled`：

```powershell
uv run python -m opsswarm.investigation.settlement.main --once
uv run python -m opsswarm.investigation.settlement.main
```

可信Bearer可调用`GET /api/v1/investigations/{run_id}/candidate-settlement`读取收尾事实；GET不会触发推进，没有新增HTTP写入口。`settled`表示本次候选阶段已收尾，独立`disposition`区分`recovery_verified`、`reinvestigation_required`和`human_resolution_required`。原128五类结果与原因保留，不能把来源失败/执行未知改称恢复；后续意图尚未实际启动。

旧交接、handed_off尝试、执行和恢复样本保持，旧历史仍可查询而当前写资格失效。第129步收尾本身维持原`mitigating`状态、版本和事件，并继续占用`settled`活动槽位；后续事故迁移和成功槽位释放见下方第130步，失败回流仍待后续交付。此进程只有数据库事务，不调用模型/网络或恢复LangGraph，也不自动添加默认Compose服务。

### 候选处置结果与事故收敛

第130步提供[独立事故收敛](docs/architecture/130-candidate-incident-convergence.md)，消费第129步收尾事实，GET只查询，独立进程负责状态推进：

```powershell
uv run python -m opsswarm.incidents.convergence.main --once
uv run python -m opsswarm.incidents.convergence.main
```

可信Bearer可读取`GET /api/v1/investigations/{run_id}/incident-convergence`。新鲜恢复证据通过`mitigating → verifying → resolved`两跳，并在同事务释放活动槽位；验证失败则`mitigating → verifying → investigating`，继续保留槽位，供后续受原预算约束的回流处理。未知、未执行、数据源失败、效果替代、过期或采样后的新告警都不会自动宣布恢复。

时效取原128窗口和最早观测后120秒的较早者，数据库提交时再次检查，不能靠收尾时间续期。原128/129记录、父run时刻、预算和图不变；未来人工close或同指纹告警重开后历史仍可读。此进程不重新采样/执行或创建调查，不自动关闭事故，也不启动默认Compose服务。

### 本地运行

```powershell
uv sync
uv run uvicorn opsswarm.fault_lab.main:app --host 127.0.0.1 --port 8000
```

在另一个 PowerShell 窗口验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/orders/order-001

Invoke-RestMethod `
  -Method Put `
  -Uri http://127.0.0.1:8000/__admin/faults/release-500 `
  -ContentType 'application/json' `
  -Body '{"enabled":true}'

Invoke-WebRequest http://127.0.0.1:8000/orders/order-001 -SkipHttpErrorCheck

Invoke-RestMethod `
  -Method Put `
  -Uri http://127.0.0.1:8000/__admin/configuration/database-pool `
  -ContentType 'application/json' `
  -Body '{"value":5}'

Invoke-WebRequest http://127.0.0.1:8000/orders/order-001 -SkipHttpErrorCheck

Invoke-RestMethod `
  -Method Put `
  -Uri http://127.0.0.1:8000/__admin/configuration/database-pool `
  -ContentType 'application/json' `
  -Body '{"value":50}'
```

### Docker Compose 运行

```powershell
docker compose up -d --build --wait
docker compose ps -a
.\scripts\verify-mvp.ps1
docker compose stop
```

运行前需确保 Docker Desktop 的 Linux Engine 已启动。完整页面位于 `http://127.0.0.1:5173`，接口文档位于 `http://127.0.0.1:8000/docs`。`control-migrate` 会在数据库健康后执行 Alembic 并正常退出；`control-api`、冻结/现场调查 Worker、日志和指标 MCP、`verification-worker` 作为独立容器运行。两个 MCP 均不发布宿主端口，只在隔离 `telemetry` 网络分别读取 Loki 和 Prometheus；现场 Worker 通过固定地址调用它们。`lab-redis` 是真实缓存依赖，`redis-proxy` 提供固定目标的受控断连，`prometheus` 每 2 秒抓取订单服务并保留一天时间序列，`alloy` 从项目专用只读日志卷采集到保留 24 小时的 `loki`。日常停止使用 `docker compose stop`，下次用 `docker compose start` 复用项目容器；只有需要重建 Compose 资源时才使用 `docker compose down`，不要使用会删除 PostgreSQL、Prometheus、Loki 和 Alloy 位置数据命名卷的 `docker compose down -v`。版本故障默认关闭，连接池容量默认恢复为安全值 `50`，Redis 代理默认可用；三种故障状态都在对应服务重启后回到安全默认值。管理与查询接口只绑定 localhost 和 Compose 演练网络，不具备生产鉴权能力。

需要演示完整事故闭环时执行以下命令。它会使用 `.env` 中配置的真实模型并产生小额费用，没有显式开关时脚本会拒绝运行：

```powershell
.\scripts\demo-mvp-flow.ps1 -AllowModelCall
```

## 测试与检查

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest

cd web
npm run typecheck
npm test
npm run build
```

集成测试会通过 Testcontainers 启动隔离的 PostgreSQL 17，运行前需要启动 Docker Desktop。测试容器结束后自动清理，不复用本地开发库。

## 安全声明

第一版只用于本地 Docker Compose 故障演练，不可直接部署到生产环境。LLM 不拥有任意 Shell 或生产写权限；所有实际操作必须由确定性白名单执行器完成，高风险动作必须经过人工审批。
