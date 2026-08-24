# 03 - PostgreSQL 事故持久化

## 目标

这一切片让 Incident、原始告警和事故时间线从进程内对象变成可恢复的业务事实，并把上一切片的领域输出落实为原子数据库事务。它重点解决完全重复告警、同一故障的并发告警、并发状态修改和服务重连后的数据恢复。

本切片不提供 FastAPI 接口，不接 LLM，也不提前实现审批或 Worker 表。输入输出先通过仓储集成测试验证，下一步再增加 HTTP 边界。

## 设计与边界

### 三类事实各司其职

- `incidents` 保存当前快照、乐观锁版本和生命周期时间。
- `alerts` 保存每个外部告警及 `(source, external_event_id)` 幂等键。
- `incident_events` 只追加时间线，并使用全局递增主键作为后续 SSE 游标。

Incident 当前状态不是事件回放的缓存，也不只存在 LangGraph checkpoint；它是可以直接查询和加乐观锁的业务事实。时间线负责解释它如何变化，两者在一次状态迁移中原子写入。

### 数据库约束决定并发正确性

仓储会先查询重复事件和活动 Incident，以减少正常路径上的异常，但查询结果不能防止另一个事务同时插入。最终正确性由两个 PostgreSQL 约束保证：

- 告警 `(source, external_event_id)` 唯一约束保证完全重复只落一条。
- Incident `fingerprint` 在 `status <> 'closed'` 条件下的部分唯一索引保证同一指纹最多一个未关闭事故。

并发命中这两个约束时，失败事务整体回滚，仓储创建新会话重试。重试后会返回原告警，或把新告警聚合到已经胜出的 Incident。其他完整性错误不会被盲目吞掉。

### 领域层与 ORM 隔离

SQLAlchemy 映射使用字符串保存领域枚举，读取时重新构造并校验 `Incident`。告警创建 Incident 和状态迁移仍调用纯领域函数，ORM 不复制状态机规则。

领域迁移返回的新快照通过 `WHERE id = :id AND version = :expected_version` 更新。只有受影响行数为一时才追加事件；版本竞争会抛出冲突并回滚整个事务。

### 迁移与运行环境

Alembic 只管理 `opsswarm` 业务库。Compose 提供固定版本 `postgres:17.6-alpine` 本地开发服务，Testcontainers 为测试启动隔离实例。测试执行正式迁移，并使用 `alembic check` 防止迁移脚本与 ORM 元数据漂移。

Psycopg 异步模式在 Windows 不支持默认 Proactor 事件循环，因此迁移和测试显式使用 Selector 循环；该兼容策略位于数据库基础设施，不影响领域层。

## 实际改动

- 引入 SQLAlchemy 2、Psycopg 3、Alembic、pydantic-settings、pytest-asyncio 和 Testcontainers。
- 增加业务数据库环境配置、可显式释放连接池的异步数据库资源和 Windows 事件循环兼容层。
- 增加 PostgreSQL 17 Compose 服务、健康检查、localhost 端口和持久卷。
- 建立 `incidents`、`alerts`、`incident_events` ORM 映射及首个可回退迁移。
- 增加状态、严重级别、JSON 对象、生命周期时间和操作者等数据库检查约束。
- 实现告警校验、64 KiB payload 限制、幂等接收、指纹聚合和唯一冲突重试。
- 实现 Incident 查询、带乐观锁的原子迁移和基于事件游标的时间线读取。
- 增加 `incident.alert_attached` 事件；聚合新告警时不改变事故状态和版本。

## 测试方法与结果

定向检查和非集成测试：

```powershell
uv lock --check
uv run ruff check src alembic tests
uv run ruff format --check src alembic tests
uv run mypy
uv run pytest -m "not integration"
```

结果：依赖锁定有效，Ruff 和 mypy 严格模式通过，60 个非集成测试通过。

真实 PostgreSQL 集成测试：

```powershell
uv run pytest -m integration -vv
```

结果：11 个集成测试通过，包括首次创建、完全幂等、同指纹聚合、8 路并发不同事件、8 路并发重复事件、乐观锁事务、并发迁移、关闭后新建、数据库引擎重连恢复、Alembic 元数据无漂移以及真实降级后重新升级。测试输出确认 `No new upgrade operations detected.`。

全仓库最终验收：

```powershell
uv run pytest
docker compose config --quiet
git diff --cached --check
```

结果：全仓库 71 个测试通过，Compose 配置有效，暂存差异不存在空白错误。

## 限制与下一步

- 当前仓储还没有 FastAPI 输入模型和 HTTP 状态码映射。
- 数据库运行账号与迁移账号在本地 Compose 中暂时相同，生产部署前必须拆分最小权限角色。
- 事件表在应用仓储中只暴露读取和追加，但尚未用数据库角色撤销 `UPDATE`、`DELETE` 权限。
- 当前只有查询单个 Incident 和时间线，没有分页列表与筛选。
- SSE 尚未实现，但事件的全局递增游标和 `after_cursor` 查询已经准备好重放语义。

下一切片将实现 FastAPI 控制面：`POST /api/v1/alerts`、Incident 列表/详情、带 `expected_version` 的开始调查动作，以及时间线查询。它会把重复告警映射为 `200`，新告警映射为 `201`，非法迁移和版本冲突映射为 `409`。

## 面试表达

我用 PostgreSQL 约束而不是进程锁解决告警并发：外部事件唯一键保证完全幂等，未关闭指纹的部分唯一索引保证同类故障只有一个活动 Incident。应用先查询优化正常路径，但并发冲突时整个事务回滚并用新会话重试。状态迁移继续复用纯领域状态机，然后用带版本条件的更新和事件插入组成同一事务。真实 PostgreSQL 集成测试覆盖了 8 路竞争、重连恢复和 Alembic 漂移，SQLite 无法替代这些语义。
