# 93 - v3 第二轮综合与开放假设恢复

## 目标

把第 92 步已经提交的第二轮专业结果收敛为独立、可查询、可恢复的新开放假设，使补查材料不再停留在分散的专业摘要中，同时保持首轮假设、审查和路由来源链不可变。

## 设计与边界

详细设计见 [v3 第二轮综合与开放假设恢复](../architecture/93-second-round-synthesis.md)。图 v7 冻结为历史第二轮执行图，新发布的图 v8 只在 `follow_up` 第二轮全部完成后执行一次根级综合；`await_human` 和 `candidate_evaluation` 仍直接结束。

两轮综合复用 `synthesize` 操作，但以业务轮次固定调用槽位：首轮为 0，第二轮为 1。上下文准备与预算预留事务都按槽位重新选择并复核目标轮次计划、任务、证据、结果和工作摘要。第二轮使用独立提示和角色版本，不能混入首轮材料，也不能改写首轮开放假设。

现有 `investigation_open_hypotheses` 已按运行和轮次唯一，因此本片没有增加业务表、字段或公开 API。新假设继续使用 `open-hypothesis-v1` 和 `pending_review`，绑定第二轮计划及成功综合调用。父调查仍为 `running`。本片不执行第二轮独立审查、第三轮、人工暂停、候选结论、规划或处置，也不开放通用 Worker 对 v3 的领取。

## 实际改动

- 新增 `SecondRoundSynthesisResult`，以独立 `follow_up_hypothesis` 字段绑定第二轮计划；非补查路由不得携带该事实。
- 上下文加载、清单重建、调用结算和模型调用器支持显式综合槽位。图 v8 的第二轮综合固定使用槽位 1，首轮及旧图继续使用槽位 0。
- 新增第二轮综合提示 `follow-up-open-hypothesis-synthesis-v1` 与角色版本 `dynamic-follow-up-synthesis-v1`，保留首轮输入和清单字节语义。
- 综合服务按轮次复用成功调用和开放假设；持久化层复核图能力、计划轮次、调用槽位、完整材料集合与证据闭包。开放假设查询按业务轮次稳定返回。
- 新增 `DynamicSecondRoundSynthesisGraph` 与图 v8 工厂。第二轮专业分支全部汇合后才综合；最终结果从业务数据库复核，不以 checkpoint 代替业务事实。
- 模型成功但假设尚未提交时，新父租约复用槽位 1 的成功调用；假设已提交但节点 checkpoint 未写时，恢复直接复用第二轮假设，两种情况都不重复外部请求。
- 迁移 `0050_second_round_synthesis` 只扩展运行图版本约束到 v8，存在 v8 运行时拒绝降级到 `0049`。图 v1～v7 保留各自恢复语义。

## 测试方法与结果

验收日期：2026-09-29。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

图 v8 集成测试覆盖三种首轮路由、两次综合调用槽位与材料隔离、两轮开放假设稳定查询、父运行非终态，以及模型成功后事实提交前和事实提交后 checkpoint 前两个恢复窗口。组合回归覆盖图 v3 首轮综合、图 v7 第二轮执行、v3 启动/API 默认版本和 0050 升降级。

最终验收结果在本切片提交前由以下命令复现：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_open_hypothesis.py tests/unit/investigation/test_summary_context.py tests/unit/investigation/test_dynamic_contracts.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_second_round_synthesis_graph.py tests/integration/test_dynamic_second_round_graph.py tests/integration/test_dynamic_synthesis_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

实际结果：目标单元测试 27 项、目标 PostgreSQL 集成测试 33 项以及完整回归 1221 项全部通过；`ruff check` 无问题，703 个文件符合格式，Mypy 检查 287 个源文件无问题，Alembic 唯一头版本为 `0050_second_round_synthesis`。190 个受版本控制及本切片新增的 Markdown 文件、569 个本地链接、UTF-8 解码和代码块闭合检查通过，`git diff --check` 无错误。

## 限制与下一步

第二轮新假设仍是 `pending_review`，尚未经过独立审查或确定性路由，因此不能描述为最终根因、规划输入或执行资格。当前两轮路径仍未形成有界循环，另外两种路由也没有进入图内人工等待或候选评估。

下一片只对第二轮开放假设执行同轮材料约束下的独立审查并持久化审查事实。再次路由、第三轮、人工介入、候选结论、规划和父运行结算继续后置。

## 面试表达

> 第二轮综合不能简单复用首轮根级调用身份，否则同一父尝试会命中相同幂等键，恢复时也可能把两轮材料混在一起。我让业务轮次决定调用槽位，首轮用 0、第二轮用 1，并在上下文准备和预算事务里重新验证目标轮次的完整任务、摘要与证据。两轮假设仍写入同一张按轮次唯一的表，模型成功和业务事实提交之间、业务事实和 checkpoint 之间都能跨父租约恢复。这样既保留了旧图和首轮调用的不可变语义，也把第二轮补查变成了可审查的新事实。
