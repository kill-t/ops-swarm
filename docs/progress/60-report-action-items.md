# 60 - 复盘改进行动项与完成审计

## 目标

把不可变事故复盘后的改进建议变成可持久化、可分配、可完成和可审计的结构化行动项，同时不允许 LLM 直接创建或关闭组织任务。

## 设计与边界

- 行动项只归属于已经冻结的 `incident_reports`，不修改报告正文和内容摘要。
- 创建请求使用报告内唯一幂等键；同键同内容返回原记录，同键不同内容冲突。
- 完成动作要求 `expected_version`，首次完成从版本 1 迁移到版本 2。
- 完全相同的完成重试不追加第二条事件，不同内容不能覆盖原完成审计。
- 当前状态与 `created` / `completed` 事件在一个 PostgreSQL 事务内提交。
- 本步不做前端、编辑、重开、通知或外部工单同步。

## 实际改动

- 新增 `follow_up` 领域模型、稳定错误和纯状态机。
- 新增 `report_action_items` 与 `report_action_item_events` SQLAlchemy 映射及 Alembic 迁移。
- 新增创建、列表、完成和事件读取四个 REST 操作。
- 新增稳定的 `404`、`409` 和领域校验 `422` 错误映射。
- 增加状态机、OpenAPI、迁移往返和真实 PostgreSQL API 集成测试。

## 测试方法与结果

```powershell
uv run ruff check src/opsswarm/follow_up src/opsswarm/control_plane/app.py src/opsswarm/control_plane/routes.py src/opsswarm/control_plane/schemas.py alembic/env.py alembic/versions/20260904_0028_create_report_action_items.py tests/unit/follow_up tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py
uv run mypy src/opsswarm/follow_up src/opsswarm/control_plane/app.py src/opsswarm/control_plane/routes.py src/opsswarm/control_plane/schemas.py
uv run pytest tests/unit/follow_up tests/api/test_control_plane_openapi.py -q
uv run pytest tests/integration/test_migrations.py -q
uv run pytest tests/api/test_control_plane_api.py -k "report_action_item" -q
```

Ruff 和 mypy 通过；领域状态机与 OpenAPI 共 `4 passed`，迁移往返 `3 passed`，行动项 API 集成 `2 passed`。覆盖同键幂等、内容冲突、报告不存在、非法截止时间、过期版本、完成重试和两条审计事件。

项目持久容器已从 `0027_create_incident_reports` 升级到 `0028_create_report_action_items`，迁移容器退出码为 0；业务库实际存在 `report_action_items` 和 `report_action_item_events`，原 `ops-swarm-control-db-1` 数据卷保持不变。

本切片只运行与改动直接相关的测试，没有重复执行完整后端或前端测试集。

## 限制与下一步

- 浏览器尚不能查看或操作行动项。
- `owner_id` 和 `actor_id` 是本地演练审计声明，不是认证身份。
- 尚无逾期通知、编辑、重开和第三方工单同步。
- 下一步在事故复盘报告页面增加行动项列表、创建表单和受版本保护的完成功能。

## 面试表达

> 我把事故报告保持为不可变快照，把复盘后的组织改进建成独立聚合。行动项创建使用业务幂等键，完成使用乐观锁；仓储用行锁串行化竞争，并把当前投影和追加式审计事件放在同一事务。相同请求可安全重试，不同请求不能借幂等键或重复完成去改写历史。
