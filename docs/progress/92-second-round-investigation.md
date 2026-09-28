# 92 - v3 第二轮专业任务执行与恢复

## 目标

执行第 91 步已经持久化的第二轮补查计划，使定向补查产生独立、可查询的任务尝试、证据、专业结果和工作摘要，并证明并行部分失败以及业务结果与 checkpoint 跨库提交窗口能够幂等恢复。

## 设计与边界

详细设计见 [v3 第二轮专业任务执行与恢复](../architecture/92-second-round-investigation.md)。图 v6 已有补查计划终态 checkpoint，本片把它固定为历史图并发布图 v7。新图只从重新验证通过的第二轮计划分派任务，不允许 checkpoint 或调用方扩大角色、服务、窗口、快照和尝试范围。

首轮和第二轮可能包含同一专业角色，因此图状态以独立 `follow_up_results` 保存第二轮结果。任务运行器要求调用方显式传入期望轮次，并同时核对运行、轮次 ID、轮次号、任务键、完整分派和冻结快照，避免错误节点跨轮执行。

第二轮继续使用首轮冻结范围、原运行共享预算、现有专业工具循环、任务证据和工作摘要。任务全部成功后第二轮状态结算为 `completed`，父调查运行仍保持 `running`。`await_human` 和 `candidate_evaluation` 不执行第二轮。本片不再次综合审查、不创建第三轮、不实现人工暂停或规划，也不开放 v3 Worker。

## 实际改动

- 新增 `FollowUpExecutionResult`，要求 `follow_up` 的第二轮结果角色、数量和快照与持久计划完全一致；另外两种路由必须同时没有第二轮计划和结果。
- 扩展 `DynamicTaskRuntime`，由图节点显式提供期望轮次；所有历史动态图同步传入首轮号，保持原校验语义。
- 新增 `DynamicSecondRoundGraph` 与图 v7 工厂。第二轮按计划任务键稳定并行分派，复用专业分析、证据保存、调用预算、结果与工作摘要结算，并在最终返回前从业务库逐项复核结果。
- 部分任务已成功时，跨父租约恢复复用成功业务结果，只为失败任务增加新尝试；结果已经落库但节点 checkpoint 尚未写入时，恢复不重复工具或模型调用。
- 迁移 `0049_second_round_investigation` 只扩展运行图版本约束，允许图 v1～v7 共存；存在图 v7 运行时拒绝降级到 `0048`。没有新增业务表、字段或公开 API。
- v1～v6 图和 checkpoint 保留历史恢复语义；v3 启动默认固定图 v7，通用 Worker 的支持集合仍只有 v1/v2。

## 测试方法与结果

验收日期：2026-09-28。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

图 v7 集成测试覆盖三种路由、持久计划唯一分派、两轮同角色隔离、第二轮证据/结果/摘要与轮次结算、共享预算累计、部分并行失败接管，以及结果提交后 checkpoint 前中断恢复。单元测试覆盖阶段结果契约与显式轮次校验；组合回归覆盖图 v1～v6、启动/API、Worker 隔离和迁移升降级。

最终全仓 1213 项测试通过，组合回归通过。Ruff 全仓检查通过，格式检查覆盖 698 个文件；mypy 检查 286 个源码文件无问题；Alembic 唯一 head 为 `0049_second_round_investigation`。188 个 Markdown 文件的 UTF-8 与本地链接检查、`git diff --check` 均通过。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_dynamic_contracts.py tests/unit/investigation/test_dynamic_task_runtime.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_second_round_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_follow_up_graph.py tests/integration/test_dynamic_routing_graph.py tests/integration/test_dynamic_review_graph.py tests/integration/test_dynamic_synthesis_graph.py tests/integration/test_dynamic_first_round_graph.py tests/integration/test_dynamic_coordination_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_workflow_migration.py tests/integration/test_dynamic_workflow_start.py tests/unit/investigation/test_worker_routing.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

第二轮专业任务虽然已经完成，但尚未综合为绑定第二轮计划的新开放假设，也没有再次独立审查或路由。因此当前仍不是有界多轮闭环，`await_human` 也没有进入持久等待，`candidate_evaluation` 不代表最终根因、规划或执行资格，父运行继续保持 `running`。

下一片只综合第二轮已提交工作摘要和证据，持久化绑定第二轮协调计划的新开放假设，并验证模型调用与业务事实提交窗口恢复。再次审查、第三轮、人工介入、规划和父运行结算继续后置。

## 面试表达

> 我没有把第二轮补查当成首轮结果列表的延伸，因为同一专业角色可能跨轮重复出现，而专业结果本身不携带轮次身份。图 v7 使用独立状态通道，并让任务运行器显式校验期望轮次和完整持久分派。任务仍复用同一运行的预算、冻结范围和审计链；部分并行失败时只重试失败任务，业务结果先于 checkpoint 提交时也不会重复外部调用。这样动态补查具备了可查询、可恢复的真实执行边界，同时没有提前混入第二轮综合或最终处置。
