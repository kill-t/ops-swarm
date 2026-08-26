# 18 - Incident 关闭与告警复发

## 目标

让工程师能够在恢复验证通过后显式关闭事故，并让同一指纹告警在关闭前后得到不同、可审计且并发安全的处理。

## 设计与边界

- 关闭命令固定执行 `close`，只接受当前版本、操作者和原因，不能由客户端指定任意目标状态。
- `resolved` 关闭前复发时自动回到 `investigating`；`closed` 后复发创建新 Incident。
- 完全重复的来源事件号始终返回原接收结果，避免网络重试制造新事故。
- 活动 Incident 行锁保证并发复发最多执行一次 `reopen`，不同外部告警仍全部保存。
- LLM、LangChain 和 LangGraph 不参与关闭或复发判断。

## 实际改动

1. 告警聚合查询增加 PostgreSQL 行锁，并在命中 `resolved` 时原子追加告警、时间线和 `reopen` 迁移。
2. 新增 `CloseIncidentRequest` 和 `POST /api/v1/incidents/{id}/actions/close`。
3. 复用现有领域状态机、乐观锁、`closed_at`、事件表和活动指纹部分唯一索引，没有新增迁移。
4. 增加关闭成功、提前关闭、旧版本冲突、关闭前复发和关闭后新建事故的 API 测试。
5. 增加真实 PostgreSQL 复发事务测试，以及 8 条并发告警只触发一次重新调查的测试。
6. 更新 README、系统架构、数据库、API 和上一切片边界文档。

## 测试方法与结果

定向检查：

```powershell
uv run pytest tests/unit/incidents tests/integration/test_incident_store.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py
uv run ruff check src tests/integration/test_incident_store.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py
uv run mypy src
```

真实 PostgreSQL 定向测试已经验证关闭和复发行为。完整验收：

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

结果：全量 201 个测试通过，其中收集到 67 个真实 PostgreSQL 集成测试；179 个受 Ruff 管理的文件格式检查通过，Ruff、87 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。业务库仍为 `0010_verification_runs (head)`，因为本切片复用已有约束和索引。项目专属 `control-db` 与 `order-service` 容器均保持健康。

## 限制与下一步

- 当前没有可信登录身份，关闭接口仍只能本地演练。
- 关闭是人工动作，没有基于时间的自动关闭任务。
- 当前没有前端展示新旧 Incident 之间的同指纹关系。
- 下一切片创建最小 Vue 3 作战室，先实现 Incident 列表、详情和 SSE 时间线。

## 面试表达

我没有把 resolved 直接等同于 closed。验证通过后仍由工程师携带乐观锁版本确认关闭；关闭前复发自动回到调查，关闭后复发创建新事故。数据库行锁保证多个并发告警只触发一次状态变化，而所有不同告警都保留在时间线中，整个过程不依赖模型判断。
