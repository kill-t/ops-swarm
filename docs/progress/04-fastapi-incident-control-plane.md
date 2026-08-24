# 04 - FastAPI 事故控制面

## 目标

这一切片把已经验证的 Incident 领域规则和 PostgreSQL 仓储变成可调用、可查看 OpenAPI、可被未来前端和告警源使用的 HTTP 接口。它打通“告警进入系统 → 查询事故 → 开始调查 → 读取时间线”的最小控制面链路。

本切片不实现 LLM、调查 Worker、SSE、JWT 或前端。API 只绑定本地演练环境，不声称具备生产身份和审批能力。

## 设计与边界

### API 不复制业务规则

路由把 Pydantic 请求转换为领域输入，然后调用 `IncidentStore`。完全重复、指纹聚合、合法迁移和乐观锁仍分别由数据库约束、仓储事务和纯领域状态机决定。这样同一规则未来可以被 REST、Worker 和 LangGraph 节点复用。

### 服务器拥有接收时间

告警源只能提交带时区的 `occurred_at`。`ingested_at` 和开始调查的发生时间由服务器 UTC 时钟生成，防止客户端伪造控制面接收和状态变更时间。应用工厂允许测试注入固定时钟，使响应和审计断言稳定。

### 状态码表达明确语义

新外部告警始终返回 `201`，无论它新建 Incident 还是聚合到现有事故；完全重复事件返回 `200`。不存在返回 `404`，版本冲突和非法迁移都返回 `409`，但使用不同稳定错误码。请求模型错误和领域输入错误返回 `422`。

### 两类游标解决不同顺序

Incident 列表使用递减展示序号游标，避免新事故插入造成 offset 分页漂移。事件列表使用递增全局事件号游标，与未来 SSE `Last-Event-ID` 使用同一顺序。

### 数据库资源跟随应用生命周期

生产应用在 FastAPI lifespan 中创建异步引擎，退出时显式释放连接池。API 集成测试运行正式 lifespan，通过 HTTPX2 ASGI transport 访问真实 PostgreSQL，未用内存仓储代替业务路径。

## 实际改动

- 新增控制面应用工厂、Uvicorn 入口、Incident 路由和严格 Pydantic 模型。
- 新增健康检查、告警接收、Incident 列表/详情、开始调查和事件列表接口。
- 告警接收根据幂等结果动态返回 `200` 或 `201`。
- 新增展示序号倒序分页和状态筛选仓储查询。
- 新增请求校验、领域校验、不存在、版本冲突和非法迁移的结构化错误响应。
- FastAPI lifespan 统一创建和释放数据库资源，并允许测试注入设置与时钟。
- 把 PostgreSQL 测试容器和迁移 fixture 提升到测试根目录，供仓储和 API 共同复用。
- 新增 API 架构事实文档，并同步 README 和系统架构。

## 测试方法与结果

静态检查与非集成回归：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"
```

结果：Ruff 和 mypy 严格模式通过，61 个非集成测试通过，其中包含不连接数据库的 OpenAPI 契约测试。

控制面定向集成测试：

```powershell
uv run pytest tests\api\test_control_plane_api.py -vv
```

结果：11 个真实 PostgreSQL API 测试通过，覆盖应用生命周期、`200/201` 幂等语义、聚合时间线、Incident 游标分页、状态筛选、开始调查、事件重放游标以及结构化 `404/409/422`。

全仓库最终验收：

```powershell
uv run pytest
docker compose config --quiet
git diff --cached --check
```

结果：原有仓储测试和新增 API 测试共 83 个全部通过，其中 22 个连接真实 PostgreSQL；Compose 配置有效，暂存差异不存在空白错误。

## 限制与下一步

- 当前没有 JWT，`actor_id` 是不可信审计声明，不能用于生产审批。
- `/health` 只表示进程存活，数据库 readiness 和可观测性指标尚未实现。
- 时间线只能分页查询，尚未提供支持断线重放的 SSE 端点。
- 控制面 API 尚未制作独立容器，当前通过本地 Uvicorn 运行。
- 只有 `start-investigation` 动作暴露为 HTTP，其余迁移会随审批、执行和验证切片逐步开放。

下一切片开始建立单 Agent 调查基线：先定义只读调查工具协议和结构化调查结果，使用固定故障数据与假模型形成可重复评测，再接可配置的 OpenAI 兼容模型。多 Agent 必须在单 Agent 基线指标保存后再引入。

## 面试表达

我的 API 层不直接改 Incident 状态，而是把严格 Pydantic 请求转换为领域命令，再调用带 PostgreSQL 事务的仓储。告警接口用 `200/201` 区分完全重复和首次接收，用稳定游标分页避免并发插入导致 offset 漂移；状态动作必须携带乐观锁版本，过期版本和非法迁移都返回 `409` 但错误码不同。集成测试通过正式 FastAPI lifespan 和真实 PostgreSQL 覆盖了完整 HTTP 路径。
