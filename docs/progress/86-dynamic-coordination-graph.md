# 86 - v3 动态图首轮协调与 checkpoint 恢复

## 目标

把 v3 启动契约、第 84 步协调模型调用和第 83 步计划持久化串进独立 LangGraph 节点，并证明首轮计划能够跨 Worker 租约和 PostgreSQL checkpoint 恢复，且不会误用旧图状态。

## 设计与边界

详细设计见 [v3 动态图首轮协调与 checkpoint 恢复](../architecture/86-dynamic-coordination-graph.md)。图当前只推进首轮协调并返回 `CoordinationPlanRecord`，不会伪造最终调查结果。通用 Worker 继续跳过 v3；测试显式领取 v3 仅用于验证真实租约、业务事实和 checkpoint 的组合恢复。

## 实际改动

- 新增独立 `DynamicCoordinationGraph`，装配 `START → coordinate → END`，绑定运行 UUID、v3、固定图版本和稳定 thread ID；首次入口与恢复入口明确分离。
- checkpoint metadata 与状态同时保存并核对工作流/图版本；输入阶段兼容读取 `__start__`，旧 v1/v2、缺失字段或其他图版本在规划调用前拒绝；返回计划还要用当前租约从业务库重建并完全比较。
- 新增 v3 装配工厂，复核多 Agent 冻结来源、运行/线程/租约、协调策略、上下文、证据、预算和模型 profile，再以当前租约创建规划服务。租约不写入 checkpoint。
- 协调上下文与共享预算兼容 v2/v3，清单记录真实运行版本；v3 额外核对启动时固定的图、协调和摘要上下文策略，v2 行为保持兼容。
- 增加真实 PostgreSQL 集成测试，覆盖首次计划和 checkpoint、重复首次入口、终态 checkpoint 读取、两个持久化崩溃窗口、新租约恢复、旧工作流和错误图版本拒绝。

## 测试方法与结果

验收日期：2026-09-13。最终全仓 1135 项测试通过；数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。新图 5 个场景和相关协调、预算、固定图、旧 checkpoint 定向回归共 54 项，均包含在全仓结果内，不重复计数。

Ruff 全仓检查和 635 个文件格式检查通过，mypy 检查 254 个源码文件无问题；Alembic 唯一 head 仍为 `0043_dynamic_workflow_start`，176 份 Markdown 的 UTF-8、一级标题、代码块及 504 个本地链接和 `git diff --check` 均通过。首次新图测试还暴露 checkpoint 恢复后计划值会成为普通字典，结果边界因此增加 Pydantic 重校验；修正后定向和全仓测试全部通过。

```powershell
.venv/Scripts/pytest.exe -q tests/integration/test_dynamic_coordination_graph.py
.venv/Scripts/pytest.exe -q tests/integration/test_dynamic_coordination_graph.py tests/integration/test_coordination_planner.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_checkpoint_recovery.py tests/integration/test_fixed_task_graph.py tests/integration/test_call_budget_store.py tests/integration/test_budgeted_call_runner.py tests/unit/investigation/test_context_builder.py tests/unit/investigation/test_worker_routing.py
.venv/Scripts/pytest.exe -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

## 限制与下一步

v3 尚未执行计划中的专业任务，也没有动态综合、独立审查、第二轮补查或最终结算。业务数据库与 checkpoint 不原子；已成功结算的调用和已提交计划能够幂等复用，但不能把未知网络结果描述为模型 exactly-once。

下一片实现 v3 首轮动态任务分派与专业结果恢复：只执行计划实际选择的 1～3 个任务，接入已有任务尝试、取证、专业循环、共享预算和工作摘要，并验证动态 fan-out 的部分失败恢复。通用 Worker 要到动态图具备综合、审查和终态校验后再开放 v3 领取。

## 面试表达

> 我把动态协调接入了独立 v3 LangGraph 图，并让 checkpoint 同时携带工作流和图版本；恢复时连初始 `__start__` 状态也严格核对。租约不进 checkpoint，新 Worker 必须用新租约重建规划服务。业务库与 checkpoint 之间的两个崩溃窗口分别靠成功调用复用和计划幂等复核收敛。因为图还没有最终结论，我没有让通用 Worker 领取 v3，避免把首轮计划完成错误结算成调查成功。
