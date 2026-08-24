# LangGraph PostgreSQL checkpoint 恢复设计

## 1. 目标与边界

本切片让现有 LangGraph 并行调查图在进程或数据库连接中断后读取持久 checkpoint，并只重新运行失败节点。它同时建立业务调查运行与图线程的稳定关联。

本阶段不新增 `multi_agent` 任务类型，不修改 Worker 路由，也不实现人工暂停、审批或 checkpoint 清理策略。这样可以先独立证明图恢复语义，再把它接到现有至少一次任务处理边界。

## 2. 为什么使用两个数据库而不是两个容器

本地仍只有一个 PostgreSQL 17 容器和一个命名数据卷，其中包含：

| 数据库 | 管理者 | 保存内容 |
| --- | --- | --- |
| `opsswarm` | Alembic | Incident、调查运行、任务、审计和最终结果 |
| `opsswarm_checkpoints` | LangGraph `setup()` | 图状态快照、channel blob、pending writes 和 schema 版本 |

checkpoint 是恢复实现细节，不是业务事实。分库可以避免 Alembic 与 LangGraph schema 管理互相覆盖，也能在未来使用不同账号和保留策略。两个数据库通过字符串 `graph_thread_id` 关联，不建立跨库外键。

## 3. 稳定线程标识

每次业务调查运行使用确定性标识：

```text
investigation-run:{investigation_run_id}
```

`InvestigationStore.ensure_graph_thread_id()` 锁定业务运行后幂等写入该值。相同运行重复绑定返回原值；如果数据库已有不符合确定性规则的值则拒绝覆盖，避免一个业务运行静默切换到另一条 checkpoint 历史。

启用 checkpointer 后，首次调用图必须显式传入 `thread_id`。恢复调用不重复提交初始状态，而是用相同 `thread_id` 和空输入读取最近 checkpoint。

## 4. 恢复语义

LangGraph 在每个 superstep 保存状态。并行 superstep 中某个节点失败时，其他成功节点的写入以 pending writes 保留；再次使用同一线程恢复时，成功节点不需要重复执行。[LangGraph checkpoint 概念](https://reference.langchain.com/python/langgraph/checkpoints)、[持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence)

当前验证流程：

```mermaid
sequenceDiagram
    participant P1 as 进程/连接一
    participant CP as opsswarm_checkpoints
    participant P2 as 进程/连接二

    P1->>CP: 使用 thread_id 启动并行图
    par 并行 superstep
        P1->>CP: 指标节点成功写入
        P1->>CP: 变更节点成功写入
        P1--xCP: 日志节点失败
    end
    P1--xP1: 连接关闭
    P2->>CP: 同一 thread_id + 空输入恢复
    P2->>CP: 只重新运行日志节点
    P2->>CP: 综合并保存最终 checkpoint
```

这解决的是 Agent 图内部节点重复执行问题。业务 Worker 仍使用租约和至少一次语义；未来接入时，外部工具和最终结果写入仍必须幂等，不能因为有 checkpoint 就假设全系统“恰好一次”。

## 5. schema 初始化与本地持久化

全新 Compose 数据卷通过只读挂载的 `docker/postgres/init-checkpoint-db.sql` 创建第二个数据库。已有数据卷只需创建一次数据库，之后不会每次启动重建。

checkpoint 表由官方 `AsyncPostgresSaver.setup()` 幂等初始化：[AsyncPostgresSaver API](https://reference.langchain.com/python/langgraph.checkpoint.postgres/aio/AsyncPostgresSaver)

```powershell
docker compose up -d --wait control-db
uv run python -m opsswarm.checkpoints.setup
```

默认使用原生 Psycopg URL：

```text
postgresql://opsswarm:local-development-only@127.0.0.1:15432/opsswarm_checkpoints
```

可通过 `OPSSWARM_CHECKPOINT_DATABASE_URL` 覆盖。SQLAlchemy 的 `postgresql+psycopg://` URL 不传给 LangGraph checkpointer，避免混淆两套连接接口。

初始化命令会比较业务库和 checkpoint 库的主机、端口与数据库名；如果两者指向同一个数据库则拒绝执行，避免配置错误把 LangGraph 表混入业务事实库。

## 6. 反序列化安全

checkpoint 数据库包含可反序列化图状态。官方建议新应用启用严格 MessagePack 或提供允许类型列表，防止数据库被篡改后加载任意 Python 类型。[PostgreSQL checkpointer 安全说明](https://reference.langchain.com/python/langgraph.checkpoint.postgres)

OpsSwarm 显式允许调查请求、证据、专业报告、结论及其枚举类型；未启用 pickle fallback。该护栏减少类型加载攻击面，但不等于静态加密。生产化前还需要独立最小权限账号、传输加密、备份与保留策略，必要时再增加 checkpoint 加密。

## 7. 验收证据

真实 PostgreSQL 17 集成测试执行两次初始化，证明 schema 初始化幂等。首次图运行中，日志、指标、变更数据源各调用一次，日志分析随后失败。关闭第一条 saver 连接并创建全新的 saver 与图实例后恢复，实际调用为：

| 恢复进程数据源 | 调用次数 |
| --- | ---: |
| 日志 | 1 |
| 指标 | 0 |
| 变更 | 0 |

最终结果包含三类证据，图记录的工具调用总数为 3，说明两个已成功并行节点直接复用了 pending writes。

## 8. 当前限制与下一步

- 现有 API 仍只创建 `single_agent` 运行和任务。
- 现有 Worker 尚未打开 checkpointer，也不会调用 `resume()`。
- checkpoint 尚无过期清理、容量观测、备份和加密策略。
- checkpoint 保存恢复状态，但最终结论、证据、审批和执行审计仍必须写业务库。
- 冻结数据源当前为同步接口，接真实遥测前要增加异步超时和取消策略。

下一切片增加显式 `multi_agent` 调查模式和对应持久任务类型，让 Worker 根据业务运行模式选择单 Agent 或 LangGraph 处理器，并用当前 `graph_thread_id` 完成失败重试后的恢复。

## 9. 面试表达

我把业务事实库和 LangGraph checkpoint 分开管理，但本地复用同一个 PostgreSQL 容器，控制复杂度。业务运行用确定性 `thread_id` 关联 checkpoint。集成测试让一个并行节点失败，再关闭连接并创建新图实例；恢复时只重跑失败节点，证明 pending writes 真正跨进程边界生效。同时我限制 checkpoint 可反序列化类型，避免把数据库内容当成任意 Python 对象加载。
