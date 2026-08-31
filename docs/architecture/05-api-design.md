# OpsSwarm 控制面 API 设计

## 1. 目标与边界

控制面 API 是调用方与 Incident 应用服务之间的传输边界。它负责 HTTP 输入校验、响应序列化、状态码和错误映射，不负责重复实现状态机、告警并发规则或数据库事务。

当前 API 版本为 `/api/v1`，仅用于 localhost 和 Docker Compose 私有网络。MVP 没有 JWT、租户和生产权限模型，因此任何 `actor_id` 都只是审计声明，不能作为人工审批或生产授权依据。

OpenTelemetry 显式开启时，除被排除的健康检查外，控制面响应返回 `X-Trace-ID`。它只用于在本地 Jaeger 中定位请求，不作为幂等键、认证信息或业务标识；追踪关闭时该响应头不存在。

## 2. 当前接口

| 方法与路径 | 成功语义 | 主要失败 |
| --- | --- | --- |
| `GET /health` | 返回控制面进程存活状态 | 当前不检查数据库就绪性 |
| `POST /api/v1/alerts` | 新外部告警 `201`；完全重复 `200` | 请求或领域校验 `422` |
| `GET /api/v1/incidents` | 状态筛选和倒序游标分页 | 查询参数错误 `422` |
| `GET /api/v1/incidents/{id}` | 返回当前 Incident 快照 | 不存在 `404` |
| `GET /api/v1/incidents/{id}/workroom` | 返回最近调查到恢复验证的同语句只读摘要 | 不存在 `404` |
| `POST /api/v1/incidents/{id}/actions/close` | 使用当前版本关闭 `resolved` 事故 | 不存在 `404`；版本或状态冲突 `409` |
| `POST /api/v1/incidents/{id}/actions/start-investigation` | 原子迁移事故并创建调查运行与任务 | 不存在 `404`；版本或迁移冲突 `409` |
| `GET /api/v1/investigations/{run_id}` | 返回运行状态、结果、指标和错误 | 不存在 `404` |
| `GET /api/v1/investigations/{run_id}/attempts` | 按序返回 Worker 尝试、耗时、Token、费用与错误 | 调查不存在 `404` |
| `GET /api/v1/investigations/{run_id}/evidence-board` | 返回结论、规范化证据、专业报告和引用 | 不存在 `404` |
| `POST /api/v1/investigations/{run_id}/action-plans` | 首次生成草案 `201`；幂等重复 `200` | 不存在 `404`；调查或假设未就绪 `409` |
| `GET /api/v1/investigations/{run_id}/action-plans/current` | 返回最高计划版本 | 调查或计划不存在 `404` |
| `POST /api/v1/action-plans/{plan_id}/approval-requests` | 首次提交 `201`；同计划幂等重复 `200` | 计划不存在 `404`；版本、状态或完整性冲突 `409` |
| `GET /api/v1/approval-requests/{request_id}` | 重验计划内容后返回限时审批请求 | 不存在 `404`；内容漂移 `409` |
| `POST /api/v1/approval-requests/{request_id}/actions/approve` | 首次批准 `decision_applied=true`；同向重试为 `false` | 不存在 `404`；过期、相反决定或完整性冲突 `409` |
| `POST /api/v1/approval-requests/{request_id}/actions/reject` | 拒绝计划并把 Incident 返回调查 | 不存在 `404`；过期、相反决定或完整性冲突 `409` |
| `POST /api/v1/action-steps/{step_id}/executions` | 首次执行 `201`；幂等终态 `200`；同键运行中 `202` | 不存在 `404`；授权、版本、幂等或租约冲突 `409`；已审计适配器失败 `502` |
| `GET /api/v1/operation-executions/{execution_id}` | 返回持久执行状态、有限结果或失败摘要 | 不存在 `404` |
| `POST /api/v1/operation-executions/{execution_id}/verification-runs` | 首次创建 `201`；同成功操作幂等读取 `200` | 不存在 `404`；执行、Incident 或规则不满足 `409` |
| `GET /api/v1/verification-runs/{run_id}` | 返回冻结规则、进度、终态和全部样本 | 不存在 `404` |
| `POST /api/v1/verification-runs/{run_id}/samples` | 首次样本 `201`；同键样本 `200` | 不存在 `404`；运行已结束或 Incident 冲突 `409` |
| `GET /api/v1/incidents/{id}/events` | 按全局事件游标正序读取时间线 | 不存在 `404`；查询参数错误 `422` |
| `GET /api/v1/incidents/{id}/stream` | 补发游标后的事件并持续保持 SSE 连接 | 不存在 `404`；非法 `Last-Event-ID` 为 `422` |
| `GET /api/v1/evaluation-runs` | 按全局运行序号倒序返回聚合历史 | 查询参数错误 `422` |
| `GET /api/v1/evaluation-runs/{run_id}` | 返回数据集版本和完整逐场景报告 | 不存在 `404` |

