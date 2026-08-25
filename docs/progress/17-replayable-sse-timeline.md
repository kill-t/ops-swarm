# 17 - 可重放事故时间线 SSE

## 目标

把 PostgreSQL 中已提交的 `incident_events` 作为实时作战室数据源，支持首次补发、持续推送和基于 `Last-Event-ID` 的断线恢复，不依赖 API 进程内状态。

## 设计与边界

- 数据库全局事件号直接作为 SSE `id`，客户端只接收严格大于恢复游标的记录。
- 默认每批 100 条；有积压时连续补发，无事件时每秒短轮询，15 秒发送一次无游标心跳。
- 每次查询使用短会话，等待期间不占连接；数据库异常通过断流和重连恢复。
- SSE 只传事实，写命令仍使用 REST；当前无鉴权，只绑定 localhost。
- Windows 正式入口显式使用 Selector 事件循环，解决 Psycopg 与 Uvicorn 默认 Proactor 循环不兼容的问题。

## 实际改动

1. 新增 SSE 配置、事件编码和数据库重放服务，支持批量补发、心跳、断连检测及安全范围校验。
2. 新增 `GET /api/v1/incidents/{id}/stream`，解析 `Last-Event-ID` 并设置禁止代理缓冲和缓存的响应头。
3. 连接前确认 Incident 存在；不存在返回结构化 `404`，非法游标返回 `422`。
4. 新增控制面正式启动入口参数，限制本机监听，并在 Windows 使用 Psycopg 支持的事件循环。
5. 增加编码与恢复单元测试、真实 PostgreSQL 重放测试、API 错误和 OpenAPI 测试。
6. 更新 README、系统架构、技术栈和 API 事实来源；本切片复用现有表，不新增迁移。

## 测试方法与结果

定向检查：

```powershell
uv run ruff check src/opsswarm/control_plane tests/unit/control_plane tests/integration/test_sse_stream.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py
uv run mypy src
uv run pytest tests/unit/control_plane tests/integration/test_sse_stream.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py
```

真实运行使用项目入口启动控制面，在本地业务库创建两条同指纹告警。首次 SSE 连接收到 `id: 1` 的 `incident.created`；第二次连接携带 `Last-Event-ID: 1`，只收到 `id: 2` 的 `incident.alert_attached`。这同时验证了 Windows 异步数据库入口和 PostgreSQL 续传路径。

完整验收：

```powershell
uv run pytest
uv run pytest --collect-only -q -m integration
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run alembic check
uv run alembic current
docker compose config --quiet
git diff --check
```

结果：全量 196 个测试通过，其中收集到 62 个真实 PostgreSQL 集成测试；177 个受 Ruff 管理的文件格式检查通过，Ruff、87 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。业务库仍为 `0010_verification_runs (head)`，因为本切片复用事件表而没有新增迁移。项目专属 `control-db` 与 `order-service` 容器均保持健康。

## 限制与下一步

- 当前没有 Vue 作战室页面消费 SSE。
- 短轮询最多约 1 秒延迟，尚未使用 PostgreSQL 通知优化唤醒。
- 当前无连接数限制、鉴权和租户隔离，仅用于本地演练。
- 下一切片补齐 Incident 关闭和关闭后新告警复发语义，再把完整状态时间线接入 UI。

## 面试表达

我用 PostgreSQL 只追加事件表实现了可恢复 SSE：事件主键就是传输游标，重连只查询更大的 ID。API 重启不会丢进度，心跳不推进游标，客户端用 ID 去重。轮询不持有连接，数据库异常时让客户端从原游标重连。真实进程测试证明 Last-Event-ID 只补发断线后的事件，而不是依赖内存广播。
