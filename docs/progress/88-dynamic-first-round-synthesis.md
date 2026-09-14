# 88 - v3 首轮协调综合与开放假设持久化

## 目标

让 v3 在首轮动态专业任务全部成功后，只综合本轮实际选择的 1～3 个任务材料，形成可追溯但尚待独立审查的开放假设，并证明模型调用、业务事实与 checkpoint 跨提交窗口可以幂等恢复。

## 设计与边界

详细设计见 [v3 首轮协调综合与开放假设持久化](../architecture/88-dynamic-first-round-synthesis.md)。图 v1 和图 v2 已有各自的终态 checkpoint，本片保留原拓扑，为综合节点启用图 v3。开放假设使用自由文本表达，保留支持证据、反证和未解问题；系统固定 `pending_review`，不映射旧 `RootCause`，不携带处置建议或执行资格。

独立审查、补查任务编译、第二轮路由、最终结算和通用 Worker 接入不在本片。综合完成后父运行继续保持 `running`。

## 实际改动

- 新增 `DynamicSynthesisGraph` 与装配工厂，在动态协调和首轮专业 `Send` 后执行根级 `synthesize_open_hypothesis`。图状态与 checkpoint metadata 双重固定 `dynamic-investigation-graph-v3`，返回前从业务库重建计划、专业结果和开放假设。
- v3 的 `summary-context-v1` 精确读取首轮计划实际选择的任务、证据和工作摘要，使用独立提示与角色版本；v2 仍要求固定三个专业任务。首轮通过 `round_number=1` 定位，使后续新增轮次不会改变已保存综合调用的来源范围。
- 新增 `OpenHypothesisProposal`、系统注入状态后的 `OpenHypothesis` 及共享语义校验。支持与反证引用必须唯一、互斥并属于本轮输入，未解问题不能为空；提案和持久记录重建复用同一校验规则。
- 新增不可变 `investigation_open_hypotheses` 表、只读查询和写入服务。记录绑定同运行的协调计划、父调查尝试和成功根级综合调用，保存版本、状态、完整 JSON、散列及 UTC 时间；数据库拒绝更新和删除。
- v3 成功综合调用在共享预算账本保存版本化输出。模型成功后假设提交前崩溃时复用调用结果；假设提交后 checkpoint 前崩溃时直接复用业务事实。旧图 v1/v2、旧综合调用和 Worker 版本隔离保持兼容。
- 迁移 `0045_dynamic_synthesis` 增加新表和复合来源约束，扩展调用结果及图版本检查；存在图 v3 或开放假设时拒绝降级，图 v2/v1 的既有降级语义继续验证。

## 测试方法与结果

验收日期：2026-09-14。数据库测试使用 Testcontainers PostgreSQL 17.6 和独立 checkpoint 数据库，模型请求使用 SDK 假传输，没有访问外部模型，也没有升级用户 Compose 数据库。

新增动态图 5 个场景通过，覆盖 1/2/3 个实际角色、终态恢复、模型成功后假设提交前中断，以及假设提交后 checkpoint 前中断。迁移 6 个场景通过，开放假设契约 6 项通过；迁移专项 62 项以及图 v1/v2、摘要上下文、启动 API 与 Worker 隔离定向回归均通过。

最终全仓 1153 项测试通过。Ruff 全仓检查和 654 个文件格式检查通过，mypy 检查 264 个源码文件无问题；Alembic 唯一 head 为 `0045_dynamic_synthesis`。9 个本片涉及的 Markdown/README 文件通过 UTF-8 与本地链接检查，`git diff --check` 通过。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_open_hypothesis.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_synthesis_graph.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_workflow_migration.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_synthesis_graph.py tests/integration/test_dynamic_first_round_graph.py tests/integration/test_dynamic_coordination_graph.py tests/integration/test_dynamic_workflow_start.py tests/api/test_control_plane_api.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_synthesis_graph.py tests/integration/test_summary_context_graph.py tests/integration/test_summary_context_reservations.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

## 限制与下一步

当前开放假设仍是模型提出且待审查的调查中间事实。它不能进入旧 Evidence Board 的根因枚举，也不能触发规划、审批或执行；“待审查”不代表根因已被证明。v3 仍只使用冻结场景，通用 Worker 继续不领取 v3。

下一片实现独立审查 Agent 与审查事实持久化：审查当前假设的支持充分性、反证、证据缺口和竞争性解释，并保留建议方向。补查任务编译、确定性路由、停止条件和父运行终态继续分别验收。

## 面试表达

> 我把动态首轮综合放进新的图 v3，避免用新拓扑恢复已经结束的图 v1/v2 checkpoint。综合上下文不是固定枚举三角色，而是从首轮持久计划重建实际 1～3 个任务及其摘要和证据。模型只能提出自由文本假设和证据关系，系统注入待审查状态，旧根因与执行资格保持隔离。成功调用与开放假设分别落库，并通过调用复用和业务事实复用覆盖两类跨库提交窗口，所以恢复不依赖聊天历史，也不声称分布式事务或恰好一次请求。
