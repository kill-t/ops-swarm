# 08 - LangGraph PostgreSQL checkpoint 恢复

## 目标

为 LangGraph 并行调查图增加独立 PostgreSQL checkpoint，建立业务运行与 `thread_id` 的稳定关联，并通过真实数据库证明连接关闭后只重新运行失败节点。

本切片不把多 Agent 接入 Worker，不增加新的 API 模式，不实现审批暂停或 checkpoint 清理。

## 设计与边界

### 同一实例、两个逻辑数据库

业务库 `opsswarm` 继续由 Alembic 管理，`opsswarm_checkpoints` 只由 LangGraph `setup()` 管理。本地复用现有 PostgreSQL 容器和数据卷，不额外增加数据库容器。

### 确定性 thread_id

业务运行 ID 推导出 `investigation-run:{run_id}`。仓储使用行锁幂等绑定，拒绝覆盖不匹配的历史关联。启用 checkpointer 的图必须显式传入线程标识。

### 节点级恢复而不是整图重跑

首次运行故意让日志节点在指标和变更节点成功后失败。关闭 saver 后使用新连接、新图实例和同一 `thread_id` 恢复，验证只重新运行日志节点。

### 限制 checkpoint 反序列化类型

序列化器只允许当前图状态明确使用的调查模型和枚举，不启用任意类型或 pickle fallback。该措施不替代数据库最小权限和静态加密。

## 实际改动

- 新增 `langgraph-checkpoint-postgres` 直接生产依赖。
- 新增 checkpoint 配置、异步 saver 生命周期和幂等 schema 初始化命令。
- 初始化命令拒绝把 checkpoint schema 建入同一个业务数据库。
- Compose 为全新数据卷挂载第二数据库初始化 SQL。
- 为 `investigation_runs.graph_thread_id` 增加确定性生成、校验和幂等绑定仓储方法。
- 并行调查图支持可选 checkpointer、显式首次运行和空输入恢复。
- 新增真实 PostgreSQL 跨连接恢复测试以及配置、线程标识和业务绑定测试。
- 更新 README、系统架构、技术栈、数据库设计和并行基线文档。

## 测试方法与结果

定向检查：

```powershell
uv run pytest tests/unit/investigation/test_graph_thread.py tests/unit/test_checkpoint_settings.py tests/unit/investigation/test_parallel_graph.py -q
uv run pytest tests/integration/test_checkpoint_recovery.py tests/integration/test_investigation_store.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

结果：14 个相关单元测试和 10 个相关真实 PostgreSQL 集成测试通过。恢复进程只调用日志源一次，指标和变更源均未重新调用；最终三类证据完整，工具调用数仍为 3。

全仓库最终验收：

```powershell
uv run pytest
docker compose config --quiet
uv run python -m opsswarm.checkpoints.setup
git diff --check
```

结果：全仓库 117 个测试全部通过，其中 33 个测试使用 Testcontainers 连接真实 PostgreSQL 17。Ruff、格式和 mypy 严格模式、Compose 配置、Markdown 相对链接及 Git 空白检查全部通过。本地 `ops-swarm-control-db-1` 保持健康运行，`opsswarm_checkpoints` 已持久创建并包含 LangGraph 管理的四张表。

## 限制与下一步

- API 和 Worker 仍只支持 `single_agent`。
- checkpoint 数据还没有保留、容量、备份和加密策略。
- 当前本地两个数据库暂时使用同一开发账号，生产部署必须拆分最小权限账号。
- 业务结果与审计不会依赖 checkpoint，下一阶段仍需在 Worker 租约内持久化最终结果。

下一切片增加 `multi_agent` 运行模式和持久任务路由，让 Worker 打开 checkpoint saver 并在任务重试时使用同一线程恢复。

## 面试表达

我没有把 checkpoint 混进业务表，而是让 Alembic 和 LangGraph 各自管理独立数据库。业务运行用确定性线程 ID 关联。真实 PostgreSQL 测试中，我让并行图一个节点失败，关闭连接后用新图实例恢复，只有失败节点被重新执行；同时使用反序列化类型白名单缩小 checkpoint 数据被篡改后的攻击面。
