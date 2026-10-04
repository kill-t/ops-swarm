# 99 - v3 第三轮开放假设独立审查与恢复

## 目标

只审查第 98 步已经保存的第三轮开放假设及其综合调用使用的同源第三轮材料，保存一份绑定第 3 轮假设的 `recorded` 审查事实，使最大调查轮次具备可恢复的支持充分性、反证、证据缺口和竞争性解释。

## 设计与边界

详细设计见 [v3 第三轮开放假设独立审查与恢复](../architecture/99-third-round-review.md)。图 v13 冻结为第三轮综合终态图；新图 v14 在第三轮综合之后追加独立审查节点。三轮审查分别使用 `invocation_index=0、1、2`，第三轮另用 `third-round-open-hypothesis-review-v1` 提示和 `independent-third-round-review-v1` 角色版本，避免历史轮次调用、清单或响应被当成第三轮事实。

第三轮审查清单必须与生成当前假设的槽位 2 综合清单逐项同源，只在末尾追加当前开放假设。它不读取 checkpoint 自报材料，不混入前两轮假设、审查或路由。完整三轮路径需要 13 次模型调用，因此验收运行在创建时显式固定 13 次调用预算；既有默认上限 12 保持不变，不在执行途中追加或重置额度。

本片不执行第三轮后的最大轮次停止路由，不创建第 4 轮任务，也不进入 LangGraph 人工暂停、候选结论、规划、处置或父运行结算。审查意见不代表根因已经客观证明，也不授予执行资格。

## 实际改动

- 新增 `ThirdRoundReviewResult`，要求存在第三轮计划时必须携带与第三轮开放假设同运行、同轮次、同假设身份的审查事实；没有第三轮计划时审查字段必须为空。
- 新增 `DynamicThirdRoundReviewGraph` 与 v14 装配工厂，在第三轮综合后执行审查槽位 2 并从业务数据库核对最终事实。图 state 与 checkpoint metadata 均固定到 v14，冻结的 v13 图及工厂显式使用旧版本常量。
- 新增 `ThirdRoundHypothesisReviewer`、第三轮审查提示和上下文角色。上下文加载、构建、成功调用复用与审查仓储按图能力开放第 3 轮，并继续拒绝错误轮次、错误假设、越界证据和同槽位多条成功调用。
- 复用开放假设审查表、上下文清单和调用账本。模型成功后事实提交前失败时复用槽位 2 成功响应；事实提交后 checkpoint 前失败时直接读取并复核业务事实，不重复模型、来源访问、预算或写入。
- 迁移 `0056_third_round_review` 把新运行图扩展到 v14，并把根级审查合法槽位扩展为 `0、1、2`。存在图 v14、槽位 2 及以后审查调用，或第 3 轮及以后审查事实时拒绝降级到 `0055_third_round_synthesis`。

## 测试方法与结果

验收日期：2026-10-04。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 业务数据库。

图 v14 的 8 项集成测试覆盖第二轮三种路由、完整第三轮审查、成功调用后事实提交前恢复、事实提交后 checkpoint 前恢复、越过第三轮假设证据闭包的引用拒绝，以及运行图版本被改回 v13 后分别由槽位 2 调用和第三轮审查事实阻止降级。断言三份审查按轮次稳定保存，第三轮只使用本轮综合清单和假设，调用槽位、提示与角色版本独立，共享预算继续累计，父运行保持 `running`。

最终验收执行完整 715 项单元测试和 127 项 API 测试，并对冻结图 v13、第二轮审查、v3 启动、动态图迁移、完整迁移链及控制面图版本执行 39 项定向回归。首次负向审查测试暴露假响应子类与上一阶段共用了越界开关，越界证据因而提前进入综合；拆分测试开关后目标用例通过，生产代码未因该测试问题改变。

最终验收命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit -q
.venv/Scripts/python.exe -m pytest tests/api -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_third_round_review_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_third_round_synthesis_graph.py tests/integration/test_dynamic_second_round_review_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_start_v3_investigation_returns_frozen_dynamic_contract -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

实际结果：Ruff 检查通过，733 个文件符合格式；Mypy 检查 293 个源文件无问题；Alembic 唯一 head 为 `0056_third_round_review`。202 个项目 Markdown 文件的 UTF-8 解码、613 个本地链接和代码块闭合检查通过，`git diff --check` 无错误。两个独立范围审查分别核对了图拓扑、材料隔离、预算、恢复窗口、迁移与测试边界；主 Agent 以实际代码和真实 PostgreSQL 结果完成最终核验。

## 限制与下一步

第三轮审查事实已经持久化，但尚未根据冻结最大轮次形成不可变停止决定。`await_human` 与 `candidate_evaluation` 仍未进入各自图内流程，通用 Worker 仍跳过 v3。

下一片只从第三轮开放假设、同轮审查和冻结协调策略确定性形成最大轮次停止路由。达到第 3 轮后不得再产生 `follow_up`；人工等待、候选结论、规划和父运行结算继续后置。

## 面试表达

> 多轮独立审查不能只按操作名寻找成功调用，否则恢复时可能把前两轮意见当成第三轮结论。我把业务轮次固定到持久审查槽位，并要求第三轮审查清单与生成该假设的综合清单逐项同源，只追加当前假设。模型调用和审查事实分开落库，所以调用后与 checkpoint 前两个崩溃窗口都能幂等收敛；迁移降级同时检查图、调用和业务事实，不能靠改写图版本隐藏已经发生的第三轮审查。