SSE 的 `id` 直接使用 `incident_events.id`，`event` 使用持久化事件类型，`data` 是对应 `IncidentEventResponse` 的单行 JSON。首次连接默认从游标 0 补发；客户端重连时通过 `Last-Event-ID` 请求严格大于该游标的事件。服务端空闲时发送不含 `id` 的注释心跳，因此不会推进恢复位置。事件至少一次投递，客户端应按 `id` 幂等处理。

接口在发送流响应头前确认 Incident 存在。连接建立后数据库异常会终止连接，不伪造业务事件；客户端按 `retry` 提示重连并从原游标继续。每轮查询使用短生命周期数据库会话，轮询等待期间不占用连接。当前实现采用短轮询，PostgreSQL 是唯一事实来源，不维护进程内订阅队列。

关闭请求只接受 `expected_version`、`actor_id` 和 `reason`。服务端固定执行 `close` 领域动作，调用方不能指定目标状态；只有 `resolved` 可以进入 `closed`。重复或过期页面使用旧版本关闭时返回 `409`，不会覆盖其他操作者已经提交的状态。

告警接收先按 `(source, external_event_id)` 判断完全重复，再锁定同指纹未关闭 Incident。命中 `resolved` 时，在同一事务内保存新告警与 `incident.alert_attached`，随后执行 `reopen` 并追加状态事件；响应返回更新后的 `investigating` 快照。命中 `closed` 不会重新打开历史事故，而是创建新 Incident。完全重复告警始终返回原 Incident，即使它后来已经关闭。

开始调查请求支持可选 `mode`：`single_agent` 或 `multi_agent`，默认 `single_agent`；也支持 `data_source_mode`：`frozen` 或 `live`，默认 `frozen`。非法值返回结构化 `422`。两种选择都会进入调查运行响应并持久化，客户端不能在重试或恢复阶段改写。多 Agent 响应中的 `investigation.graph_thread_id` 使用业务运行 ID 确定性生成；该字段用于恢复关联，不是外部调用方可以指定的输入。

### 2.1 Evidence Board 读取语义

Evidence Board 接口只读取业务数据库，不读取 LangGraph checkpoint。`queued` 或重试中的运行返回当前状态、空结论和空集合；成功运行返回按稳定业务标识排序的证据、专业报告和 `hypotheses`。单 Agent 可以有证据但没有专业报告，多 Agent 同时返回报告及其 `evidence_ids`。主根因假设返回 `supported`、`contested` 或 `insufficient_evidence` 状态，并包含规则版本和逐证据关系。每条证据暴露 SHA-256 `content_hash`，用于调用方检测内容漂移，不把它宣传为外部来源签名。

