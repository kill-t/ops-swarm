# 29 - 确定性处置计划能力映射

## 目标

把调查建议 v2 与现有 Action Plan 安全边界真正衔接起来。系统需要明确告诉工程师某条建议是否有确定性模板，并防止配置回滚、依赖恢复或继续调查被错误转换成现有的版本回滚动作。

## 设计与边界

- 能力判断同时读取 `root_cause`、`recommendation` 和 `hypothesis_status`。
- 状态分为 `available`、`unsupported`、`not_applicable`，模板存在与当前可创建使用不同字段表达。
- 当前只开放 `release_500_regression + rollback_release → rollback-release-v1`。
- `rollback_configuration` 与 `restore_dependency` 返回 `template_not_implemented`，没有 Action Plan 或执行权限。
- `continue_investigation` 返回 `not_applicable`，不创建空计划。
- 根因与建议不匹配时返回 `recommendation_root_cause_mismatch`，禁止借用其他模板。
- 能力是业务事实的实时确定性投影，不新建数据库表，不调用模型，也不修改 Incident。

## 实际改动

- 新增 `ActionPlanCapability`、能力状态和稳定原因码。
- 新增纯领域函数 `assess_action_plan_capability` 与模板版本 `rollback-release-v1`。
- `propose_action_plan` 改为必须显式接收建议，并在提取回滚参数前强制能力判断。
- `ActionPlanStore` 从持久调查结果和主假设重算能力，并校验两处根因一致。
- 新增 `GET /api/v1/investigations/{run_id}/action-plans/capability`。
- 创建计划遇到不可用模板时返回 `409 action_plan_capability_unavailable`，详情包含完整能力判断。
- 保留既有版本回滚、审批、执行和验证链路，不修改数据库结构。
- 更新 README、Action Plan、API 和建议契约架构文档。

## 测试方法与结果

纯领域、执行适配器、验证适配器和 OpenAPI 定向测试：

```powershell
uv run pytest tests/unit/planning tests/unit/execution/test_adapter.py `
  tests/unit/verification/test_adapter.py tests/api/test_control_plane_openapi.py -q
```

共 29 个定向测试通过。真实 PostgreSQL API 定向测试覆盖：

- 版本回滚能力返回 `available`、模板版本和 `can_create=true`；
- 数据库配置回滚返回 `unsupported/template_not_implemented`；
- 对不支持能力 POST 创建计划返回结构化 `409`；
- 拒绝后查询当前计划仍为 `404`，证明没有产生计划记录。

随后执行：

```powershell
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run alembic check
docker compose config --quiet
git diff --check
```

完整 pytest 回归通过；Ruff 检查通过，161 个文件格式符合规范；mypy 对 109 个源文件检查通过；Alembic 确认没有待生成迁移；Compose 配置和 Git 空白检查通过。

## 限制与下一步

- 配置回滚和依赖恢复仍只有语义，没有模板、适配器或执行权限。
- 作战室聚合接口暂未展示能力状态，前端需要单独调用新接口。
- 下一步优先设计数据库连接池配置回滚模板，但必须先冻结配置键白名单、旧值证据来源、风险等级、幂等操作和恢复验证，不能仅因为模型输出了建议就开放修改能力。

## 面试表达

扩展 Agent 输出枚举后，我增加了确定性能力层，而不是直接扩展执行器。能力层用根因、建议和假设状态共同判断模板是否可用，并把“模板存在”和“当前可以创建”分开。API 能明确展示配置回滚已被识别但尚不支持；即使客户端跳过查询直接创建，领域计划器仍会返回结构化冲突且不写数据库。这把模型语义和生产操作权限隔离开，避免错误降级和能力越权。
