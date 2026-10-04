# 100 - v3 第三轮审查后的确定性最大轮次停止路由

## 目标

把第 99 步已经持久化的第三轮开放假设和独立审查，按运行创建时冻结的策略与真实任务数编译为第三份不可变路由事实，使动态调查在最多三轮后明确停止自动补查，并能在父租约更换和 checkpoint 恢复后得到同一决定。

## 设计与边界

详细设计见 [v3 第三轮审查后的确定性最大轮次停止路由](../architecture/100-third-round-terminal-routing.md)。图 v14 冻结为第三轮审查终态图；新图 v15 只在该图末尾追加 `route_third_round_review` 节点，继续复用 `deterministic-review-routing-v1` 和既有路由表。

支持充分且没有反证、证据缺口或竞争解释时路由到 `candidate_evaluation`；其他情况路由到 `await_human`。第三轮仍有完整补查建议时保存全部建议并增加 `round_limit_reached`，但 `remaining_rounds` 必须为 0，不能产生 `follow_up` 或第 4 轮计划。路由只是可查询决定，本片不执行人工等待、候选结论、规划、处置或父运行结算，也不开放通用 Worker 领取 v3。

## 实际改动

- 新增 `ThirdRoundRoutingResult` 与 `ThirdRoundReviewRoutingService`，要求第三份路由绑定同一运行、轮次、开放假设、审查身份及审查散列，并固定决定轮次 3、剩余轮次 0、路由不为 `follow_up`。
- 新增 `DynamicThirdRoundRoutingGraph` 与 v15 装配工厂，在第三轮审查后保存和复核停止路由。图 state、checkpoint metadata 和工厂入口均固定 v15；冻结的 v14 图与工厂改用独立旧版本常量。
- 把路由持久化能力门禁改为按业务轮次精确判断：第二轮只能由支持第二轮路由的图保存，第三轮只允许 v15。这样即使绕过图直接调用仓储，v14 也不能写入第三轮路由。
- 迁移 `0057_third_round_routing` 只扩展运行图约束到 v15。存在 v15 运行或关联第 3 轮及以后业务轮次的路由事实时拒绝降级；没有第三轮路由的 v14 历史运行仍可先通过本迁移降级。
- 更新数据库、总路线、Agent 专项设计、项目状态和进度索引，使“已决定”“已实现”和“后续人工/候选路径”保持分离。

## 测试方法与结果

验收日期：2026-10-04。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 业务数据库。

图 v15 集成测试覆盖清洁充分支持、有实质异议、最大轮次仍建议补查三种停止决定，第二轮提前人工或候选结束，路由事实提交前和提交后 checkpoint 前恢复，v14 直接存储门禁，以及第三轮路由事实的降级保护。断言三份路由按业务轮次稳定保存、第三份路由绑定本轮假设与审查、建议完整保留、不创建第 4 轮，模型/工具/来源/预算在确定性路由恢复中不增加，父运行保持 `running`。

最终验收执行完整 720 项单元测试和 127 项 API 测试；图 v15 的 9 项、冻结图 v14 的 8 项以及动态图迁移、完整迁移链和 v3 启动的 25 项 PostgreSQL/checkpoint 定向回归全部通过。补强的 v14 前两轮路由事实允许降级用例在完整文件结束后又单独执行一次，结果通过。

最终验收命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit -q
.venv/Scripts/python.exe -m pytest tests/api -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_third_round_routing_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_third_round_review_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_workflow_migration.py tests/integration/test_migrations.py tests/integration/test_dynamic_workflow_start.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_third_round_routing_graph.py::test_v14_cannot_save_third_round_route_outside_graph -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

实际结果：Ruff 检查通过，738 个文件符合格式；Mypy 检查 294 个源文件无问题；Alembic 唯一 head 为 `0057_third_round_routing`。204 个项目 Markdown 文件的 UTF-8 解码、620 个本地链接和代码块闭合检查通过，`git diff --check` 无错误。两个实现前独立审查分别核对了图版本冻结、持久化门禁、降级保护和测试边界；其中指出的 v14 直接存储与降级允许路径均已补充回归，主 Agent 以实际代码和真实 PostgreSQL 结果完成最终核验。

## 限制与下一步

v3 已把三轮调查收敛为不可变终端路由事实，但 `await_human` 与 `candidate_evaluation` 尚未执行，通用 Worker 仍跳过 v3。专业交互历史裁剪、只读 MCP、手册与经验、人工恢复、规划和多步骤处置也仍未完成。

下一片进入只读遥测 MCP 阶段，先定义本地 MCP 进程的最小协议、信任边界、显式时间窗、受限参数、结构化错误和调用审计，并让一个日志专业任务经 MCP 查询隔离演练环境。指标、变更与 Git 工具另片迁移，不连接真实生产环境。

## 面试表达

> 多轮 Agent 的“最多三轮”不能只写在提示词里。我让第三轮继续复用同一确定性路由器，但从业务库重建假设、审查、冻结策略和真实任务数；即使模型仍建议补查，代码也保留建议并记录轮次上限，只能转人工。路由先落业务事实，checkpoint 只记恢复位置，因此提交前后两个崩溃窗口都能幂等收敛，历史 v14 图也不能绕过能力门禁写入第三轮路由。
