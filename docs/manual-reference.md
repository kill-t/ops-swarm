# 手工运行与历史接口参考

本页保留原首页中的手工命令、兼容路径和分阶段接口说明，命令均从仓库根目录执行。文中的阶段状态保留当时语义；当前能力、默认运行入口与验收缺口以[项目首页](../README.md)、[交接入口](PROJECT_STATE.md)和[本地发布范围](architecture/local-multi-agent-release-scope.md)为准。

推荐通过[本地发布运行手册](local-release.md)装配当前多 Agent 主线；下列独立命令用于按需调试、兼容与历史复现。

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

Compose 仅将动态开关传入冻结池。动态演练使用 `release-500-worker-v1`，把固定模拟材料的最新观察锚定事故开始时间，按完整内容计算稳定快照；限 order-service 发布故障，不是真实现场。历史 v3 图不会被新 Worker 领取，其他冻结来源的旧 graph-v15 也不能混入新快照。终端交接后父运行是 `await_human` 或 `await_candidate`，父尝试 `handed_off`、工作项释放租约；没有旧根因成功结果，也不授予执行资格。`GET /api/v1/investigations/{run_id}/terminal-handoff` 可查精确路由来源。详见 [终端交接设计](architecture/111-dynamic-worker-handoff.md)。

第 112 步提供 [可信人工补充与取消](architecture/112-trusted-human-wait-commands.md)。先配置第 104 步的有效工程师 Bearer 凭据；即使控制面使用 `legacy_local`，以下入口也要求可信认证。以已有 await_human 运行 ID 为例，令牌由本地环境变量读取：

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

第 113 步增加 [可信人工重评输入准备](architecture/113-human-input-preparation.md)。在至少一次补充后，可以按最新等待版本创建独立准备任务。Worker 实际生成完整待重评 ModelInput、来源清单和原预算诊断；必要材料过大或预算过期会明确 blocked，不静默裁剪人工约束。准备阶段没有调用模型，ready 也不是调用预留或恢复授权。

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

第 114 步增加 [实际人工重评](architecture/114-human-input-reassessment.md)。工程师必须另行提交显式重评命令；准备就绪本身不会调用模型。准备 GET 响应提供 `artifact_sha256` 完整工件散列，命令须精确绑定该散列与最新等待版本：

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

第 115 步增加 [重评后续路由](architecture/115-human-reassessment-routing.md)。成功意见同事务保存唯一、不可变的确定性意图；历史第 114 步成功意见由同一重评 Worker 有界补齐，不额外调用模型。查询使用可信 Bearer：

```powershell
Invoke-RestMethod -Uri "$reviewUrl/$($reviewJob.job.id)/route" -Headers $waitHeaders
```

响应 `record.decision.route` 区分 `await_human`、`follow_up`、`candidate_evaluation`，固定 `dispatch_status=not_dispatched`。原轮次/任务容量、反证、未解决人工约束、原时限及共享预算都参与判断；完整约束和预算快照可追溯。`availability` 区分 pending/current/stale，`current_blocking_reason_codes` 单独诊断当前预算/约束，不改写历史决定。current 只表示输入仍适用，既不代表已分派，也不授权执行。补充/取消后旧路由保留为 stale；原 run/work/attempt 和 checkpoint 保持，候选评估由下述第 116 步显式消费，预算追加仍待后续。

第 116 步增加 [受限候选评估](architecture/116-candidate-evaluation.md)。原终端交接或当前重评候选路由均可作为来源，工程师须另行提交命令；`source_sha256` 绑定**完整来源记录**散列，而非旧 `source_sha256` 字段。原终端来源为 `source_kind=terminal_handoff`、`expected_wait_version=null`；重评来源为 `human_route` 和精确等待版本。其余字段为 `action=evaluate_candidate`、`source_id`、`idempotency_key`、`reason`。

