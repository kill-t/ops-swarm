# 02 - Incident 纯领域状态机

## 目标

这一切片把事故从告警出现到正式关闭的生命周期变成可执行、可测试的确定性规则。它解决三个基础问题：当前状态允许做什么、并发操作者怎样避免覆盖彼此、每次变化怎样产生可审计事实。

此时先不接 FastAPI、SQLAlchemy、PostgreSQL 或 LLM，避免把状态规则与传输、存储和模型编排混在一起。下一个数据库切片可以直接复用经过测试的领域行为。

## 设计与边界

### 不可变快照和显式动作

Incident 使用冻结数据类表示某一版本的快照。调用方不能直接修改 `status`，只能向 `transition_incident` 提交一个 `IncidentAction`。状态机目前固定 11 条合法迁移，`closed` 不接受任何动作。

显式动作比直接提交目标状态多表达一层业务意图。例如，从 `investigating` 到 `verifying` 的动作是 `start_verification`，而从 `mitigating` 到同一目标的动作是 `complete_mitigation`。两者目标相同，审计含义不同。

### 乐观锁和事务输出

每次迁移必须携带 `expected_version`。版本不一致时抛出 `IncidentVersionConflict`，未来 API 映射为 `409 Conflict`。成功迁移生成版本加一的新快照，同时产生 `IncidentEvent`。

领域层返回 `IncidentMutation`，把新快照和配套事件绑定在一起，但不负责数据库事务。后续仓储层必须原子执行 Incident 更新与事件追加，任何一项失败都要整体回滚。

### 时间和生命周期字段

所有时间必须带时区，并在领域对象内部统一为 UTC。迁移时间不能早于当前快照的更新时间。进入 `resolved` 时写入 `resolved_at`，复发回到调查时清除它；进入终态 `closed` 时写入 `closed_at`。

### 审计不是授权

事件记录 `system`、`human`、`agent` 或 `executor` 操作者、操作者标识和中文原因。它回答“谁因为什么触发了变化”，但不证明操作者有权限。身份认证、审批有效性与执行授权留在后续应用层和处置域实现。

## 实际改动

- 新增 Incident 状态、严重级别、业务动作、操作者类型和事件类型枚举。
- 新增不可变 `Incident`、`IncidentActor`、`IncidentEvent` 和 `IncidentMutation` 领域对象。
- 新增创建事故和状态迁移两个纯函数，以及验证错误、版本冲突和非法迁移三类错误。
- 校验 UUID、必填文本、长度、枚举、版本、UTC 时间及生命周期字段一致性。
- 为 11 条合法迁移、终态、时间语义、版本冲突和非法输入增加单元测试。
- 更新数据库设计中的动作表和领域层到持久化层的事务边界。

## 测试方法与结果

先对新增模块执行定向检查：

```powershell
uv run ruff check src\opsswarm\incidents tests\unit\incidents
uv run mypy
uv run pytest tests\unit\incidents
```

结果：Ruff 与 mypy 通过，Incident 领域的 45 个测试全部通过。

随后执行提交前全量验收：

```powershell
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
git diff --cached --check
```

结果：锁文件有效，Ruff 规则和格式检查通过，mypy 严格模式检查 10 个源文件通过，全仓库 53 个测试全部通过，暂存差异不存在空白错误。

## 限制与下一步

- 当前对象只存在于测试或调用进程中，重启后不会保存。
- `IncidentActor` 是审计值对象，不提供登录、角色或审批权限校验。
- 领域事件还没有数据库递增游标，不能用于 SSE 重放。
- 告警的来源事件幂等和相同指纹并发聚合尚未实现。

下一切片将引入 SQLAlchemy 2、Psycopg 3、Alembic 和 PostgreSQL，先实现 `incidents`、`alerts`、`incident_events` 三张表及仓储事务，让领域状态与时间线在服务重启后仍可恢复。

## 面试表达

我没有一开始把 Incident 状态直接写进 FastAPI 路由或 LangGraph 节点，而是先实现了一个纯领域状态机。调用方只能提交显式业务动作和期望版本，状态机校验 11 条迁移并返回不可变新快照与审计事件。这样规则可以用毫秒级单元测试完整覆盖，ORM 只负责在同一事务里更新带乐观锁的 Incident 并追加事件。这个设计避免并发覆盖，也让后续 REST、Worker 和 Agent 编排复用同一套确定性规则。
