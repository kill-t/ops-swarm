# 31 - 数据库连接池确定性故障演练

日期：2026-08-29

## 目标

把评测集和处置计划中的数据库连接池配置错误落实为可运行、可重复的本地故障，使后续白名单执行器和恢复验证拥有稳定目标。

## 设计与边界

- 连接池容量默认值为 `50`，固定演练负载容量为 `10`。
- 容量低于负载时订单固定返回 `500 database_pool_timeout`，不使用随机数或真实连接耗尽。
- 管理接口只允许严格整数 `1～1000`，控制器内部再次校验。
- 状态响应同时提供 `database_pool_saturation` 和 `database_pool_waiters`。
- `release_500` 与连接池错误同时存在时，版本故障固定优先。
- 故障状态属于单进程演练旋钮，重启后恢复安全默认值，不写入业务数据库。
- 本切片不开放配置计划审批、执行或恢复验证。

## 实际改动

1. 扩展 `FaultSnapshot`，由连接池容量确定性计算饱和度、等待数和故障状态。
2. 扩展线程安全 `FaultController`，增加默认容量和白名单更新方法。
3. 新增连接池配置查询与更新模型，拒绝未知字段、字符串、布尔值和越界容量。
4. 新增 `GET/PUT /__admin/configuration/database-pool`。
5. 扩展订单接口，在连接池容量不足时返回有限的结构化 500。
6. 为版本故障与连接池故障定义固定优先级。
7. 把处置计划中的配置整数参数收紧为 Pydantic 严格整数。
8. 更新 README、系统架构、计划能力边界和下一步说明。

## 测试方法与结果

定向测试覆盖控制器、故障实验室 API 和配置计划参数：

```powershell
uv run pytest `
  tests/unit/test_fault_controller.py `
  tests/api/test_fault_lab_api.py `
  tests/unit/planning/test_planner.py -q
```

结果：29 项测试全部通过。

真实 Docker Compose 端到端验证：

1. 启动并重建项目专属 `order-service`；
2. 初始容量 `50`、饱和度 `0.2`；
3. 设置容量 `5` 后饱和度 `1.0`、等待数 `5`；
4. 订单请求返回 `500 database_pool_timeout`；
5. 恢复容量 `50` 后订单返回 `200`；
6. 容器健康，并在验证结束后保持运行。

最终验收结果：

- 全量 291 项 Python 测试通过，其中 66 项使用真实 PostgreSQL/Testcontainers 的集成测试。
- Ruff 检查通过，242 个受格式检查的文件均已格式化。
- mypy 严格模式检查 109 个源码文件，无类型问题。
- 项目业务库位于 `0014_configuration_rollback (head)`，`alembic check` 未发现迁移漂移。
- `docker compose config --quiet`、`git diff --check` 和 `.env` 忽略规则检查通过。
- 项目专属 `control-db` 与 `order-service` 容器均保持健康运行。
- 本切片没有调用模型 API，不产生 Token 或模型费用。

## 限制与下一步

- 当前是确定性故障模型，不是真实 SQLAlchemy 或 PostgreSQL 连接池压测。
- 管理接口没有生产鉴权，只能用于 localhost 或 Compose 私有网络。
- 配置动作仍不能审批或执行。
- 下一步增加固定配置回滚执行适配器和 `database_pool_saturation` 只读恢复探针；完成幂等、当前值漂移和失败回流测试后才开放审批。

## 面试表达

我把数据库连接池配置错误设计为一个确定性被控系统：固定负载为 10，容量 5 稳定产生等待和超时，容量 50 稳定恢复。相比真实耗尽数据库连接，这种模型更快、更安全，也适合重复评测。HTTP 与领域层都做白名单校验，故障组合有固定优先级，并且没有因为模拟接口存在就提前开放 Agent 执行权限。
