# 94 - v3 第二轮开放假设独立审查与恢复

## 目标

把第 93 步保存的第二轮开放假设交给独立审查角色，只使用该假设来源综合调用的同轮补查材料，形成可查询、可恢复的第二份审查事实，为后续确定性再次路由提供可信输入。

## 设计与边界

详细设计见 [v3 第二轮开放假设独立审查与恢复](../architecture/94-second-round-review.md)。图 v8 冻结为历史第二轮综合图，新发布的图 v9 只在 `follow_up` 路径的第二轮开放假设后增加审查节点；`await_human` 和 `candidate_evaluation` 仍直接结束。

两轮审查复用 `review` 操作和现有审查表，但以业务轮次固定调用槽位：首轮为 0，第二轮为 1。第二轮使用 `follow-up-open-hypothesis-review-v1` 提示和 `independent-follow-up-review-v1` 角色版本。上下文清单必须与第二轮综合清单逐项相同，再追加唯一的第二轮开放假设，不能混入首轮摘要、假设、审查或路由材料。

审查意见仍只记录支持充分性、接受的支持、反证、缺口、竞争性解释、建议方向和有限补查意图。本片不再次路由、不创建第三轮、不进入人工等待、候选结论、规划或处置，不结算父运行，也不开放通用 Worker 对 v3 的领取。

## 实际改动

- 新增 `SecondRoundReviewResult` 与独立 `follow_up_hypothesis_review` 字段；补查路径必须绑定第二轮假设，另外两条路径必须为空。
- 抽取按轮审查内核，保留 `IndependentHypothesisReviewer` 首轮公开契约，新增只投影第二轮材料的 `SecondRoundHypothesisReviewer`。
- 上下文准备、预算预留、调用结算、成功结果复用和事实来源复核都传递实际审查槽位；第二轮上下文仅图 v9 可用。
- 审查事实读取会按目标槽位枚举成功调用并要求来源唯一，避免已有事实绕过后来插入的重复成功调用；查询改按业务轮次稳定排序。
- 新增 `DynamicSecondRoundReviewGraph` 和图 v9 工厂。最终结果从业务数据库重建两轮事实，checkpoint 只承担恢复快照。
- 迁移 `0051_second_round_review` 同时扩展图版本与根级审查槽位约束；`review` 只允许 0/1，`coordinate` 仍只允许 0，存在图 v9 或非零审查调用时拒绝降级。
- 模型成功但第二轮审查尚未提交时，新父租约复用槽位 1 的成功调用；审查已提交但 checkpoint 未写时，恢复直接复用原事实，两种情况都不重复外部请求。

## 测试方法与结果

验收日期：2026-09-29。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

图 v9 集成测试覆盖三种首轮路由、两次审查调用槽位、第二轮综合与审查材料清单同源、跨轮任务隔离、查询轮次顺序、父运行非终态、非法槽位 2，以及模型成功后事实提交前和事实提交后 checkpoint 前两个恢复窗口。另以跨父尝试重复成功调用验证查询与恢复入口拒绝来源冲突。兼容回归覆盖图 v4 首轮审查、图 v8 第二轮综合、v3 启动和 0051 升降级。

最终验收由以下命令复现：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_dynamic_contracts.py tests/unit/investigation/test_hypothesis_review.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_review_graph.py tests/integration/test_dynamic_second_round_review_graph.py tests/integration/test_dynamic_workflow_migration.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest --collect-only -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

实际结果：目标单元测试 20 项通过；图 v4/v9 与动态图迁移目标回归 24 项通过，其中独立覆盖图 v9 和审查槽位 1 两种降级拒绝。完整回归在生产代码定稿后收集的 1230 项全部通过；独立复核只再强化测试和架构文字，未修改生产代码，新增迁移保护测试通过后当前集合为 1231 项。`ruff check` 无问题，708 个文件符合格式，Mypy 检查 288 个源文件无问题，Alembic 唯一头版本为 `0051_second_round_review`。192 个受版本控制及本切片新增的 Markdown 文件、575 个本地链接、UTF-8 解码和代码块闭合检查通过，`git diff --check` 无错误。

## 限制与下一步

第二轮审查事实尚未经过确定性再次路由，因此审查建议不会创建第三轮、进入人工等待或成为候选结论。两轮路径仍未形成完整有界循环。

下一片只根据第二轮假设、审查事实和冻结容量策略保存第二轮确定性路由。第三轮计划与任务、图内人工介入、候选结论、规划和父运行结算继续后置。

## 面试表达

> 两轮独立审查不能共用同一个根级调用身份，也不能让第二轮读取首轮判断。我把审查调用槽位固定为业务轮次减一，并让上下文从目标轮次计划和假设来源综合调用重建材料。审查清单必须与同轮综合清单完全一致，只多当前假设；持久化读取还反查同槽位是否只有唯一成功调用。这样第二轮既能共享原运行预算，又能在两个提交窗口跨租约恢复，同时保留图 v8 和首轮审查的历史语义。