```powershell
$candidateUrl = "http://127.0.0.1:8000/api/v1/investigations/$runId/candidate-evaluations"
# 正文以待评估的完整来源及其规范 JSON 散列构造，使用工程师 Bearer。
$candidateJob = Invoke-RestMethod -Method Post -Uri $candidateUrl -Headers $waitHeaders -ContentType "application/json" -Body $candidateBody
uv run python -m opsswarm.investigation.candidate.main --once
Invoke-RestMethod -Uri "$candidateUrl/$($candidateJob.job.id)" -Headers $waitHeaders
Invoke-RestMethod -Uri "$candidateUrl/commands" -Headers $waitHeaders
```

独立 Worker 实际消费完整调查材料、当前审查、人工前缀及固定三项能力目录。模型只提能力与证据，参数、风险与验证规则由候选明确引用的结构化证据推导；不支持或缺参时等待人工，安全候选仅形成 `planning_review`，始终 `execution_eligible=false`。它不创建 ActionPlan/审批，不把开放假设转成既有根因。来源变化、事故关闭、过期或失租后拒绝发布；新阶段真实尝试与旧父来源分别记录，继续共享原预算，成功调用可复用。Compose 可显式启动 `candidate-evaluation` profile 的 `candidate-evaluation-worker`；模型配置要求同原运行，SDK 重试关闭，需使用最新镜像与迁移。未升级用户业务库。

第117步增加 [候选草案可信准入](architecture/117-candidate-plan-admission.md)。可信工程师可从当前成功且为 `planning_review` 的候选生成独立单步骤 `ActionPlan` 草案；服务重算原证据、参数和风险，不接受客户端计划正文，不发送模型请求或追加预算。

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

第123步增加[持久发布演练状态](architecture/123-lab-release-state-cas.md)，默认关闭。开启前先执行正式迁移，再启动order-service；以下命令仅供本地演练，不会由开发助手自动升级用户数据库：

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

第125步增加[发布命令只读回执](architecture/125-release-receipt-reconciliation.md)：使用原命令的idempotency_key查询`GET /__admin/simulated-release/commands/{idempotency_key}`。存在返回200与完整历史命令/current/effect_current；不存在返回带演练来源的404封套，查询不会产生写入。它不能证明尚在途的请求已经停止，不能据404自动重发；effect_current只表示模拟版本与修订仍匹配，不是恢复健康证明。

第126步增加[远端受限派发](architecture/126-bounded-release-dispatch.md)：`POST /__admin/simulated-release/dispatches`注册operation_id、完整rc1→stable CAS和不超过30秒的not_before/expires_at；随后GET/PUT/DELETE同一路径下的`/{operation_id}`分别查询、执行原命令和取消未应用操作。窗口不可续期，数据库时钟在锁后及提交约束校验，结果丢失使用原ID/键只读查询。此处是隔离演练管理协议，控制面通过第127步独立Worker接入；生产环境不得使用。

第127步增加[控制面可信执行任务与Worker](architecture/127-candidate-release-worker.md)。对精确124请求调用`POST .../release-execution-request/execution-job`只入队，正文包含request_id、request_content_sha256、expected_incident_version、idempotency_key和reason；GET该路径及`/commands`查询状态、最多3个尝试、原始审计及执行结果。当前配置必须有同版本executor凭据，旧authorization不会自动成为长期委派。运行独立Worker：

```powershell
uv run python -m opsswarm.execution.release_jobs.main --once
# 持续处理时省略 --once；仍须显式配置本地数据库、演练地址和操作者认证。
```

Worker首次写前持久标记，远端窗口不超过原准备/凭据/租约期限，注册后再核验才执行。响应未知或重领只GET，不自动换键/重发；unknown终态当前需人工调查，无自动重开接口。结果表示模拟操作历史，尚未客观验证恢复，也未结算父运行。新迁移仅在隔离测试库验收；本片未自动启用Compose Worker服务或升级用户数据库。

### 候选执行后的业务恢复验证

