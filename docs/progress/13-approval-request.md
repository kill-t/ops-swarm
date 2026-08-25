# 13 - 精确版本审批请求与原子状态迁移

## 目标

把需要人工批准的高风险 `draft` 计划提交为持久化 `pending` 请求，并保证审批对象、计划状态、Incident 状态和时间线在并发与失败场景下始终一致。

本切片只实现“请求决定”，不实现批准、拒绝、身份认证、LangGraph 恢复或执行动作。

## 设计与边界

- 审批请求绑定计划 ID、精确版本和内容 SHA-256，读取时重新校验风险策略与内容完整性。
- 内容哈希覆盖计划来源、版本、摘要、风险和全部有序步骤，但排除合法变化的生命周期状态。
- 创建请求时使用 PostgreSQL `FOR UPDATE` 锁定关联 Incident。
- 请求插入、计划 `draft → awaiting_approval`、Incident `investigating → awaiting_approval`、乐观锁版本递增和事件追加在同一事务完成。
- 同一计划最多一个请求；并发或网络重试返回同一记录，不重复迁移状态。
- 请求固定 30 分钟有效，但本切片不在读取路径静默改写过期状态；下一切片在决定命令中校验有效期。
- `actor_id` 只记录本地演练审计信息，不代表登录、角色或审批授权。

## 实际改动

1. 新增 `opsswarm.approval` 领域错误、请求状态、持久化记录、SQLAlchemy 表和事务仓储。
2. 新增确定性计划内容哈希，并让持久计划可以恢复为领域模型用于重算。
3. 扩展计划状态为 `draft`、`awaiting_approval`，继续复用确定性风险校验。
4. Alembic `0007_approval_requests` 创建审批表、精确计划版本复合外键、唯一约束、有效期与哈希检查约束。
5. 控制面增加审批请求 POST 与 GET API，以及稳定的 `404`、`409` 错误契约。
6. 增加内容哈希单元测试、真实 PostgreSQL 并发/回滚/外键/篡改测试和 API 全链路测试。
7. 更新系统架构、技术栈、数据库、API、计划边界和项目入口文档。

## 测试方法与结果

定向迁移与审批验收：

```powershell
uv run alembic upgrade head
uv run pytest tests/integration/test_approval_request_store.py tests/api/test_control_plane_api.py -q
```

结果：迁移从 `0006_action_plans` 成功升级到 `0007_approval_requests`；18 个审批仓储与控制面 API 定向测试通过。覆盖并发只创建一个请求、幂等重试、过期乐观锁整笔回滚、精确版本复合外键、计划内容篡改检测，以及计划与 Incident 同步迁移。

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

结果：全量 147 个测试通过，其中收集到 44 个真实 PostgreSQL 集成测试；133 个 Python 文件格式检查通过，Ruff、66 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。本地持久业务库为 `0007_approval_requests (head)`，项目专属 `ops-swarm-control-db-1` 容器保持健康。

## 限制与下一步

- 当前请求只有 `pending`，没有批准、拒绝或显式过期决定。
- 当前没有可信身份、角色、租户和审批权限；只能用于 localhost/Compose 演练。
- 一个计划只允许一个请求；拒绝后的修订应生成新计划版本。
- 内容 SHA-256 只检测业务库内漂移，不是数字签名或外部证据证明。
- Incident 已进入 `awaiting_approval`，但系统仍没有任何环境执行能力。

下一切片实现批准和拒绝决定：决定必须绑定当前 pending 请求，重验 30 分钟有效期、计划哈希与 Incident 乐观锁；批准只把状态推进到受控处置阶段，不直接执行故障开关。

## 面试表达

我把审批建模成精确版本的业务对象，而不是一个可覆盖的布尔字段。请求保存计划 ID、版本和内容哈希；创建时锁定 Incident，并在同一事务中写请求、更新计划与 Incident、追加时间线。并发重试只返回同一记录，乐观锁失败则全部回滚，计划被篡改后读取也会失败。这样先把“等待人工决定”做可靠，再单独实现身份、决定和白名单执行，安全边界更清楚。
