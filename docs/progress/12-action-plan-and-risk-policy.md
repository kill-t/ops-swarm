# 12 - 版本化处置计划与风险分级

## 目标

把 `supported` 主根因转换为可审查、可持久化、可供后续审批绑定的结构化计划，并使用确定性规则阻止模型或调用方降低风险、绕过审批。

本切片不创建审批，不迁移 Incident，不执行故障开关，也不接入任意 Shell 或 Docker 控制。

## 设计与边界

- 调查事实先独立提交，计划生成失败不能回滚 Evidence Board。
- 当前只支持 `release_500_regression → rollback_release`，参数从已完成部署证据提取。
- 计划和步骤使用严格 Pydantic 模型，额外字段与未知动作会被拒绝。
- `action-risk-v1` 把版本回滚固定为高风险并要求人工审批，写入和读取时都会重算。
- PostgreSQL 通过同调查复合外键、计划版本唯一约束和高风险审批检查约束兜底。
- POST 首次创建返回 `201`，并发或重复生成返回同一计划和 `200`。
- 计划保持 `draft`，Incident 继续是 `investigating`。

## 实际改动

1. 新增 `opsswarm.planning` 模块，包含结构化计划、回滚参数、验证规则和风险模型。
2. 新增纯 Python 计划模板及 `action-risk-v1` 风险策略。
3. Alembic `0006_action_plans` 创建 `action_plans` 和 `action_steps`。
4. 新增 `ActionPlanStore`，支持并发幂等生成和读取当前版本。
5. 控制面增加计划 POST 和 GET API，以及稳定的 `404`、`409` 错误契约。
6. 增加单元、真实 PostgreSQL、并发、数据库约束和 API 契约测试。
7. 更新系统架构、技术栈、数据库、API 和前序质疑文档。

## 测试方法与结果

切片定向测试：

```powershell
uv run pytest tests/unit/planning -q
uv run pytest tests/integration/test_migrations.py tests/integration/test_investigation_store.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py -q
```

结果：6 个纯领域计划与风险测试通过；31 个迁移、真实 PostgreSQL、并发幂等和 API 定向测试通过。覆盖未通过质疑时阻止计划、缺少安全版本时拒绝回滚、风险与策略版本防篡改、跨调查计划外键、高风险免审批数据库约束，以及重复 POST 返回相同版本。

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

结果：全量 142 个测试通过，其中收集到 41 个真实 PostgreSQL 集成测试；121 个 Python 文件格式检查通过，Ruff、58 个源码文件的 mypy 检查、Alembic 元数据一致性和 Docker Compose 配置检查通过。本地持久业务库已升级到 `0006_action_plans (head)`，`ops-swarm-control-db-1` 保持健康。Markdown 相对链接和代码围栏检查也通过。

## 限制与下一步

- 当前只有版本回滚模板，没有数据库连接池和 Redis 处置模板。
- 当前只生成版本 1，没有人工修改和新版本接口。
- 没有审批主体、有效期、决定、状态迁移或执行授权。
- 恢复判据只被保存，尚未运行验证。

下一切片创建绑定精确计划版本的审批请求，并在同一事务中迁移 Incident 到 `awaiting_approval`；批准计划和执行动作继续保持未实现。

## 面试表达

我把处置建议从文本升级为不可变版本化计划。回滚参数只能来自已提交证据，风险由纯规则重算，数据库再用检查约束阻止高风险免审批。计划生成独立于调查事务，并发调用通过唯一键只生成一个版本，因此既保留调查事实，又为后续审批建立了稳定、可审计的绑定对象。