第128步增加[独立只读恢复验证](architecture/128-candidate-recovery-verification.md)。启动Worker后自动发现第127步终态执行，为每个原执行保存唯一验证任务及不可变结果：

```powershell
uv run python -m opsswarm.verification.candidate.main --once
uv run python -m opsswarm.verification.candidate.main
```

使用`OPSSWARM_VERIFIER_FAULT_LAB_BASE_URL`配置本地订单服务（默认`http://127.0.0.1:18001`），单请求总超时由`OPSSWARM_VERIFIER_REQUEST_TIMEOUT_SECONDS`控制，默认3秒、最多10秒。Worker只GET固定订单业务探针；每次尝试最多3份、间隔至少2秒，最多3次尝试。采样窗口从原执行完成开始120秒且不超过原运行期限，重启不会续期。版本已回退但订单仍500会记为未恢复，来源不可用记为source_failed；unknown不会自动重发或被标成健康。

可信Bearer可查询`GET .../release-execution-request/execution-job/recovery-verification`，包含任务、尝试、样本和结果；未被Worker发现时为空视图。此接口不触发探针，没有新增HTTP写命令。恢复结果不修改父运行或Incident，不能将有限演练样本表述为生产恢复或因果证明；默认Compose尚未自动启用此Worker。

### 候选恢复结论与父运行收尾

第129步增加[父运行持久收尾](architecture/129-candidate-parent-settlement.md)。独立进程发现已完成的候选恢复验证，完整重建原事实、确认当前依据后，在同一事务保存唯一收尾记录、进度事件并将父run置为`settled`：

```powershell
uv run python -m opsswarm.investigation.settlement.main --once
uv run python -m opsswarm.investigation.settlement.main
```

可信Bearer可调用`GET /api/v1/investigations/{run_id}/candidate-settlement`读取收尾事实；GET不会触发推进，没有新增HTTP写入口。`settled`表示本次候选阶段已收尾，独立`disposition`区分`recovery_verified`、`reinvestigation_required`和`human_resolution_required`。原128五类结果与原因保留，不能把来源失败/执行未知改称恢复；后续意图尚未实际启动。

旧交接、handed_off尝试、执行和恢复样本保持，旧历史仍可查询而当前写资格失效。第129步收尾本身维持原`mitigating`状态、版本和事件，并继续占用`settled`活动槽位；后续事故迁移和成功槽位释放见下方第130步，失败回流仍待后续交付。此进程只有数据库事务，不调用模型/网络或恢复LangGraph，也不自动添加默认Compose服务。

### 候选处置结果与事故收敛

第130步提供[独立事故收敛](architecture/130-candidate-incident-convergence.md)，消费第129步收尾事实，GET只查询，独立进程负责状态推进：

```powershell
uv run python -m opsswarm.incidents.convergence.main --once
uv run python -m opsswarm.incidents.convergence.main
```

可信Bearer可读取`GET /api/v1/investigations/{run_id}/incident-convergence`。新鲜恢复证据通过`mitigating → verifying → resolved`两跳，并在同事务释放活动槽位；验证失败则`mitigating → verifying → investigating`，继续保留槽位，供后续受原预算约束的回流处理。未知、未执行、数据源失败、效果替代、过期或采样后的新告警都不会自动宣布恢复。

时效取原128窗口和最早观测后120秒的较早者，数据库提交时再次检查，不能靠收尾时间续期。原128/129记录、父run时刻、预算和图不变；未来人工close或同指纹告警重开后历史仍可读。此进程不重新采样/执行或创建调查，不自动关闭事故，也不启动默认Compose服务。

### 验证失败的回流准入

第131步提供[有界回流准入](architecture/131-bounded-reinvestigation-admission.md)。可信engineer向`POST /api/v1/investigations/{run_id}/reinvestigation-admission`提交`action=request_reinvestigation`、130的`convergence_sha256`、`expected_incident_version`、`idempotency_key`和`reason`。GET同路径查看不可变请求及当前适用性，GET `/commands`按游标读取逐请求业务审计。

