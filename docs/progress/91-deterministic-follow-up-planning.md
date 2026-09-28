# 91 - v3 确定性补查计划与第二轮任务持久化

## 目标

把已提交 `follow_up` 路由中的全部补查意图编译为第二轮协调计划、业务轮次和待执行专业任务，并证明这些业务事实与 LangGraph checkpoint 跨提交窗口可以幂等恢复。

## 设计与边界

详细设计见 [v3 确定性补查计划与第二轮任务持久化](../architecture/91-deterministic-follow-up-planning.md)。图 v5 已有终态 checkpoint，本片把它固定为历史路由图，发布图 v6。新节点只执行纯确定性编译，不调用协调模型或工具。

编译器完整保留建议角色和问题，理由继续保存在不可变路由来源中；系统注入稳定任务键、第二轮号、事故服务、首轮冻结快照与截止时间、角色允许的最大回看窗和策略固定的尝试上限。保存时再次检查轮次、单轮与总任务额度，不能截断建议绕过上限。

只有 `follow_up` 创建第二轮；`await_human` 与 `candidate_evaluation` 直接结束图。第二轮保持 `running`，任务保持 `pending`，父运行继续 `running`。本片不执行第二轮，也不实现再次综合审查、人工暂停、候选结论、规划或处置。

## 实际改动

- 新增补查计划纯编译器和应用服务。三类专业按稳定顺序映射，日志/指标固定回看 900 秒，变更固定回看 21600 秒；当前切片只接受首轮后的第二轮。
- 扩展协调计划记录，增加互斥的 `source_review_route_id`。专用持久化入口在同一有效租约事务中重建路由、首轮计划和策略，再原子创建计划、轮次、全部任务与事件；普通计划入口不能为图 v6 创建后续轮次。
- 读取第二轮计划时从审查路由、开放假设、首轮计划、冻结范围、策略、轮次和任务重新编译，拒绝跨运行、非补查路由、内容漂移或来源不一致。
- 新增 `FollowUpPlanningResult`、`DynamicFollowUpGraph` 与工厂，发布 `dynamic-investigation-graph-v6`。图 v5 固定为历史路由图；综合能力允许 v3～v6，审查能力允许 v4～v6，路由能力允许 v5/v6，补查计划只允许 v6。
- 计划保存前中断时恢复重新执行纯编译；计划、轮次和任务提交后 checkpoint 前中断时复用同一计划，不重复任务或外部调用。首轮计划恢复改为复核第 1 轮来源并允许已经存在后续计划。
- 迁移 `0048_follow_up_planning` 增加同运行复合外键、路由来源唯一与双来源互斥约束，并扩展运行图版本；存在图 v6 或路由派生计划时拒绝降级。

## 测试方法与结果

验收日期：2026-09-28。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

补查编译单元测试覆盖完整三角色映射、稳定顺序、系统窗口、非补查路由和非首轮来源拒绝。图 v6 集成测试覆盖三种路由、第二轮待执行状态、计划保存前中断、计划提交后 checkpoint 前中断及跨租约恢复；迁移测试覆盖新来源约束、图 v6 降级保护和旧图回退链。图 v1～v5、启动/API 和 Worker 隔离参加兼容回归。

最终全仓 1199 项测试通过。Ruff 全仓检查通过，格式检查覆盖 691 个文件；mypy 检查 285 个源码文件无问题；Alembic 唯一 head 为 `0048_follow_up_planning`。文档 UTF-8 与本地链接检查、`git diff --check` 通过。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_follow_up_planning.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_follow_up_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_workflow_migration.py tests/integration/test_follow_up_plan_migration.py tests/integration/test_migrations.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

第二轮任务尚未执行，因此仍没有第二轮证据、专业结果、工作摘要或再次综合审查。`await_human` 尚未进入持久等待或 LangGraph interrupt，`candidate_evaluation` 也不是最终根因、规划或执行资格。通用 Worker 继续跳过 v3。

下一片按已持久化第二轮计划动态分派专业任务，复用当前任务运行器、冻结证据和原运行共享预算，并验证部分完成与 checkpoint 提交窗口恢复；再次综合审查和第三轮循环继续后置。

## 面试表达

> 我把模型提出的补查建议当作受限意图，而不是可直接执行的任务。确定性编译器完整消费建议，并由系统注入服务、冻结快照、时间窗和尝试上限；计划、轮次和任务在一个租约事务中提交，且以路由来源一一绑定。图 v6 只创建第二轮待执行任务，checkpoint 丢失时从不可变业务事实恢复，不会重复调用模型或创建任务。这样多轮调查的动态性来自模型，资源和状态正确性仍由代码与数据库约束保证。