作战室聚合接口只返回值班工程师需要快速浏览的摘要，不替代上述明细接口。它使用一条 PostgreSQL 查询选择 Incident 最近调查、该调查最高版本计划、计划唯一审批、计划最近执行及其唯一验证运行，并同时计算证据、报告、步骤和样本数量。调查摘要显式返回 `data_source_mode`，页面显示“冻结基准”或“真实现场”，避免把现场运行误认为评测结果。不存在的后续阶段返回 `null`，原始日志、原始证据和动作参数不会进入聚合响应。具体一致性与选择规则见 [Incident 作战室只读聚合投影](21-incident-workroom-projection.md)。

### 2.2 处置计划生成语义

计划 POST 没有可由客户端指定的动作参数；服务端只从已提交 Evidence Board 提取白名单参数。调查必须为 `succeeded`、主假设必须为 `supported`；回滚模板要求明确的变更前后值，Redis 恢复模板要求健康指标明确为不可用。相同假设版本重复或并发调用返回同一计划，调用方通过 `created` 区分首次创建。

计划生成响应是 `draft`，包含风险策略版本、逐步骤风险理由和恢复判据。创建或读取计划本身不会迁移 Incident，也不执行动作。当前 API 没有身份认证，只能在 localhost/Compose 私网演练。

### 2.3 审批请求语义

审批 POST 携带 `expected_incident_version`、`actor_id` 和 `reason`。首次提交在同一事务中创建 `pending` 请求、把计划迁移到 `awaiting_approval`，并把 Incident 迁移到同名状态及追加时间线事件。同计划并发或网络重试返回原请求，不重复迁移。

请求绑定计划 ID、版本和内容 SHA-256，固定 30 分钟有效。GET 会重新校验计划策略与内容哈希。批准和拒绝是不可变终态；同方向重试返回原决定，相反方向返回冲突。决定到达时已经过期会先持久化 `expired` 并把 Incident 返回调查，再响应 `409`。

批准把 Incident 迁移到 `mitigating`，但接口不调用执行器或环境工具。`actor_id` 只供本地审计，不能证明调用者身份。

### 2.4 受控执行语义

执行 POST 只接受 8～100 字符的 `idempotency_key` 和 `expected_incident_version`。路径中的 `step_id` 决定要执行的数据库步骤；客户端不能提交动作类型、目标、参数、命令或 URL。服务端重新校验当前批准计划与内容哈希后，先创建带 15 秒租约的 `running` 记录，再调用最长 3 秒的固定本地适配器。

首次创建并成功完成返回 `201`、`created=true`、`execution_performed=true`。相同键的终态重试返回 `200` 且不重复调用适配器；有效租约内的并发同键请求返回 `202` 和同一 `running` 记录。进程中断后，相同键只能在租约过期后接管并增加 `attempt_count`。响应不暴露内部 `lease_token`。

适配器失败会先保存 `failed`、错误摘要和 Incident 回调查，再返回 `502`。成功保存有限结果并把 Incident 推进到 `verifying`，但不代表业务已经恢复。调用方必须通过执行记录 GET 和后续恢复验证读取事实，不能把 HTTP 请求已返回等同于事故已解决。

### 2.5 逐次恢复验证语义

验证运行 POST 只接受 `expected_incident_version`。目标操作必须为 `succeeded`，Incident 必须处于 `verifying`；服务端从操作关联步骤读取并冻结验证规则，并在同一事务创建唯一 `verification_work_item`。同操作重复请求返回原运行，即使调用方仍携带旧版本，也不会创建第二个验证或调度任务。

样本 POST 只接受 8～100 字符的 `idempotency_key`，不接受观测值、状态码、URL 或时间。服务端固定探针完成只读请求后，以服务器 UTC 时间追加样本。同键已存在时返回 `200`、`created=false`、`probe_performed=false`；并发竞争可能已经执行只读探针，但数据库仍只保存一个同键样本，响应通过 `probe_performed` 如实表达本次是否发起了探测。

