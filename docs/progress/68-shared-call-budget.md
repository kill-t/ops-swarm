# 68 - 运行级共享调用预算与预留账本

## 目标

让冻结 v2 固定图在外部操作前占用持久额度，避免三个专业节点、综合节点和 Worker 重试各自计算预算。运行失败或接管后保留已消耗的次数和原始截止时间，为后续动态工具调查建立可核对的调用边界。

## 设计与边界

先更新 [专项设计](../architecture/68-shared-call-budget.md)、[数据库设计](../architecture/04-database-design.md) 和 [API 设计](../architecture/05-api-design.md)。本片仅控制模型次数、工具次数、每专业任务工具次数与运行总时限；Token、费用与模型窗口预算需要完整输入边界，留到下一片。现有用量遥测继续保留，预留账本不替代供应商 usage。

新 v2 运行创建时固定 `call-count-v1` 策略，默认 12 次模型、18 次工具、每任务 6 次工具和 300 秒。时限从首次领取计时，包含退避和重试等待。旧 v1 和迁移前 v2 的策略为 null，保持未预算路径，不为历史调用补造零消耗；新 v2 提交 null 仍使用默认策略。

一次预留提交后才允许调用；同一预留身份再次出现不获得第二次执行资格。失败、取消和未知记录都继续占用次数，所以预留后、请求前崩溃可能保守消耗一次额度。这里保证预算不会因重试清零，不承诺物理请求恰好执行一次或费用为零。

## 实际改动

- 迁移 `0032_shared_call_budget` 增加运行策略 JSONB 和调用预留表。复合外键限定运行、父尝试和专业尝试归属；运行内序号与幂等键唯一，硬配置上限使单运行最多保存 160 条预留。
- 从任务仓储提取共享的有效租约事务。预算仓储在父工作项行锁内验证身份、聚合占用和预留额度，提交前复核租约；调用在事务外执行，调用前再次检查剩余时限。
- `BudgetedCallRunner` 组织预留、限时等待、执行与结算，通过异步任务上下文隔离专业身份。策略、契约、数据库映射、仓储、查询、API 和父收尾分别负责内聚职责，图仍只负责节点与路由装配。
- 三种固定取证、三种专业模型分析和综合模型均接入预算。专业结果复用不再次取证；专业技术重试建立新尝试并累计原任务工具次数。预算路径关闭模型 SDK 隐含重试，Worker 层重试仍受同一账本约束。
- 额度或时限耗尽时父运行直接持久失败，不再进入自动重试；父失败、接管和完成事务同时收尾悬挂预留。旧租约或新父尝试都不能把上次失联预留覆盖为成功。
- 启动 API 支持受限 `call_budget`；新增只读预算查询，返回固定策略、首次开始时间、截止时间和调用历史，不暴露租约令牌，没有重置或追加额度接口。

## 测试方法与结果

测试在仓库根目录运行，使用 Testcontainers 临时 PostgreSQL 17.6 和独立逻辑 checkpoint 库；模型、数据源使用假实现和冻结场景。没有向现有业务库应用迁移，也没有进行真实模型验收。

环境开始时 Docker 未运行，数据库测试停在初始化。启动 Docker 后曾等待后台就绪，中止这次未完成的测试进程后重新运行。为诊断等待，第一组通过 `pytest.main` 开启即时输出和 90 秒堆栈诊断；实际命令如下：

```powershell
uv run python -u -c "import faulthandler, pytest; faulthandler.dump_traceback_later(90, repeat=True); raise SystemExit(pytest.main(['tests/unit/investigation/test_call_budget_policy.py', 'tests/integration/test_call_budget_store.py', 'tests/integration/test_budgeted_call_runner.py', 'tests/integration/test_budgeted_graph.py', 'tests/api/test_call_budget_api.py', 'tests/api/test_collaboration_api.py', 'tests/integration/test_fixed_task_graph.py', 'tests/integration/test_investigation_task_store.py', 'tests/integration/test_task_result_transactions.py', 'tests/integration/test_migrations.py', '-v', '--tb=short']))"

uv run pytest tests/integration/test_investigation_store.py tests/integration/test_multi_agent_worker.py tests/integration/test_checkpoint_recovery.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py tests/unit/investigation/test_parallel_graph.py tests/unit/investigation/test_worker_routing.py tests/unit/investigation/test_worker_main.py tests/unit/investigation/test_worker_usage.py tests/unit/investigation/test_settings.py --tb=short

uv run ruff check .
uv run ruff format --check .
uv run mypy
git diff --check
```

第一组 **66 passed**，覆盖并发争抢、预留幂等、跨事故/角色拒绝、失败和失联不退还、租约接管、取消与两类超时、模型及工具额度耗尽、等待超过截止时间、专业结果复用、新请求空值默认策略、历史未知语义和迁移往返。原初始 checkpoint 恢复测试改用新的父 Worker 尝试，与真实重试身份一致；同父尝试的相同预留不能再次执行。

第二组 **78 passed**，覆盖既有调查仓储、v1 并行图与 Worker、checkpoint、启动/查询 API、模型配置和用量记录；两组共 **144 项通过**。Ruff 检查通过，格式检查覆盖 **455 个文件**，mypy 检查 **190 个源文件**通过。前端、Compose 配置与现场适配器未变，本片没有重新执行完整现场发布验收。

另用 Python 检查本次 **10 份 Markdown、204 个本地链接**，UTF-8 读取、代码块闭合、代码块外单个一级标题及项目内链接均通过；`git diff --check` 无错误。

## 限制与下一步

- 下一片在完整模型输入边界上实现输入/输出 Token 限额与估算费用预留，保存价格和策略依据，继续保留未知用量；随后单独验收受限异步现场取证及现场 v2。
- 该账本针对固定图操作，一次专业尝试的每种操作只分配一个预留身份；未来专业工具循环必须显式演进调用序号契约，不能重复使用现有键执行更多查询。
- 当前 v2 仍只使用冻结材料，没有提供动态协调、完整上下文清单、MCP、记忆、图内人工暂停恢复或预算追加。新增 API 尚未接前端工作台。
- Docker 启动时自动恢复了旧演练的两个调查 Worker 和一个验证 Worker；为释放本机资源已停止这三个容器，未修改其数据，也未重新启动演练任务。

## 面试表达

> 我给多 Agent 调查增加了运行级共享调用预算：并行角色和重试先在 PostgreSQL 中预留额度，再调用模型或工具。进程在请求附近崩溃时，我们不猜测有没有扣费，而是保留未知状态与预算占用；新 Worker 接管也不能清零。通过真实 PostgreSQL 测试验证了并发不会超发、恢复不会重复领取同一次执行资格，以及总时限耗尽后停止调查。这一片先完成调用次数和时限，Token 与费用预算继续在完整模型输入边界实现。
