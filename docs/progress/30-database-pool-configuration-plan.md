# 30 - 数据库连接池配置回滚计划模板

## 目标

为已经能被 Agent 准确识别的数据库连接池配置错误生成可持久化、可审查的确定性 Action Plan 草案，同时保证没有执行器的动作不能进入审批或执行阶段。

## 设计与边界

- 只允许 `database_pool_size`，值域为 1～1000。
- 当前值和目标值只从同一条已完成配置变更证据的 `to_value`、`from_value` 提取。
- 配置计划使用 `rollback-configuration-database-pool-v1` 模板。
- `action-risk-v2` 把配置回滚固定判为高风险并要求人工审批。
- 历史 `action-risk-v1` 版本回滚计划继续按原规则读取。
- 计划可创建为 `draft`，但审批仓储返回 `action_plan_not_executable`，不迁移 Incident。
- 执行仓储和 HTTP 适配器继续只允许版本回滚，形成第二道防线。
- 本切片不调用 LLM，不修改 `.env`，也不产生模型费用。

## 实际改动

- 新增 `rollback_configuration` 动作、配置键枚举和精确参数模型。
- `ActionStep` 根据动作类型强制对应参数模型。
- 计划器新增数据库连接池变更证据筛选、配置回滚摘要和饱和度验证规则。
- 能力查询把数据库配置回滚从 `unsupported` 升级为 `available`。
- 风险策略升级为 v2，并保留 v1 历史验证分支。
- 持久化读取根据动作类型恢复正确的参数模型。
- 新增审批前执行能力检查和执行仓储、适配器的防御性拒绝。
- 新增 Alembic `0014_configuration_rollback`，扩展动作检查约束并保护降级安全。
- 更新 README、技术栈、数据库、Action Plan、审批、能力映射和 API 文档。

## 测试方法与结果

定向领域测试：

```powershell
uv run pytest tests/unit/planning tests/unit/approval `
  tests/unit/execution tests/unit/verification -q
```

36 个测试通过，覆盖配置能力、参数提取、风险策略、历史 v1 读取和适配器拒绝。

真实 PostgreSQL API 定向测试覆盖：

- 原版本回滚完整链路继续通过；
- 配置回滚能力返回可用；
- 配置计划和步骤成功持久化；
- 配置参数为 `5 → 50`，验证信号为连接池饱和度；
- 提交审批返回结构化 `409`；
- Incident 保持 `investigating` 且版本不变；
- 审批记录和执行记录数量均为 0。

完整验收命令：

```powershell
uv run pytest -q
uv run ruff check src tests alembic
uv run ruff format --check src tests alembic
uv run mypy
uv run alembic upgrade head
uv run alembic current
uv run alembic check
docker compose config --quiet
git diff --check
```

完整 pytest 回归通过；Ruff 检查通过，176 个文件格式符合规范；mypy 对 109 个源文件检查通过。固定开发库已升级到 `0014_configuration_rollback`，Alembic 确认没有待生成迁移。

## 限制与下一步

- 目前只能生成配置回滚草案，不能提交审批或执行。
- 故障实验室还没有数据库连接池错误容量状态。
- 恢复验证器还不能采集 `database_pool_saturation`。
- 下一步实现确定性数据库连接池故障模拟和白名单管理接口，不直接放开审批执行。

## 面试表达

我为数据库连接池配置错误新增了确定性回滚草案：只允许一个配置键，参数只能从已提交变更证据提取，风险由 v2 策略重算，同时保留 v1 历史兼容。由于执行器尚未实现，我在审批事务迁移 Incident 之前就拒绝请求，并验证没有审批和执行记录落库。这样模型识别能力可以先转成可审查计划，但不会提前扩大生产变更权限。