常驻验证 Worker 默认通过 PostgreSQL 租约自动触发样本；手动样本 POST 仍保留给本地诊断和幂等验证。一个 5xx 数值样本或探针错误会形成 `failed` 运行并把 Incident 返回 `investigating`，这属于成功持久化的业务判定，因此样本接口仍返回 `201`，不会伪装成传输层失败。健康样本只有在数量和 60 秒窗口都满足时才使运行 `passed`、Incident `resolved`。GET 返回全部样本，当前计划最多要求 100 个成功样本；后续高流量场景再增加分页。

`GET /api/v1/verification-runs/{run_id}/work-item` 返回调度状态、下次样本序号、到期时间、Worker 标识、租约到期时间、尝试次数和有限错误。响应永不包含 `lease_token`；该接口只读，客户端不能提前调度、延长租约或改写失败。

## 3. 告警幂等 HTTP 语义

`POST /api/v1/alerts` 的状态码描述“外部告警是否首次接收”，不描述“是否创建新 Incident”：

| 场景 | HTTP 状态 | `deduplicated` | `created_incident` |
| --- | --- | --- | --- |
| 新事件且没有活动同指纹事故 | `201` | `false` | `true` |
| 新事件聚合到活动 Incident | `201` | `false` | `false` |
| 相同来源事件号重复上报 | `200` | `true` | `false` |

`ingested_at` 由服务器 UTC 时钟生成，客户端不能提交或覆盖。`occurred_at` 来自告警源，但必须携带时区。原始 `payload` 必须是 JSON 对象，序列化后最多 64 KiB。

## 4. Incident 表示

Incident 响应同时提供：

- `id`：跨系统关联使用的 UUID。
- `display_number`：数据库生成的递增展示序号。
- `reference`：面向工程师的 `INC-000001` 格式标识。
- `status` 与 `version`：提交动作时进行状态判断和乐观锁控制。
- UTC 生命周期时间：发生、解决、关闭、创建和更新时间。

客户端不能通过通用更新接口直接提交目标状态。每个状态变化必须调用语义化动作接口，并携带 `expected_version`。

## 5. 游标分页

Incident 列表按 `display_number DESC` 排序。响应中的 `next_before_display_number` 非空时，下一页把它作为同名查询参数。新 Incident 插入不会造成基于 offset 的重复或漏读。

事件列表按全局 `incident_events.id ASC` 排序。响应中的 `next_after_cursor` 非空时，下一页把它作为 `after_cursor`。该语义与后续 SSE `Last-Event-ID` 重放保持一致。

评测运行列表按 `sequence DESC` 排序，响应中的 `next_before_sequence` 用作下一页同名参数。Incident 与评测列表默认返回 50 条，事件默认返回 100 条，三类接口单次请求上限均为 100；仓储多读取一条判断是否还有下一页。

评测列表与详情都返回不可变 `experiment`、`repetition_index` 和单/多 Agent `telemetry`。真实模型尝试列表与详情还返回调用前预算、`running`、`succeeded` 或 `failed` 状态、模式级结果或稳定失败分类。其中模型成本使用十进制定点字符串传输，避免 JSON 浮点改变审计数值；控制面仍没有启动或改写评测的 HTTP 接口。

## 6. 错误契约

领域和请求错误使用稳定结构：

```json
{
  "error_code": "incident_version_conflict",
  "message": "Incident 版本冲突：期望版本 1，实际版本 2",
  "details": {
    "expected_version": 1,
    "actual_version": 2
  }
}
```

