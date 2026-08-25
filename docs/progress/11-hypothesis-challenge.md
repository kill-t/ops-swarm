# 11 - 根因假设与确定性质疑

## 目标

在已持久化的 Evidence Board 上建立主根因假设、逐证据支持或反驳关系，以及不依赖 LLM 的安全质疑门槛。证据不足或存在竞争性解释时，调查可以被审计地完成，但不能把结论直接送入处置阶段。

本切片不生成处置计划，不创建审批，不执行操作，也不改变 Incident 状态。

## 设计与边界

- 每次调查当前只保存一个主假设，后续需要候选假设时再扩展排序和版本模型。
- 每条 Evidence Board 证据必须被标记为 `supports`、`refutes` 或 `context`，不能只挑支持结论的证据。
- 存在任意反驳证据时状态为 `contested`；没有反驳且至少两类独立证据支持时为 `supported`；其余为 `insufficient_evidence`。
- 质疑规则是带 `hypothesis-challenge-v1` 版本的纯 Python 领域函数，不调用模型或外部工具。
- 多 Agent 在 LangGraph 综合节点后运行质疑节点，单 Agent 处理器复用同一函数。
- Worker 输出模型和仓储都会重新计算质疑结果，防止伪造或过期规则结果进入业务库。
- 假设关系只随成功调查在有效 Worker 租约事务中提交；checkpoint 中间结果不是业务事实。

## 实际改动

1. 新增假设状态、证据关系和结构化质疑结果模型。
2. 新增确定性质疑规则，覆盖版本回归、数据库连接池异常和 Redis 不可用的最小特征。
3. LangGraph 增加 `challenge_hypothesis` 节点，并把质疑结果纳入 PostgreSQL checkpoint。
4. 单 Agent 与多 Agent Worker 统一提交经过重算校验的质疑结果。
5. Alembic `0005_hypothesis_challenge` 新增 `hypotheses` 和 `hypothesis_evidence`，用复合外键拒绝跨调查关系。
6. Evidence Board API 增加假设状态、规则版本和逐证据关系响应。
7. 更新系统架构、技术栈、数据库、API 和 Evidence Board 文档。

## 测试方法与结果

纯规则、API 和非集成测试：

```powershell
uv run pytest -m "not integration" -q
```

结果：95 个测试通过。覆盖两类独立证据通过、依赖异常反驳版本回归、单类证据不足、数据库健康但连接池饱和、Redis 指标与日志相互印证，以及伪造质疑结果在处理器和仓储边界被拒绝。

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

结果：全量 134 个测试通过，其中收集到 39 个真实 PostgreSQL 集成测试；107 个 Python 文件格式检查通过，Ruff、49 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。迁移测试覆盖 `0005` 降级与重新升级，本地持久业务库位于 `0005_hypothesis_challenge (head)`，`ops-swarm-control-db-1` 保持健康。Markdown 相对链接和代码围栏检查也通过。

## 限制与下一步

- 当前只保存主假设，不管理候选假设排序。
- 规则只覆盖三个 MVP 故障类别的最小特征，不承担通用因果推断。
- 质疑失败不会自动发起新工具查询，Incident 始终保持 `investigating`。
- 规则升级必须使用新版本并补充评测，不能覆盖历史判断。

下一切片只为 `supported` 假设生成版本化结构化处置计划并进行风险分级；仍不执行动作，也不提前批准计划。

## 面试表达

我没有再加一个 LLM 当裁判，而是在综合节点后增加了版本化确定性质疑。系统逐条保存支持、反驳和上下文关系，只有至少两类独立证据支持且没有反驳时才通过。Worker 和仓储会重算结果，PostgreSQL 用同运行复合外键及租约事务保护完整性，所以“模型认为自己正确”被转换成了可测试、可恢复、可审计的业务门槛。
