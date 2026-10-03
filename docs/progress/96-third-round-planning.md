# 96 - v3 第二轮补查路由的第三轮计划持久化

## 目标

把第 95 步保存的第二轮 `follow_up` 路由转换为可恢复的第 3 轮协调计划、业务轮次和待执行专业任务，使动态调查第一次到达冻结策略允许的最大轮次，并为下一步第三轮执行提供唯一持久来源。

## 设计与边界

详细设计见 [v3 第二轮补查路由的第三轮计划持久化](../architecture/96-third-round-planning.md)。图 v10 冻结为历史第二轮路由图，新发布图 v11 只在第二轮路由为 `follow_up` 时进入 `compile_third_round_plan`；`await_human` 与 `candidate_evaluation` 直接结束本阶段。

补查计划编译规则从“首轮到第二轮”泛化为“目标路由到下一轮”，继续完整保留审查建议，并由系统注入服务、冻结证据快照、观测窗口、任务身份和重试上限。持久化入口按来源业务轮次检查图能力：首轮派生第二轮兼容图 v6～v11，第二轮派生第三轮只允许图 v11，避免复用服务改变图 v10 的历史语义。

本片不执行第三轮任务，不新增模型或工具调用，不进入第三轮综合、审查、人工暂停、候选结论、规划或处置，也不结算父调查。现有计划、轮次与任务表已支持三轮，因此没有新增业务表、字段或公开 API。

## 实际改动

- 新增 `ThirdRoundPlanningResult`，第二轮补查路由必须携带同运行、绑定该路由、无模型来源且目标轮次为 3 的协调计划；另外两种路由必须为空。
- 抽取按审查路由工作的计划服务内核，保留首轮 `FollowUpPlanService` 公开契约，并增加只选择第二轮路由的 `ThirdRoundPlanService`。
- 计划仓储从已保存路由、来源轮次计划和固定协调策略重建第三轮提案，复用前两轮一致的冻结快照与截止时间，在有效父租约和运行行锁覆盖的单个事务中保存计划、轮次与任务。
- 新增 `DynamicThirdRoundPlanningGraph` 与 v11 工厂；v10 改用冻结常量，旧 state、checkpoint metadata、工厂和拓扑保持历史语义。
- 计划提交前中断时重新执行纯编译；事实已提交但 checkpoint 未写时按第二轮路由复用同一计划，恢复不重调模型、工具或现场来源，也不改变预算账本。
- 迁移 `0053_third_round_planning` 只扩展运行图版本约束。存在图 v11 运行或第三轮路由派生计划时拒绝降级，允许降级时恢复图 v1～v10 约束。

## 测试方法与结果

验收日期：2026-10-03。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 业务数据库。

图 v11 的 8 项集成测试覆盖第二轮三种路由、首轮两种非补查终止、计划提交前与提交后 checkpoint 前两处恢复窗口，以及第三轮计划事实的迁移降级保护。断言第三轮计划绑定第二轮路由，任务保持 `pending`，冻结快照与截止时间继承第二轮来源，父运行保持 `running`，模型调用预留仍为 11。

兼容回归覆盖图 v10 的 8 项第二轮路由测试，其中直接调用计划仓储证明旧图不能绕过拓扑创建第三轮；另覆盖 v3 启动、15 项动态图迁移、完整迁移链和当前 API 图版本，共 30 项。全仓 696 项单元测试和 127 项 API 测试通过。

最终验收由以下命令复现：

```powershell
uv run pytest tests/unit -q
uv run pytest tests/api -q
uv run pytest tests/integration/test_dynamic_third_round_planning_graph.py -q
uv run pytest tests/integration/test_dynamic_second_round_routing_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_start_v3_investigation_returns_frozen_dynamic_contract -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run alembic heads
git diff --check
```

实际结果：Ruff 检查通过，718 个文件符合格式；Mypy 检查 290 个源文件无问题；Alembic 唯一 head 为 `0053_third_round_planning`；196 个项目 Markdown 文件的 UTF-8 解码、本地链接和代码块闭合检查通过，`git diff --check` 无错误。独立只读审查未发现实现阻断项，提出的 v10 直接调用仓储门禁覆盖已补充并通过真实 PostgreSQL 回归。

## 限制与下一步

第三轮计划和待执行任务已持久化，但尚未执行，也没有第三轮工作摘要、综合、审查或停止路由。`await_human` 与 `candidate_evaluation` 仍未进入各自图内流程，通用 Worker 仍跳过 v3。

下一片只从图 v11 已保存的第三轮计划稳定分派专业任务，复用既有冻结证据范围、任务尝试、共享预算、专业工具循环与工作摘要。第三轮综合、审查、人工介入、候选结论、规划和父运行结算继续后置。

## 面试表达

> 同一套补查规则可以服务多轮，但恢复语义不能随当前常量漂移。我把计划服务抽成按路由工作的内核，同时在仓储层按来源轮次限制图版本：v10 即使直接调用仓储也不能创建第三轮。v11 在一个租约事务内保存计划、轮次和任务，checkpoint 只记录恢复位置；事实提交前后两处中断都收敛到同一业务事实，而且不增加模型、工具或预算调用。