| 错误码 | HTTP 状态 | 含义 |
| --- | --- | --- |
| `request_validation_error` | `422` | 路径、查询或请求体不符合 API 模型 |
| `incident_validation_error` | `422` | 输入通过传输校验但违反领域约束 |
| `incident_not_found` | `404` | Incident 不存在 |
| `incident_version_conflict` | `409` | `expected_version` 已过期 |
| `invalid_incident_transition` | `409` | 当前状态不允许执行指定动作 |
| `investigation_run_not_found` | `404` | 调查运行不存在 |
| `evaluation_run_not_found` | `404` | 评测运行不存在 |
| `evaluation_attempt_not_found` | `404` | 真实模型评测尝试不存在 |
| `action_plan_not_found` | `404` | 调查存在但尚无计划 |
| `action_plan_not_ready` | `409` | 调查未成功或缺少已提交假设 |
| `hypothesis_not_supported` | `409` | 根因假设尚未通过质疑 |
| `action_plan_capability_unavailable` | `409` | 调查建议没有当前可创建的确定性计划模板 |
| `unsupported_planning_root_cause` | `409` | 当前没有该根因的白名单计划模板 |
| `unsafe_rollback_target` | `409` | 证据无法确定安全回滚目标 |
| `unsafe_configuration_rollback_target` | `409` | 证据无法确定白名单配置的安全前后值 |
| `unsafe_dependency_restore_target` | `409` | 证据没有明确证明 Redis 当前不可用 |
| `approval_action_plan_not_found` | `404` | 要提交审批的计划不存在 |
| `approval_request_not_found` | `404` | 审批请求不存在 |
| `action_plan_not_current` | `409` | 目标计划已被更新版本替代 |
| `action_plan_not_submittable` | `409` | 计划状态不允许新建审批请求 |
| `action_plan_does_not_require_approval` | `409` | 目标计划不需要人工审批 |
| `action_plan_not_executable` | `409` | 计划动作尚无确定性执行器，不能提交审批 |
| `approval_requester_must_be_human` | `409` | 当前切片只接受人工类型请求人 |
| `approval_plan_integrity_violation` | `409` | 计划当前内容与提交审批时的哈希不一致 |
| `approval_decider_must_be_human` | `409` | 批准或拒绝不是由人工类型提交 |
| `approval_request_already_decided` | `409` | 请求已经进入与当前命令相反的终态 |
| `approval_request_expired` | `409` | 请求已收敛为过期，人工决定未应用 |
| `action_plan_not_decidable` | `409` | pending 请求关联的计划不在待审批状态 |
| `operation_step_not_found` | `404` | 处置步骤不存在 |
| `operation_execution_not_found` | `404` | 执行记录不存在 |
| `operation_not_authorized` | `409` | Incident、当前计划或精确批准不满足执行条件 |
| `operation_idempotency_conflict` | `409` | 幂等键已绑定另一个步骤 |
| `operation_step_already_running` | `409` | 步骤已有使用其他幂等键的运行记录 |
| `operation_step_already_succeeded` | `409` | 步骤已经使用其他幂等键成功执行 |
| `execution_lease_lost` | `409` | 当前执行实例的租约已过期或被接管 |
| `operation_execution_failed` | `502` | 受控适配器失败，失败事实和 Incident 回流已落库 |
| `verification_run_not_found` | `404` | 恢复验证运行不存在 |
| `verification_work_item_not_found` | `404` | 恢复验证调度任务不存在 |
| `operation_execution_not_verifiable` | `409` | 操作未成功、Incident 不在验证状态或目标不受支持 |
| `verification_run_completed` | `409` | 已结束运行不能使用新的幂等键追加样本 |
| `unsupported_verification_rule` | `409` | 当前验证器不支持步骤冻结的信号或比较符 |

数据库连接失败和未处理错误不转换为伪造的业务成功响应，也不向客户端暴露 SQL、连接字符串或堆栈。追踪仅记录稳定异常类型，不记录可能包含供应商响应、密钥或模型内容的异常消息；结构化业务日志仍沿用独立的有限字段策略。

## 7. 生命周期与测试

生产应用工厂在 FastAPI lifespan 中创建数据库引擎和会话工厂，关闭时显式释放连接池。测试使用同一应用生命周期和 HTTPX2 ASGI transport，并连接 Testcontainers 启动的真实 PostgreSQL，不替换仓储或数据库语义。

OpenAPI 文档由 Pydantic 请求/响应模型生成。未知请求字段统一拒绝，避免客户端把拼写错误的字段误认为已被系统采用。
