# OpsSwarm 控制面 API 设计

## 1. 目标与边界

控制面 API 是调用方与 Incident 应用服务之间的传输边界。它负责 HTTP 输入校验、响应序列化、状态码和错误映射，不负责重复实现状态机、告警并发规则或数据库事务。

当前 API 版本为 `/api/v1`，仅用于 localhost 和 Docker Compose 私有网络。MVP 没有 JWT、租户和生产权限模型，因此任何 `actor_id` 都只是审计声明，不能作为人工审批或生产授权依据。

## 2. 当前接口

| 方法与路径 | 成功语义 | 主要失败 |
| --- | --- | --- |
| `GET /health` | 返回控制面进程存活状态 | 当前不检查数据库就绪性 |
| `POST /api/v1/alerts` | 新外部告警 `201`；完全重复 `200` | 请求或领域校验 `422` |
| `GET /api/v1/incidents` | 状态筛选和倒序游标分页 | 查询参数错误 `422` |
| `GET /api/v1/incidents/{id}` | 返回当前 Incident 快照 | 不存在 `404` |
| `POST /api/v1/incidents/{id}/actions/start-investigation` | 原子迁移事故并创建调查运行与任务 | 不存在 `404`；版本或迁移冲突 `409` |
| `GET /api/v1/investigations/{run_id}` | 返回运行状态、结果、指标和错误 | 不存在 `404` |
| `GET /api/v1/investigations/{run_id}/evidence-board` | 返回结论、规范化证据、专业报告和引用 | 不存在 `404` |
| `POST /api/v1/investigations/{run_id}/action-plans` | 首次生成草案 `201`；幂等重复 `200` | 不存在 `404`；调查或假设未就绪 `409` |
| `GET /api/v1/investigations/{run_id}/action-plans/current` | 返回最高计划版本 | 调查或计划不存在 `404` |
| `GET /api/v1/incidents/{id}/events` | 按全局事件游标正序读取时间线 | 不存在 `404`；查询参数错误 `422` |

`GET /api/v1/incidents/{id}/stream` 将在 SSE 切片实现，不提前维护进程内订阅状态。

开始调查请求支持可选 `mode`：`single_agent` 或 `multi_agent`，默认 `single_agent` 以保持现有客户端兼容。非法模式返回结构化 `422`。多 Agent 响应中的 `investigation.graph_thread_id` 使用业务运行 ID 确定性生成；该字段用于恢复关联，不是外部调用方可以指定的输入。

### 2.1 Evidence Board 读取语义

Evidence Board 接口只读取业务数据库，不读取 LangGraph checkpoint。`queued` 或重试中的运行返回当前状态、空结论和空集合；成功运行返回按稳定业务标识排序的证据、专业报告和 `hypotheses`。单 Agent 可以有证据但没有专业报告，多 Agent 同时返回报告及其 `evidence_ids`。主根因假设返回 `supported`、`contested` 或 `insufficient_evidence` 状态，并包含规则版本和逐证据关系。每条证据暴露 SHA-256 `content_hash`，用于调用方检测内容漂移，不把它宣传为外部来源签名。

### 2.2 处置计划生成语义

计划 POST 没有可由客户端指定的动作参数；服务端只从已提交 Evidence Board 提取白名单参数。调查必须为 `succeeded`、主假设必须为 `supported`，且证据必须包含明确的发布前后版本。相同假设版本重复或并发调用返回同一计划，调用方通过 `created` 区分首次创建。

计划响应是 `draft`，包含风险策略版本、逐步骤风险理由和恢复判据。创建或读取计划不会迁移 Incident，不创建审批，也不执行动作。当前 API 没有身份认证，只能在 localhost/Compose 私网演练。

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

两类接口默认最多返回 50 和 100 条，单次请求上限均为 100；API 多读取一条判断是否还有下一页。

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
| `action_plan_not_found` | `404` | 调查存在但尚无计划 |
| `action_plan_not_ready` | `409` | 调查未成功或缺少已提交假设 |
| `hypothesis_not_supported` | `409` | 根因假设尚未通过质疑 |
| `unsupported_planning_root_cause` | `409` | 当前没有该根因的白名单计划模板 |
| `unsafe_rollback_target` | `409` | 证据无法确定安全回滚目标 |

数据库连接失败和未处理错误不转换为伪造的业务成功响应，也不向客户端暴露 SQL、连接字符串或堆栈。后续可观测性切片会增加请求追踪标识和结构化日志关联。

## 7. 生命周期与测试

生产应用工厂在 FastAPI lifespan 中创建数据库引擎和会话工厂，关闭时显式释放连接池。测试使用同一应用生命周期和 HTTPX2 ASGI transport，并连接 Testcontainers 启动的真实 PostgreSQL，不替换仓储或数据库语义。

OpenAPI 文档由 Pydantic 请求/响应模型生成。未知请求字段统一拒绝，避免客户端把拼写错误的字段误认为已被系统采用。
