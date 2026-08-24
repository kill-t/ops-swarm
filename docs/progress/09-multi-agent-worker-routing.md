# 09 - 多 Agent 持久 Worker 路由

## 目标

让控制面能够显式创建单 Agent 或多 Agent 调查任务，并让独立 Worker 根据持久模式确定性路由。多 Agent 失败重试必须使用现有 PostgreSQL checkpoint 恢复，而不是整图重跑。

本切片不实现 Evidence Board、常驻 Worker、SSE、审批或外部监控工具。

## 设计与边界

### 模式、任务和线程同时持久化

`single_agent` 对应单 Agent 任务，`multi_agent` 对应多 Agent 任务和确定性 `graph_thread_id`。数据库约束多 Agent 必须有线程，Worker 再校验模式与任务类型一致。

### 保持 API 向后兼容

开始调查请求新增可选 `mode`，默认仍为 `single_agent`。调用方不能指定内部任务类型、提示版本或线程 ID。

### 查询 checkpoint 决定恢复

多 Agent 处理器不依据 `attempt_count` 猜测。线程已有 checkpoint 就用空输入恢复；完全没有 checkpoint 才提交初始状态。

## 实际改动

- 新增 `multi_agent` 运行模式和 `run_multi_agent_investigation` 任务类型。
- 新增 Alembic `0003_multi_agent_worker` 及 ORM 同步约束。
- API 开始调查请求支持可选模式，非法模式返回 `422`。
- 多 Agent 运行在创建事务内获得稳定 `graph_thread_id`。
- Worker 使用 `ProcessorContext` 传递持久路由事实。
- 新增单/多模式确定性路由和 LangGraph 多 Agent 处理器。
- Worker 进程打开独立 checkpoint saver，保留单 Agent 基线处理器。
- checkpoint 测试夹具提取为共享真实 PostgreSQL 测试基础设施。
- 新增多 Agent Worker 跨尝试恢复端到端测试。

## 测试方法与结果

定向测试：

```powershell
uv run pytest tests/unit/investigation/test_worker_routing.py tests/api/test_control_plane_api.py tests/integration/test_investigation_store.py tests/integration/test_checkpoint_recovery.py tests/integration/test_multi_agent_worker.py tests/integration/test_migrations.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

结果：28 个相关路由、API、仓储、checkpoint、Worker 和迁移测试通过。多 Agent 第二次处理使用新 saver 恢复，只调用日志源一次，指标与变更源均为 0 次；业务运行成功、任务完成，`attempt_count=2`。

全仓库最终验收：

```powershell
uv run pytest
docker compose config --quiet
uv run alembic current
git diff --check
```

结果：全仓库 121 个测试全部通过，其中 36 个测试使用 Testcontainers 连接真实 PostgreSQL 17。Ruff、格式和 mypy 严格模式、Compose 配置、Markdown 相对链接及 Git 空白检查全部通过。本地业务库已升级到 `0003_multi_agent_worker`，持久 PostgreSQL 容器与 checkpoint 数据库继续保持健康可用。

## 限制与下一步

- 数据源仍为冻结演练数据。
- 专业报告和证据尚未拆分为业务表。
- Worker 仍为单次命令。
- 没有记录多 Agent 节点级 Token、成本和耗时。

下一切片建立 Evidence Board，先持久化规范化证据和专业报告，再增加质疑节点与假设关系。

## 面试表达

我没有让模型选择单 Agent 还是多 Agent，而是把模式、任务类型和线程 ID 作为持久业务事实。Worker 根据模式确定性路由，多 Agent 通过查询 checkpoint 决定首次运行或恢复。真实 PostgreSQL 端到端测试证明第一个 Worker 失败后，第二个 Worker 只重跑失败节点并安全提交最终结果。
