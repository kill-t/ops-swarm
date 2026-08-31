# 39 - 持久变更记录模拟器与只读调查数据源

## 目标

补齐日志、指标之外的第三类真实现场数据：让本地变更模拟服务持久记录发布、连接池配置和 Redis 维护事实，并让 Agent 的 `list_recent_changes` 工具只读返回规范化变更 Evidence。本切片不调用模型、不需要 API Key，也不切换持久调查 Worker。

## 设计与边界

- 变更服务只记录已经发生的事实，不执行发布、配置修改、Redis 重启或故障开关。
- 三种请求使用精确 Pydantic 联合类型，来源、服务和服务器时间不能由调用方覆盖。
- `(source, external_change_id)` 是数据库幂等边界；相同内容重放返回原记录，内容漂移返回 `409`。
- 记录只追加，不提供更新和删除接口；重启服务后从项目 PostgreSQL 卷恢复。
- Agent 只传 `order-service` 与 1～24 小时窗口，不能传 URL、SQL、任意时间范围或数量上限。
- 现场三数据源仍与冻结评测隔离，避免把不断变化的现场证据误算为固定 Top-1。

## 实际改动

- 新增 `change_records` 领域契约、SHA-256 内容摘要、有限错误和 PostgreSQL 仓储。
- 新增 Alembic `0019_change_records`，包含类型、来源、服务、JSON、时间、摘要和幂等约束；存在记录时拒绝有损降级。
- 新增独立 FastAPI `change-service`，提供健康检查、幂等管理写入和有界只读查询。
- Compose 新增常驻安全容器，绑定 `127.0.0.1:18002` 并复用既有业务 PostgreSQL 卷。
- 新增 `ChangeApiOnlyInvestigationDataSource`、环境配置和无模型 `change_smoke` 命令。
- 对响应大小、生成时间、查询窗口、截断、重复记录、HTTP 错误和精确内容执行失败关闭校验。
- 新增模型、仓储、并发、API、迁移和 LangChain 真实工具测试。

## 测试方法与结果

定向验收覆盖：

```powershell
uv run pytest `
  tests/unit/change_records `
  tests/unit/investigation/test_changes.py `
  tests/integration/test_change_record_store.py `
  tests/api/test_change_record_api.py `
  tests/integration/test_migrations.py
```

结果：36 个测试全部通过。覆盖三种白名单契约、时区与顺序、稳定摘要、PostgreSQL 并发幂等、内容漂移冲突、结果截断、API `201/200/409`、拒绝回填时间、Agent 固定查询、URL 白名单、坏响应与失败关闭，以及 Alembic 元数据一致、降级和升级。

真实 Compose 冒烟：

```powershell
docker compose up -d --build --wait
uv run python -m opsswarm.investigation.change_smoke --lookback-hours 6
docker compose restart change-service
```

结果：`change-service` 健康运行在 `127.0.0.1:18002`；发布、连接池配置和 Redis 维护各写入一条，Agent 工具返回三条 `change-simulator` Evidence。重启服务后仍读取到原来三条外部变更号，且 `truncated=false`，证明记录由 PostgreSQL 持久化。

全量验收：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
docker compose config --quiet
git diff --check
```

结果：Ruff 检查通过，299 个文件格式检查通过，mypy 检查 130 个源码文件通过，443 个测试全部通过；Compose 配置与 Git 空白检查通过。

## 限制与下一步

- 管理写接口没有生产鉴权，仅绑定 localhost 与 Compose 私网，不能用于生产变更平台。
- 当前本地业务进程共用数据库账号；生产化前必须为迁移、API、Worker 与变更服务拆分最小权限角色。
- 当前只支持一个服务与三种故障演练所需变更，不包含 Git commit、操作者、审批单或多环境目录。
- 变更事实与实际故障开关刻意解耦，演练脚本需要按顺序分别记录变更和注入故障。
- 持久调查 Worker 仍使用冻结证据；下一步实现统一现场数据源和显式 Worker 切换。
- 后续第 45 步接入 GitHub 时已复用只读协议和规范化 Evidence，没有扩大模型权限。

## 面试表达

我把变更记录建成独立、追加式、可恢复的外部事实源，而不是查询时临时伪造。PostgreSQL 唯一约束负责并发幂等，内容摘要发现同一外部编号的内容漂移。Agent 只能查询固定服务和时间窗；适配器对 URL、数量、时间、联合类型、重复和截断做失败关闭。变更事实通常早于 Incident，因此不建立 Incident 外键，而是在调查时按服务和时间关联，规范化后再进入具体运行的 Evidence Board。
