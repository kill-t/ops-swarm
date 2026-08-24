# OpsSwarm

当线上系统报警后，OpsSwarm 自动拉取日志、指标和最近变更，组织多个 Agent 并行调查，输出带证据的根因判断和修复方案；经工程师审批后执行受控操作，并验证系统是否恢复。

## 当前状态

项目已交付五个小切片：确定性版本 500 故障模拟、纯领域 Incident 状态机、PostgreSQL 事故持久化、FastAPI 控制面和单 Agent 调查基线。当前可以通过 HTTP 管理事故，也可以让可注入模型使用三类只读工具调查冻结故障，输出带真实证据引用的结构化根因并计算离线指标；调查 Worker、LangGraph、SSE 和前端尚未接入。

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
- [开发进度与复盘](docs/progress/README.md)

## 实施路线

1. 项目章程、架构边界和最小目录。
2. 确定性故障模拟服务与 Incident 状态机，不接 LLM。
3. 单 Agent 调查基线。
4. 协调 Agent 与日志、指标、变更并行调查节点。
5. Evidence Board、质疑节点和结构化处置计划。
6. 人工审批、白名单执行器和幂等操作。
7. 恢复验证、失败恢复与时间线 UI。
8. 评测集以及单 Agent、多 Agent 对比。
9. 接入真实 Prometheus、Docker、GitHub 或 MCP 工具。

阶段 1 使用“新版本触发大量 500”场景逐步打通完整链路：当前已完成确定性故障注入、Incident 领域状态机、告警幂等、PostgreSQL 持久化、不可变时间线、FastAPI 控制面和单 Agent 调查基线。下一阶段把调查运行接入持久任务与独立 Worker，确保任务和结果可恢复，再进入 LangGraph 多 Agent 编排。

## 单 Agent 调查基线

`opsswarm.investigation` 使用 LangChain 1.x 组装单 Agent 工具循环，但只暴露日志、指标和变更三个有界只读工具。结论必须符合 `InvestigationConclusion`，并且只能引用本轮工具实际返回的证据 ID。固定 `release_500` 场景用于离线计算根因 Top-1、证据召回、建议准确率、安全违规率和工具调用数。

默认测试使用假模型和冻结工具结果，不需要模型密钥或网络：

```powershell
uv run pytest tests/unit/investigation -q
```

真实 OpenAI 兼容模型仅通过 `LLM_API_KEY`、`LLM_MODEL`、可选 `LLM_BASE_URL` 和 `LLM_TEMPERATURE` 显式配置。当前没有自动调用真实模型的命令，避免开发或持续集成误产生费用。

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

默认连接只服务本地开发，可通过 `OPSSWARM_DATABASE_URL` 覆盖。停止容器使用 `docker compose down`；命名卷会保留数据库内容，用于验证重启恢复。

## 运行控制面 API

业务库迁移完成后启动只绑定本机的控制面：

```powershell
uv run uvicorn opsswarm.control_plane.main:app --host 127.0.0.1 --port 8000
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
```

### Docker Compose 运行

```powershell
docker compose up --build -d --wait
Invoke-RestMethod http://127.0.0.1:18001/health
docker compose down
```

运行前需确保 Docker Desktop 的 Linux Engine 已启动。故障开关默认关闭，服务重启后重置。管理接口只绑定 localhost 和 Compose 演练网络，不具备生产鉴权能力。

## 测试与检查

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

集成测试会通过 Testcontainers 启动隔离的 PostgreSQL 17，运行前需要启动 Docker Desktop。测试容器结束后自动清理，不复用本地开发库。

## 安全声明

第一版只用于本地 Docker Compose 故障演练，不可直接部署到生产环境。LLM 不拥有任意 Shell 或生产写权限；所有实际操作必须由确定性白名单执行器完成，高风险动作必须经过人工审批。
