# 70 - 受限异步取证与迟到结果隔离

## 目标

修复多 Agent 固定图在异步节点中直接运行同步 HTTP 查询的问题，使日志、指标与变更取证能够实际重叠，并在来源阻塞时保持事件循环响应；同时防止超时和重试制造不断增长的后台线程请求。

## 设计与边界

先保存 [专项设计](../architecture/70-bounded-async-evidence.md)，同步 [系统架构](../architecture/02-system-architecture.md)。复用原同步适配器的来源、参数、超时与响应校验，通过异步只读端口和专用线程桥接接入固定图。

Worker 进程的多 Agent 调查共享最多 3 个未完成查询，默认等待上限 30 秒。容量满时直接拒绝；取消协程或等待超时不会提前释放正在运行的底层查询槽位。线程只执行取证，模型调用、业务持久化与图推进仍在原协程和事务中处理。没有增加另一套 Agent 调度器。

本片不新增数据库表、迁移、API 或人工取消命令。现场 v1 和冻结 v1/v2 多 Agent Worker 已装配桥接；单 Agent 的历史 LangChain 工具路径保持兼容。现场 v2 仍受原路由限制，不能将本片描述为已完成现场任务恢复。

## 实际改动

- `async_source.py` 定义异步查询端口与同步来源桥接，保持固定日志、指标和变更能力；非冻结来源缺少执行器时在装配阶段拒绝。冻结评测可以直接读取内存材料。
- `query_execution.py` 仅负责只读查询的线程提交、容量、等待、取消及资源收尾。提交前检查容量，不向无界队列持续堆积；复制 ContextVars，保留专业身份与 trace 上下文。
- 固定图和任务运行器改为等待异步取证，v2 继续先预留预算再提交查询；正常返回后仍核对租约及业务状态。Worker 主入口与 v2 图工厂共享同一执行器，避免每次重试创建新池。
- 超时/取消保留 unknown 账本和已占次数；迟到成功或异常仅完成底层资源收尾，不再进入模型分析。关闭时拒绝新查询并取消未启动工作，不阻塞事件循环等待运行中线程。

## 测试方法与结果

单元测试使用阻塞事件和 HTTP 假传输，不调用真实模型或遥测平台。集成测试使用真实 PostgreSQL 17.6 临时数据库和独立 checkpoint 库，不迁移现有业务数据库。

```powershell
uv run python -u -m pytest tests/unit/investigation/test_query_execution.py tests/unit/investigation/test_async_source.py tests/unit/investigation/test_parallel_graph.py tests/unit/investigation/test_live.py tests/unit/investigation/test_loki.py tests/unit/investigation/test_prometheus.py tests/unit/investigation/test_changes.py tests/unit/investigation/test_github_changes.py tests/unit/investigation/test_tools.py tests/unit/investigation/test_agent.py tests/unit/investigation/test_worker_main.py tests/unit/investigation/test_worker_routing.py -v --tb=short

uv run python -u -m pytest tests/integration/test_async_evidence_graph.py tests/integration/test_fixed_task_graph.py tests/integration/test_budgeted_graph.py tests/integration/test_model_budget_graph.py tests/integration/test_budgeted_call_runner.py tests/integration/test_checkpoint_recovery.py tests/integration/test_multi_agent_worker.py -v --tb=short

uv run ruff check .
uv run ruff format --check .
uv run mypy
git diff --check
```

第一组 **144 passed**，第二组 **30 passed**，共 **174 项通过**。新增 18 项单元测试与 5 项 PostgreSQL 集成测试，覆盖线程重叠、事件循环响应、上下文隔离、取消/超时后容量保留、迟到异常回收、来源错误、预算前置与租约失效。旧 v1/v2 图、模型预算、专业结果复用和 checkpoint 回归通过。

首次 PostgreSQL 测试在 Docker 后台连接管道失效时未能初始化，恢复 Docker 后重新运行，初始化失败不计入验收。Ruff 检查通过，格式检查覆盖 479 个文件，mypy 检查覆盖 197 个源文件；本片 7 篇 Markdown 的结构与 187 个本地链接检查通过，`git diff --check` 通过。

## 限制与下一步

- 取消线程等待不是终止远端请求。运行中的同步请求仍依赖原客户端超时收尾；极端卡死最多占满固定槽位，关闭解释器也可能等待线程，需要进程重启处理。本片不承诺强杀任意同步操作。
- 并发测试证明事件循环能在阻塞查询期间继续调度，并未测量真实现场吞吐或模型质量提升。单 Agent 兼容路径没有新增运行级取证槽位护栏。
- 下一片先定义现场 v2 的固定观测窗口、证据快照身份、提交与恢复复用规则，再接入任务事实；现场来源失败不能回退到冻结材料。ContextBuilder、动态调查、经验和 MCP 继续逐片开发。

## 面试表达

> 多 Agent 图看起来并行，不代表取证真的并发。我发现同步 HTTP 查询仍在事件循环里运行，于是把只读取证放进进程内共享的受限线程桥接。关键是取消等待后不立即释放运行中查询的槽位，否则重试会堆积后台请求。我用事件同步验证三类查询重叠，再用真实 PostgreSQL 验证调用前预算、租约失效及迟到结果拒绝，明确区分协程取消、底层请求完成和业务结果提交。