只有130明确验证失败、事故版本仍匹配且没有后续告警时才可新建。每根运行最多一次准入；相同主体与完整命令精确重放返回原请求，换键或换主体不会增加次数。请求绑定原账本和原总截止，未知模型用量继续占用，至少保留一次完整协调调用容量；GET或重放不续期。

准入的`dispatch_status=not_dispatched`，原settled运行继续持槽。本片未创建新的调查或预算预留，实际分派将以独立阶段在原账本重新检查和预留；不能把准入当成处置授权或已开始重新调查。

### 独立回流协调 Worker

第132步提供[回流协调阶段](architecture/132-reinvestigation-coordination.md)，自动发现第131步唯一准入，冻结完整材料，使用独立尝试/租约在原账本预留后调用结构化模型，保存下一轮待分派建议。模型配置及定价必须匹配根运行创建时冻结的策略。

```powershell
uv run python -m opsswarm.investigation.reinvestigation_coordination.main --once
uv run python -m opsswarm.investigation.reinvestigation_coordination.main --continuous
```

仅显式启动，不加入默认Compose服务。可信Bearer/read可查询`GET /api/v1/investigations/{run_id}/reinvestigation-coordination`和`/input`；旧调用的model-input、interaction和context-manifest接口也支持新调用，新材料在legacy_local模式下仍要求可信Bearer。

原轮次/任务容量、次数、Token、估算费用和总截止继续累计。完整上下文超过根运行原单次输入上限时明确blocked且不调用；旧默认24,000额度可能不足，需在创建根运行时按场景配置足够容量，不能修改既有运行或复制余额。成功调用在结果保存前崩溃可跨尝试复用；失联调用记unknown并保守占用。新告警、来源变化或超期后不能发布建议。

建议始终`not_dispatched`，尚未新增专业Round/Task或执行新调查、图恢复及处置。131原请求和128～130历史保持，原settled运行继续持槽。

### 回流专业任务分派

第133步提供[确定性分派消费者](architecture/133-reinvestigation-dispatch.md)，将132成功建议原子编译为独立业务轮次和pending专业任务，保留原运行、原轮次/任务容量、累计预算与总截止。服务范围由系统注入，取证窗口限制在失败验证结果之后，不复用旧冻结快照。此阶段只写业务队列，不调用模型或遥测。

```powershell
uv run python -m opsswarm.investigation.reinvestigation_dispatch.main --once
uv run python -m opsswarm.investigation.reinvestigation_dispatch.main --continuous
```

仅显式启动，默认Compose不启用。可信Bearer/read可读取`GET /api/v1/investigations/{run_id}/reinvestigation-dispatch`，包括不可变record/tasks及当前适用性。来源失效或容量不足保存blocked且无任务；有在途调用时暂缓消费，待结算后再处理。重复或并发消费不会追加任务，事务失败则整轮回滚。原132建议仍为历史not_dispatched，133队列单独表示实际分派进展。

独立表用于保持旧最高轮交接和132冻结输入可读，逻辑轮次仍受原上限约束；没有新run或新额度。第133步止于分派；第134步接通真实专业尝试、重新取证和调用预留，综合与审查仍待后续切片。

### 独立回流专业执行

第134步提供[专业执行 Worker](architecture/134-reinvestigation-execution.md)。每个133任务建立独立投影和真实租约尝试，在原账本预留工具调用后重新采集快照，冻结完整上下文并预留模型调用，保存结构化专业报告。133原始队列继续是不可变pending分派事实，实际进度由134接口提供。

```powershell
uv run python -m opsswarm.investigation.reinvestigation_execution.main --once
uv run python -m opsswarm.investigation.reinvestigation_execution.main --continuous
```

先配置现有本地日志/指标MCP及变更来源、与原运行冻结profile一致的模型参数；默认Compose不启动本Worker。日志/指标通过固定只读MCP异步端口，变更通过受限线程读取本地窗口接口；失败不回退冻结材料。同根最多3个未失租任务，所有尝试共享原次数/Token/费用和总截止，不重新开始计时。

