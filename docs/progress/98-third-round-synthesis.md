# 98 - v3 第三轮综合与开放假设恢复

## 目标

只综合第 97 步已经提交的第 3 轮专业结果、工作摘要和完整证据，保存一份绑定第三轮协调计划的 `pending_review` 开放假设，使最大调查轮次进入独立审查前的明确、可恢复业务状态。

## 设计与边界

详细设计见 [v3 第三轮综合与开放假设恢复](../architecture/98-third-round-synthesis.md)。图 v12 冻结为只执行第三轮专业任务的历史图；新图 v13 在第三轮专业分支汇合后增加根级综合节点。三轮综合分别使用 `invocation_index=0、1、2`，第三轮另用 `third-round-open-hypothesis-synthesis-v1` 提示和 `dynamic-third-round-synthesis-v1` 角色版本，避免前两轮成功调用、上下文和假设被误当成第三轮事实。

第三轮上下文只从业务库重建本轮协调计划、已提交工作摘要和完整证据。它不读取 checkpoint 自报材料，不混入前两轮假设、审查或路由，也不重新执行专业任务。模型仍只提出结构化假设、支持证据、反证和未解问题；系统负责状态、轮次、计划、调用及父尝试绑定。

本片不执行第三轮独立审查或确定性停止路由，不进入 LangGraph 人工暂停、候选结论、规划、处置或父运行结算，也不开放通用 Worker 领取 v3。

## 实际改动

- 新增 `ThirdRoundSynthesisResult`，要求存在第三轮计划时必须携带同运行、同第 3 轮的新开放假设；没有第三轮计划时该字段必须为空。
- 新增 `DynamicThirdRoundSynthesisGraph` 与 v13 装配工厂，在第三轮结果完成后执行槽位 2 综合并从数据库核对最终事实。图 state 与 checkpoint metadata 均固定到 v13，冻结的 v12 图显式使用旧版本常量。
- `ThirdRoundSynthesizer` 复用持久调用账本和开放假设仓储，但使用独立调用槽位、提示与角色版本。成功模型调用后、事实提交前失败时可重用槽位 2 成功响应；假设提交后、checkpoint 前失败时直接读取既有业务事实，不重复外部请求。
- 摘要上下文按轮次支持第 3 轮综合，材料加载器只对图 v13 开放第三轮 `synthesize`；独立审查仍只支持前两轮，因此本片没有越过下一切片边界。
- 迁移 `0055_third_round_synthesis` 只扩展运行图版本约束，不新增表、字段或公开 API。存在图 v13、槽位 2 及以后综合调用，或第 3 轮及以后开放假设时拒绝降级到 `0054_third_round_investigation`。

## 测试方法与结果

验收日期：2026-10-03。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 业务数据库。

图 v13 的 8 项集成测试覆盖第二轮三种路由、完整第三轮综合、成功调用后事实提交前恢复、假设提交后 checkpoint 前恢复、越过第三轮任务闭包的证据引用拒绝，以及运行图版本被改回 v12 后分别由槽位 2 调用和第三轮开放假设事实阻止降级。断言三份开放假设按轮次稳定保存，第三轮只使用本轮不同任务集合和槽位 2，上下文提示与角色版本独立，共享预算继续累计，父运行保持 `running`。

冻结图 v12、v3 启动、动态图迁移、完整迁移链及控制面图版本共 30 项回归通过；43 项阶段契约、摘要上下文与开放假设单元测试通过。完整 708 项单元测试和 127 项 API 测试通过。

最终验收由以下命令复现：

```powershell
uv run pytest tests/unit -q
uv run pytest tests/api -q
uv run pytest tests/unit/investigation/test_summary_context.py tests/unit/investigation/test_dynamic_contracts.py tests/unit/investigation/test_open_hypothesis.py -q
uv run pytest tests/integration/test_dynamic_third_round_synthesis_graph.py -q
uv run pytest tests/integration/test_dynamic_third_round_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_start_v3_investigation_returns_frozen_dynamic_contract -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run alembic heads
git diff --check
```

实际结果：Ruff 检查通过，728 个文件符合格式；Mypy 检查 292 个源文件无问题；Alembic 唯一 head 为 `0055_third_round_synthesis`。200 个项目 Markdown 文件的 UTF-8 解码、638 个链接和代码块闭合检查通过，`git diff --check` 无错误。两个实现前只读审查分别核对了图拓扑、上下文隔离、预算与迁移保护；最终只读审查未发现阻止提交的问题，主 Agent 仍以实际代码和真实 PostgreSQL 结果完成验收。

## 限制与下一步

第三轮开放假设已经持久化，但尚未形成同轮独立审查和最大轮次停止决定。`await_human` 与 `candidate_evaluation` 仍未进入各自图内流程，通用 Worker 仍跳过 v3。

下一片只让独立审查读取第三轮综合的同源材料和新开放假设，保存绑定第 3 轮的审查事实。停止路由、人工介入、候选结论、规划和父运行结算继续后置。

## 面试表达

> 多轮综合不能只复用同一个模型调用键或把所有历史摘要混在一起，否则恢复时会把前一轮响应当成当前轮事实。我把业务轮次固定到持久调用槽位，第三轮使用独立提示、上下文角色和结果字段，只允许引用第三轮已提交材料。模型调用与假设分别先落业务库，所以无论失败发生在调用后还是 checkpoint 前，新父租约都能幂等收敛；迁移降级也检查最早的槽位 2 调用事实，不能靠改写图版本掩盖已经发生的第三轮综合。
