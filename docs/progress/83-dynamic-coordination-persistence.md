# 83 - 动态协调计划持久化

## 目标

把通过确定性闸门的动态协调提案、运行策略和实际轮次作为可恢复、可查询、可复核的业务事实保存。基线为 main `d524db9`，开始时工作区干净。

## 设计与边界

见 [专项设计](../architecture/83-dynamic-coordination-persistence.md)。运行首次动态计划写入时固定完整策略；计划与轮次、任务、父 Worker 尝试在同一租约事务内提交。当前没有启动参数、协调模型和动态图路由，不改变旧 v1/v2 行为。

## 实际改动

- 新增运行级协调策略表和轮次计划表，使用复合外键、JSON 大小与版本约束、SHA-256 以及更新/删除拒绝触发器。
- 计划服务在可信事故范围和既有任务数量上重新编译提案，原子创建策略、轮次、专业任务、事件和计划；同内容可重放，冲突或固定历史混用明确拒绝。
- 新增只读查询，返回前从策略、事故、父尝试、轮次和任务重建计划；轮次散列抽取保持旧 JSON 编码完全兼容。
- 迁移 `0041_dynamic_coordination_plans` 不回填旧运行，已有协调事实时拒绝降级。

## 测试方法与结果

验收日期：2026-09-12。统一相关回归 51 项通过，耗时 77.18 秒；此前 24 项、36 项和 50 项定向验证均与该范围重叠，不重复计数。测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，不调用外部模型，也不升级用户 Compose 数据库。覆盖四路并发幂等、第二轮策略复用与累计任务校验、策略/计划冲突、固定历史混用拒绝、进入事务前及提交前租约失效全量回滚、更新/删除触发器、轮次与任务执行字段篡改检测、旧运行升级、降级保护、固定图和原协作 API。

Ruff 全仓检查和 618 个文件格式检查通过，mypy 检查 250 个源码文件无问题；迁移唯一 head 为 `0041_dynamic_coordination_plans`。9 篇更新文档的 UTF-8、代码围栏及 326 个本地链接检查通过，`git diff --check` 通过。

```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation/test_coordination_policy.py tests/unit/investigation/test_task_round_identity.py tests/integration/test_investigation_task_store.py tests/integration/test_coordination_plan_store.py tests/integration/test_coordination_plan_migration.py tests/integration/test_fixed_task_graph.py tests/api/test_collaboration_api.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

策略目前在首次计划写入时固定，尚未随新工作流运行创建；查询是内部应用服务，未增加 HTTP API。计划提案尚未来自模型，也没有跨专业补查、独立审查或多轮图路由。

下一片增加协调模型的结构化调用与共享预算账本，保存足以恢复的调用结果和计划来源；之后再以独立 v3 图接入动态扇出。

## 面试表达

> 我把协调计划当成需要审计的业务事实，而不是只放在 checkpoint。首次计划固定完整策略，计划、轮次、任务、事件和父 Worker 尝试在同一租约事务提交；读取时从事故、策略、轮次和任务重新编译核对。并发重放只创建一次，租约过期会整体回滚，固定历史不能被补挂成动态语义。
