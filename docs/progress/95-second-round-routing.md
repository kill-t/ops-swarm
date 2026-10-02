# 95 - v3 第二轮独立审查的确定性再次路由

## 目标

把第 94 步保存的第二轮开放假设和独立审查交给既有确定性规则，按冻结协调策略与前两轮真实任务数保存第 2 轮路由事实，使两轮调查拥有一致、可查询、可恢复的决策边界，并为下一步第三轮计划编译提供唯一来源。

## 设计与边界

详细设计见 [v3 第二轮独立审查的确定性再次路由](../architecture/95-second-round-routing.md)。图 v9 冻结为历史第二轮审查图，新发布的图 v10 只在补查路径的第二轮独立审查后增加确定性路由节点；首轮直接进入 `await_human` 或 `candidate_evaluation` 时仍直接结束。

第二轮继续使用 `deterministic-review-routing-v1`。规则从第二轮假设、同轮审查、运行创建时冻结的协调策略和截至第 2 轮的真实任务数重建决定，不调用模型、工具或预算账本。现有路由表按审查与业务轮次唯一，可以保存同一运行的第二条路由，无需新增业务表、字段或公开 API。

第二轮 `follow_up` 只保存仍有容量的第三轮补查意图。本片不创建第三轮计划、轮次或任务，不进入图内人工暂停、候选结论、规划或处置，不结算父运行，也不开放通用 Worker 对 v3 的领取。

## 实际改动

- 新增 `SecondRoundRoutingResult` 与独立 `follow_up_review_route` 字段；补查路径必须绑定第二轮假设与审查，另外两条首轮路径必须为空。
- 抽取按目标审查工作的路由服务内核，保留首轮 `ReviewRoutingService` 公开契约，并增加只接受第二轮审查事实的 `SecondRoundReviewRoutingService`。
- 持久化入口按业务轮次校验图能力：首轮继续兼容图 v5～v10，第二轮仅允许图 v10，避免绕过图节点让历史图写入新事实。
- 路由查询关联轮次表，并按 `round_number` 与稳定身份排序，不依赖跨尝试写入时间。
- 新增 `DynamicSecondRoundRoutingGraph` 和图 v10 工厂；最终结果从业务数据库重建两轮路由，checkpoint 只承担恢复快照。
- 图 v9 改用冻结常量，旧 checkpoint、工厂和拓扑保持原语义；新运行固定到图 v10。
- 迁移 `0052_second_round_routing` 只扩展运行图版本约束。存在图 v10 运行或第二轮及以后路由事实时拒绝降级，允许降级时恢复图 v1～v9 约束。
- 路由事实提交前中断时，新父租约重新执行纯规则；事实已提交但 checkpoint 未写时复用原记录，两种恢复都不增加外部调用或预算消耗。

## 测试方法与结果

验收日期：2026-10-02。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

图 v10 集成测试覆盖第二轮三种路由、首轮两种非补查终止、两处提交窗口恢复和存储层历史图拒绝，并通过固定相同写入时间、反转稳定身份顺序证明查询仍按业务轮次排序。领域测试覆盖第二轮轮次上限和累计任务总量耗尽。断言两条路由按轮次绑定各自假设与审查，第二轮 `follow_up` 后数据库仍只有两轮且没有第三轮计划或任务，父运行保持 `running`。兼容回归覆盖图 v9 第二轮审查、图 v5 首轮路由、v3 启动、API 当前版本和 0052 升降级。

最终验收由以下命令复现：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit -q
.venv/Scripts/python.exe -m pytest tests/api -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_second_round_routing_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_second_round_review_graph.py tests/integration/test_dynamic_routing_graph.py tests/integration/test_dynamic_workflow_start.py tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py -q
.venv/Scripts/python.exe -m pytest --collect-only -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

实际结果：全仓 695 项单元测试和 127 项 API 测试全部通过；图 v10 的 8 项真实 PostgreSQL/checkpoint 集成测试通过；图 v9 第二轮审查、图 v5 首轮路由、v3 启动、动态图迁移及全迁移链的 31 项兼容测试通过，共复核 861 项互不重复的行为。目标单元测试中 26 项覆盖路由规则与阶段结果契约，包括第二轮轮次上限和累计总任务容量耗尽。当前完整测试集合可收集 1244 项；长时间全量运行因桌面会话切换中断，未把未完成运行写成通过事实。

`ruff check` 无问题，713 个文件符合格式，Mypy 检查 289 个源文件无问题，Alembic 唯一头版本为 `0052_second_round_routing`。194 个受版本控制及本切片新增的 Markdown 文件、583 个本地链接、UTF-8 解码和代码块闭合检查通过，`git diff --check` 无错误。独立只读审查未发现生产代码阻塞问题；审查提出的累计容量与查询排序证据缺口已补成确定性回归。

## 限制与下一步

第二轮 `follow_up` 尚未编译为第三轮协调计划和待执行任务，`await_human` 与 `candidate_evaluation` 也尚未进入各自图内流程，因此有界多轮仍未闭合。

下一片只把第二轮 `follow_up` 路由的完整建议确定性编译为第三轮计划、轮次和待执行任务。第三轮执行、综合、审查、图内人工介入、候选结论、规划和父运行结算继续后置。

## 面试表达

> 多轮调查不能用最新一条路由覆盖前一轮，也不能让历史图通过复用服务写入新事实。我保留首轮公开契约，新增显式第二轮入口，并在存储层按业务轮次限制图版本。第二轮决定从同轮假设、审查、冻结策略和真实任务数重建；查询按轮次排序，业务数据库保存事实，checkpoint 只保存恢复位置。这样既复用了同一套确定性规则，又能证明图 v9 不会被当前版本常量或直接服务调用改变历史语义。
