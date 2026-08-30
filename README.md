# OpsSwarm

当线上系统报警后，OpsSwarm 自动拉取日志、指标和最近变更，组织多个 Agent 并行调查，输出带证据的根因判断和修复方案；经工程师审批后执行受控操作，并验证系统是否恢复。

## 当前状态

项目已交付三十七个小切片：确定性版本 500 故障模拟、纯领域 Incident 状态机、PostgreSQL 事故持久化、FastAPI 控制面、单 Agent 调查基线、持久调查 Worker、LangGraph 并行调查基线、PostgreSQL checkpoint 恢复、多 Agent Worker 路由、Evidence Board、根因假设质疑、版本化处置计划、精确版本审批请求、审批决定与过期收敛、确定性白名单执行器、逐次恢复验证、可重放 SSE 时间线、工程师关闭与告警复发、Vue 3 Incident 作战室、作战室处置链路聚合展示、三故障版本化调查评测集、评测结果 PostgreSQL 持久化与只读查询、只读评测面板、评测实验身份与运行遥测、真实模型尝试生命周期、DeepSeek 兼容适配、真实模型三场景批量对照、处置建议契约与评测集 v2、确定性处置能力映射、数据库连接池配置回滚计划模板、数据库连接池确定性故障演练、配置回滚受控执行与恢复验证、持久恢复验证调度 Worker、Redis 不可用确定性处置闭环、真实 Redis 与受控故障代理、Prometheus 指标与恢复验证，以及 Prometheus 只读调查数据源。当前页面可以读取 Incident、实时事件和调查至验证六阶段摘要，也可以从持久评测历史下钻到实验配置、重复序号、单/多 Agent 指标、Token、成本、失败计数和逐场景证据；暂不提供浏览器状态变更命令。

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
- [开发进度与复盘](docs/progress/README.md)

## 实施路线

1. 项目章程、架构边界和最小目录。
2. 确定性故障模拟服务与 Incident 状态机，不接 LLM。
3. 单 Agent 调查基线。
4. 持久调查任务、租约重试和独立 Worker。
5. 协调 Agent 与日志、指标、变更并行调查节点。
6. Evidence Board、质疑节点和结构化处置计划。
7. 人工审批、白名单执行器和幂等操作。
8. 恢复验证、失败恢复与时间线 UI。
9. 评测集以及单 Agent、多 Agent 对比。
10. 继续接入 Loki、OpenTelemetry、GitHub 或 MCP 工具。

阶段 1 的版本回归、数据库连接池配置错误和 Redis 不可用三个场景都已具备本地确定性故障、计划、审批、白名单执行和恢复验证链路。Redis 场景通过固定 TCP 故障代理访问独立真实 Redis 容器；三种恢复信号由项目内 Prometheus 真实抓取，验证 Worker 只执行固定查询。评测集快照、运行、逐场景结果和聚合指标已经持久化到 PostgreSQL，并通过只读接口提供给评测面板。所有环境读写仍只允许本地故障演练环境。

## 单 Agent 与多 Agent 任务模式

开始调查接口的 `mode` 可取 `single_agent` 或 `multi_agent`，省略时保持单 Agent 默认值。模式不是模型决定的，而是作为业务运行和持久任务的一部分写入 PostgreSQL。

```json
{
  "expected_version": 1,
  "actor_id": "engineer-001",
  "reason": "使用并行调查模式",
  "mode": "multi_agent"
}
```

Worker 校验运行模式与任务类型一致：单 Agent 调用 LangChain 基线；多 Agent 查询对应线程是否已有 checkpoint，存在则恢复，不存在才启动新图。

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

`opsswarm.investigation` 使用 LangChain 1.x 组装单 Agent 工具循环，但只暴露日志、指标和变更三个有界只读工具。结论必须符合 `InvestigationConclusion`，并且只能引用本轮工具实际返回的证据 ID。固定 `release_500` 场景用于离线计算根因 Top-1、证据召回、建议准确率、安全违规率和工具调用数。

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

当前 Worker 每次运行至多处理一个任务，需要显式配置真实模型：

```powershell
$env:LLM_API_KEY = "本地模型服务密钥"
$env:LLM_MODEL = "模型名称"
$env:LLM_BASE_URL = "可选 OpenAI 兼容地址"
$env:LLM_THINKING_MODE = "provider_default、enabled 或 disabled"
$env:LLM_STRUCTURED_OUTPUT_METHOD = "json_schema、json_mode 或 function_calling"
uv run python -m opsswarm.worker.main
```

当前两种持久处理器都仍读取冻结 `release_500` 证据，主要用于公平验证任务、Agent 调用、checkpoint 和结果落库链路。真实 Prometheus 指标已接入独立只读调查数据源，但在 Loki 和变更适配器完成前不会与冻结证据混用，也不会把现场结果对照固定答案计算准确率。

不调用模型、直接验证 Agent 指标工具与真实 Prometheus 的只读链路：

```powershell
uv run python -m opsswarm.investigation.prometheus_smoke --lookback-minutes 15
```

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

业务库迁移完成后启动只绑定本机的控制面：

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

先启动控制面 API，再在另一个 PowerShell 窗口启动前端开发服务：

```powershell
cd web
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173/` 查看 Incident 作战室，访问 `http://127.0.0.1:5173/evaluations` 查看只读评测面板。Vite 将同源 `/api` 请求转发到 `127.0.0.1:8000`，因此本地开发不需要放宽 FastAPI CORS。生产构建使用 `npm run build`；当前尚未制作前端静态资源容器。

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

## 运行故障模拟切片

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
Invoke-RestMethod http://127.0.0.1:18001/health
docker compose stop
```

运行前需确保 Docker Desktop 的 Linux Engine 已启动。`control-migrate` 会在数据库健康后执行 Alembic 并正常退出，`verification-worker` 会作为常驻容器自动领取到期验证任务；`lab-redis` 是真实缓存依赖，`redis-proxy` 提供固定目标的受控断连，`prometheus` 每 2 秒抓取订单服务并保留一天时间序列。日常停止使用 `docker compose stop`，下次用 `docker compose start` 复用项目容器；只有需要重建 Compose 资源时才使用 `docker compose down`，不要使用会删除 PostgreSQL 与 Prometheus 命名卷的 `docker compose down -v`。版本故障默认关闭，连接池容量默认恢复为安全值 `50`，Redis 代理默认可用；三种故障状态都在对应服务重启后回到安全默认值。管理与查询接口只绑定 localhost 和 Compose 演练网络，不具备生产鉴权能力。

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
