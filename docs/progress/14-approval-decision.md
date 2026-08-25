# 14 - 审批决定、过期收敛与审计

## 目标

让工程师可以对精确计划版本提交批准或拒绝，并让超时请求形成明确、持久、可恢复的 `expired` 终态。决定必须与计划状态、Incident 状态和时间线保持事务一致。

本切片不实现登录授权、LangGraph 暂停恢复、白名单执行器或任何环境操作。批准只表示计划获得后续受控执行资格。

## 设计与边界

- 请求状态增加 `approved`、`rejected`、`expired`，三个终态不可相互覆盖。
- 计划同步进入同名终态，避免只看计划时无法判断审批结果。
- 批准通过 `approve_plan` 把 Incident 推进到 `mitigating`；拒绝通过 `reject_plan` 返回 `investigating`。
- 超时不是人工拒绝，新增 `expire_approval` 领域动作，并使用无操作者 ID 的 `system` 审计主体返回调查。
- 同方向重试返回原决定和 `decision_applied=false`；相反方向后到命令返回 `409`。
- 决定仓储锁定 Incident 后刷新请求和计划，防止等待锁期间使用过期 ORM 快照。
- 决定前重算风险策略与内容 SHA-256；乐观锁、状态或完整性失败会整笔回滚。
- PostgreSQL 约束强制人工终态和系统过期终态具有正确、完整的决定审计组合。

## 实际改动

1. 扩展审批请求、处置计划和 Incident 动作枚举，增加明确审批终态与过期动作。
2. 为审批读取模型增加 UTC 决定时间、决定主体、原因和跨字段终态校验。
3. Alembic `0008_approval_decisions` 增加决定审计列、状态约束和审计组合约束。
4. `ApprovalRequestStore.decide` 原子处理批准、拒绝、过期、同向幂等和相反决定冲突。
5. 控制面增加 approve、reject 两个语义化 REST 命令及稳定错误契约。
6. 增加审批审计单元测试、过期状态机测试、真实 PostgreSQL 原子决定、并发竞争和数据库约束测试。
7. 更新系统架构、技术栈、数据库、API、审批请求和项目入口文档。

## 测试方法与结果

定向验收：

```powershell
uv run pytest tests/unit/approval tests/unit/incidents/test_state_machine.py tests/unit/planning -q
uv run alembic upgrade head
uv run pytest tests/integration/test_approval_request_store.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py -q
```

结果：42 个状态机、审计模型和计划风险定向单元测试通过；迁移从 `0007_approval_requests` 升级到 `0008_approval_decisions`；24 个审批仓储与控制面定向测试通过。覆盖批准、同向幂等、拒绝、超时系统收敛、相反决定并发竞争、数据库决定审计约束和 API 冲突响应。

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

结果：全量 158 个测试通过，其中收集到 49 个真实 PostgreSQL 集成测试；138 个受 Ruff 管理的文件格式检查通过，Ruff、66 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。本地持久业务库为 `0008_approval_decisions (head)`，项目专属 `ops-swarm-control-db-1` 容器保持健康。

## 限制与下一步

- `actor_id` 不是可信登录身份，当前决定接口只能用于 localhost/Compose 演练。
- 没有角色、双人审批、撤销批准或后台过期扫描任务。
- 过期只在决定命令到达时显式收敛，纯 GET 不修改业务状态。
- 批准后 Incident 为 `mitigating`，但尚无执行记录，也没有动作真正发生。
- LangGraph 尚未在审批节点暂停和恢复；业务决定已经可以独立恢复。

下一切片实现确定性白名单执行器和 `operation_executions`：只接受有效已批准计划，重新校验计划绑定、动作参数和幂等键，只操作本地故障演练环境，并保存成功或失败结果。

## 面试表达

我把审批决定做成不可变状态机，而不是一个可以覆盖的字段。同方向重试幂等，相反决定冲突；并发命令通过 Incident 行锁串行化，取得锁后刷新请求，最终在一个事务里同步审批、计划、Incident 和时间线。超时用独立系统动作区分于人工拒绝，数据库还强制终态审计字段组合。批准接口没有环境写权限，只为下一层确定性执行器授予资格，这样审批和执行不会混在一起。
