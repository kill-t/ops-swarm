# 69 - 模型输入、Token 与估算费用预算

## 目标

把第 68 步的次数护栏推进到完整模型输入边界：模型真正接收的系统指令、证据、报告和结构化输出 Schema 都参与预算估算；专业、综合与重试共享 Token 和估算费用额度，能够解释每次调用看到了什么、预留了多少、实际报告了多少。

## 设计与边界

先更新 [专项设计](../architecture/69-model-input-budget.md)、[数据库设计](../architecture/04-database-design.md) 和 [API 设计](../architecture/05-api-design.md)。本片使用显式 `model_budget` 配置，只允许冻结 v2；未启用的新旧运行继续走原次数/时限路径，不追溯修改历史预算。

运行开始时固定模型配置和价格，Worker 核对环境中的完整配置快照。输入采用 `utf8-json-v1` 保守估算，同时预留输出与安全余量；该数值不代表供应商精确 Token 数。Token 和费用预留不返还，实际报告超过预留时保留较大占用并停止，未知用量保持 null。SDK 输出上限实际传入请求，不通过删掉材料满足限制。

## 实际改动

- 迁移 `0033_model_input_budget` 增加运行模型预算和调用输入/预留/报告用量字段；应用输入限制 256 KiB，数据库 JSONB 限制 512 KiB，摘要各限 8 KiB。发现已有模型预算时拒绝降级删除审计事实。
- 预算契约、估算策略、模型调用适配和配置读取各自独立；提示组装从模型适配文件提取，保留原提示内容与版本。没有把 SQL、提示和 HTTP 堆入同一运行器。
- 模型适配器在消息与输出 Schema 完整形成后，通过原预算仓储原子预留次数、Token 和估算费用；图外层不重复预留。LangGraph 仍负责原图与 checkpoint，没有另建调度或工具循环。
- 使用字典 Schema 和 `include_raw` 保留原始响应，再由 Pydantic 校验业务结果。已取得响应的解析失败仍记录报告用量；核对供应商原始字段，兼容层补零或 null 都不能变成伪造的完整用量。SDK 失败而未取得可用响应时保留未知。
- Worker 固定单次返回数量为 1、关闭 SDK 自动重试，配置不匹配或兼容层尝试替换输出方式时拒绝请求。模型输入与费用耗尽沿用父运行终止、失联结算和迟到拒绝。
- 预算 GET 增加配置与占用汇总；列表不加载完整输入，新增按运行和调用读取输入快照的只读 API。`model_budget` 由启动接口显式提交，运行中不能修改配置或追加额度。Compose 传递可选配置，并保留原价格变量兼容。

## 测试方法与结果

在项目根目录使用真实 PostgreSQL 17.6 临时数据库与独立逻辑 checkpoint 库。模型测试使用真实 LangChain/OpenAI 兼容 SDK 加 HTTP 假传输，没有请求真实供应商，也未迁移现有业务库。

```powershell
uv run python -u -m pytest tests/unit/investigation/test_model_budget_policy.py tests/unit/investigation/test_budgeted_model_caller.py tests/integration/test_model_budget_store.py tests/integration/test_model_budget_graph.py tests/api/test_model_budget_api.py tests/integration/test_model_budget_migration.py tests/integration/test_migrations.py tests/integration/test_call_budget_store.py tests/integration/test_budgeted_call_runner.py tests/integration/test_budgeted_graph.py tests/api/test_call_budget_api.py -v --tb=short

uv run python -u -m pytest tests/integration/test_investigation_store.py tests/integration/test_multi_agent_worker.py tests/integration/test_checkpoint_recovery.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py tests/unit/investigation/test_parallel_graph.py tests/unit/investigation/test_worker_routing.py tests/unit/investigation/test_worker_main.py tests/unit/investigation/test_worker_usage.py tests/unit/investigation/test_settings.py tests/integration/test_fixed_task_graph.py tests/integration/test_investigation_task_store.py tests/integration/test_task_result_transactions.py tests/api/test_collaboration_api.py tests/unit/investigation/test_call_budget_policy.py -v --tb=short

uv run ruff check .
uv run ruff format --check .
uv run mypy
docker compose --env-file .env.example config --quiet
git diff --check
```

第一组 **64 passed**，第二组 **121 passed**，共 **185 项通过**。覆盖三种结构化输出的实际 SDK 请求参数、缺失/null 用量、解析失败记账、超长输入提前拒绝、完整固定图、综合预算耗尽、配置不匹配、并发最后额度、输入重放冲突、跨运行读取、未知费用、重试累计、超支事实、迁移往返及禁止丢失审计的回退检查。旧 v1、原次数预算 v2、专业结果复用、checkpoint 与 API 回归通过。

初次数据库验证期间 Docker 后台不稳定，恢复后才完成上述 PostgreSQL 结果；不把初始化失败或未完成的进程计入验收。测试暴露并修复了缺失 usage 经 SDK 表现为 null、模型单次返回数量需显式设定，以及 SDK 静默切换结构化输出方式三个边界。

Ruff 检查通过，格式检查覆盖 471 个文件，mypy 检查覆盖 195 个源文件。Compose 示例环境配置校验通过；本片 11 篇 Markdown 的结构与 212 个本地链接检查通过，`git diff --check` 通过。

## 限制与下一步

- 本片预算需要显式启用及完整模型价格配置，默认 v2 不自动增加费用上限。字节估算会偏保守，未验收真实供应商对输出限制的支持，不能把假 HTTP 传输描述为真实模型验收。
- 输入快照保留应用输入，不是供应商的内部提示或精确账单；字段 `reported_*` 只统计已报告部分，未知请求保留全部预留。缺少用量时不会生成费用为零的结论。
- 模型、端点或计价策略变化需要操作员更新 `profile_id`；该标识不证明供应商模型别名背后的权重不变。输入读取仍使用本地控制面的既有访问边界，可信身份在后续阶段交付。
- 尚未交付 ContextBuilder 的材料筛选、工作摘要、工具消息压缩、完整版本清单、动态调查或 MCP。下一片先做受限异步取证，明确实际并发、超时、取消与迟到结果处理，再逐步开放现场 v2。

## 面试表达

> 我把预算检查放在完整模型输入形成之后，连系统提示和输出 Schema 一起估算，再用 PostgreSQL 行锁给并行 Agent 原子预留次数、Token 和费用。失败或超时不会返还未知请求的额度；解析失败但已经收到 usage 时，仍然记录真实报告。输入快照和版本化估算让调用可以复核，Worker 更换配置也不能静默改变旧运行的价格。我用实际 SDK 的假传输和真实 PostgreSQL 验证这些边界，清楚区分了估算护栏、供应商报告和真实账单。