旧MCP只支持整分钟，实际窗口会向内取整并保存requested/effective与调整原因；不足一分钟的日志/指标任务明确blocked且零调用，不扩大到失败验证前。原请求未观测区间进入模型输入，不能据此声称完整覆盖。成功取证与模型调用可跨尝试复用，失联调用保留unknown占用，新告警/超期/迟到响应不能发布报告。

可信Bearer/read可查询`GET /api/v1/investigations/{run_id}/reinvestigation-execution`的状态、真实尝试、新快照、输入和专业结果。新调用的model-input、context-manifest、interaction接口同样要求可信读取。这里只完成专业分析，综合/独立审查和后续规划审批仍待交付，旧批准不能授权新动作。

### 独立回流综合

第135步提供[综合 Worker](architecture/135-reinvestigation-synthesis.md)。同轮所有专业均成功后，完整原材料、新证据、专业报告和未观测窗口进入冻结输入；有专业失败则明确阻断，未完成则等待。综合只引用本次新证据，在原账本预留`synthesize_reinvestigation`模型调用，结果固定`pending_review`，独立审查后续交付。

```powershell
uv run python -m opsswarm.investigation.reinvestigation_synthesis.main --once
uv run python -m opsswarm.investigation.reinvestigation_synthesis.main --continuous
```

沿用原运行冻结profile和环境模型配置，SDK重试关闭，不加入默认Compose。最多3次真实尝试，成功调用可以跨尝试复用；unknown保留原占用，不重置原预算或期限。必要上下文超容量时停止调用。可信Bearer/read可查询`GET /api/v1/investigations/{run_id}/reinvestigation-synthesis`和`/input`，以及新调用的三类材料接口。HTTP查询不运行模型。

### 独立回流审查

第136步提供[独立审查 Worker](architecture/136-reinvestigation-review.md)，读取135成功综合的精确提案、完整原材料/人工约束和新专业证据，使用独立角色与提示。在原账本预留`review_reinvestigation`，接受的支持只限于原提案已引用的支持集合，反证限定同轮新证据。原135记录保留`pending_review`历史，136独立保存`recorded`意见；137独立消费者生成后续路由。

```powershell
uv run python -m opsswarm.investigation.reinvestigation_review.main --once
uv run python -m opsswarm.investigation.reinvestigation_review.main --continuous
```

沿用原运行冻结profile和模型配置，SDK重试关闭；最多3次真实尝试，成功调用可跨尝试复用，unknown保留原占用，不延长原期限。默认Compose不启动本Worker，没有业务工具。可信Bearer/read查询`GET /api/v1/investigations/{run_id}/reinvestigation-review`及`/input`，新调用三类材料接口同样需要可信读取。HTTP不运行模型，意见及补查建议不创建任务、计划、审批或执行资格。

### 回流审查确定性路由

第137步提供[纯事务路由消费者](architecture/137-reinvestigation-routing.md)。绑定135综合与136独立意见的精确摘要、同源完整材料、原轮次/任务数、原预算账本及事故事件游标，按既有规则保存`candidate_evaluation`、`follow_up`或`await_human`的`not_dispatched`意图。保留人工约束、未解问题和请求/实际观测窗口；未知用量不清零，后续模型及工具仍须原子预留。新告警或总期限失效只改变当前诊断，不改写历史意图；旧批准不覆盖新依据。

```powershell
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_routing.main --once
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_routing.main --continuous
```

可信Bearer/read查询`GET /api/v1/investigations/{run_id}/reinvestigation-routing`，返回`record`、`availability`和当前`blocking_reason_codes`；未消费时为`pending`，GET不会生成路由。不加入默认Compose，不调用模型；实际后续候选、规划、审批和处置另行接入。验收见[第137步记录](progress/137-reinvestigation-routing.md)。

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
