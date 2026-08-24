# 06 - 持久调查任务与独立 Worker

## 目标

把单 Agent 调查从进程内调用升级为不会因 API 或 Worker 重启而丢失的持久任务。开始调查必须同时迁移 Incident、创建调查运行并投递任务；独立 Worker 通过数据库租约领取、重试和保存结构化结果。

本切片不接 LangGraph、多 Agent、常驻轮询、SSE 或真实 Loki/Prometheus。Worker 默认不调用外部模型，真实模型必须显式配置。

## 设计与边界

### 三项写入共用一个事务

如果先把 Incident 改为 `investigating`，再创建任务，第二步失败会留下“显示调查中但永远没有 Worker 处理”的事故。因此 `InvestigationStore.start_investigation` 复用领域状态机，并在一个会话事务内保存 Incident 版本、状态事件、`investigation_runs` 和 `work_items`。

### 数据库任务不是业务结果

`work_items` 只保存调度状态、租约、重试和有限错误摘要；`investigation_runs` 才保存模型模式、提示版本、最终结构化结果和评测指标。以后清理调度数据也不能丢失调查审计。

### 租约令牌阻止迟到覆盖

每次领取都会生成新的 `lease_token`。完成或失败必须携带当前令牌且在有效期内提交；旧 Worker 即使使用相同 `worker_id`，也不能在其他实例接管后覆盖新结果。

### 处理器协议隔离编排框架

Worker 只依赖 `InvestigationProcessor.process(request)`。当前 LangChain 基线处理器和测试假处理器实现同一协议；后续 LangGraph 可以替换处理器而不修改租约状态机。

## 实际改动

- 新增 `investigation_runs` 和 `work_items` ORM 映射及第二个 Alembic 迁移。
- 新增调查运行、任务、错误、租约和读取快照模型。
- 抽取可在调用方事务中复用的 Incident 迁移操作。
- 实现原子开始调查、运行查询、任务查询、并发领取、到期接管、成功提交和失败重试。
- 使用 `FOR UPDATE SKIP LOCKED` 和每次唯一租约令牌保证多 Worker 正确性。
- 新增独立 Worker、处理器协议、指数退避与有限错误摘要。
- 提供显式真实模型配置的单次 Worker 命令入口。
- 开始调查 API 现在返回 Incident、Investigation 和任务标识；新增调查运行查询接口。
- 更新数据库、系统、API、单 Agent 和 README 架构事实。

## 测试方法与结果

定向真实 PostgreSQL 测试：

```powershell
uv run pytest tests/integration/test_migrations.py `
  tests/integration/test_investigation_store.py `
  tests/api/test_control_plane_api.py -q
```

结果：迁移一致性与回退、原子投递、并发领取、失败重试、租约接管、旧令牌拒绝、最终失败、Worker 成功落库和 HTTP 契约均已通过。

全仓库最终验收：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
docker compose config --quiet
git diff --check
```

结果：Ruff、格式和 mypy 严格模式通过；全仓库 101 个测试全部通过，其中 31 个测试使用 Testcontainers 连接真实 PostgreSQL 17。Compose 配置、Markdown 相对链接和 Git 空白检查均通过。本地持久开发库成功升级到 `0002_investigation_queue`，现有容器和数据卷继续保留。

## 限制与下一步

- Worker 每次启动只处理一个任务，不是常驻服务。
- Worker 还没有结构化日志、Prometheus 指标和 OpenTelemetry trace。
- 当前真实处理器只读取冻结演练数据，不代表已经接入 Loki、Prometheus 或 Git 平台。
- `result` 和 `metrics` 保存为已校验 JSONB，证据尚未拆入 Evidence Board。
- 没有 LangGraph checkpoint，Agent 内部执行仍不能从节点级恢复。

下一切片在稳定的任务和运行边界内引入 LangGraph 协调器，以及日志、指标、变更三个并行调查节点。单 Agent 和多 Agent 必须使用相同冻结场景与指标对照。

## 面试表达

我的 API 不直接调用模型，而是在一个 PostgreSQL 事务中迁移 Incident、创建调查运行和投递任务。Worker 用 `FOR UPDATE SKIP LOCKED` 并发领取，并使用带到期时间的唯一租约令牌防止旧实例迟到覆盖。异常按指数退避重试，最后一次租约崩溃也会由后续轮询收敛为失败。Agent 处理器通过协议注入，因此任务可靠性不依赖 LangChain 或未来 LangGraph。
