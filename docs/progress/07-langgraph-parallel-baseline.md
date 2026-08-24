# 07 - LangGraph 并行调查基线

## 目标

在不改变现有持久任务和单 Agent 基线的前提下，实现显式 LangGraph 状态图：确定性协调器扇出日志、指标、变更三个专业调查节点，全部完成后由综合节点生成结构化根因，并使用同一冻结场景与单 Agent 对照。

本切片不接 PostgreSQL checkpoint、Worker 多模式选择、人工暂停、质疑循环或真实遥测平台。

## 设计与边界

### 固定任务不使用模型规划

MVP 已知必须调查日志、指标和变更。协调节点用确定性代码形成三项计划，模型只负责专业证据分析和最终综合，避免一次没有业务增益的额外模型调用。

### 专业能力通过协议注入

三个并行节点共享 `SpecialistAnalyst` 协议，但携带不同专业类型和隔离证据。综合节点依赖 `ConclusionSynthesizer`。生产适配使用 LangChain 结构化模型，离线测试注入异步假实现。

### 并行状态只使用可合并字段

证据、报告使用 reducer 累加，工具调用数使用加法 reducer。由于并行分支顺序不确定，综合前显式排序，不能把偶然调度顺序写入模型提示或评测结果。

### 继续执行确定性证据校验

专业报告只能引用本分支证据，综合结论只能引用全图证据。即使结构化输出合法，只要证据引用越界就终止图运行。

## 实际改动

- 把 LangGraph 1.x 声明为直接生产依赖。
- 新增 `SpecialistReport`、多 Agent 运行结果和单/多模式对照模型。
- 新增显式 `StateGraph`，包含协调、三个并行调查和综合节点。
- 新增专业分析与综合协议，以及 LangChain `with_structured_output` 模型适配器。
- 新增分支级和全图级证据引用校验。
- 新增实际编译图 Mermaid 输出，便于调试和架构展示。
- 新增同场景指标比较函数。
- 新增并发、扇入、图结构、证据越界和单类数据源为空测试。
- 同步 README、系统架构、技术栈和本阶段架构文档。

## 测试方法与结果

定向测试：

```powershell
uv run pytest tests/unit/investigation/test_parallel_graph.py -q
uv run ruff check src/opsswarm/investigation tests/unit/investigation
uv run ruff format --check src/opsswarm/investigation tests/unit/investigation
uv run mypy
```

结果：6 个 LangGraph 定向测试通过，覆盖三个专业节点最大并发数为 3、三路扇入后综合、实际 Mermaid 图结构、分支证据越界拒绝、综合证据越界拒绝、单类数据源为空时继续综合、单/多模式同评测契约以及 LangChain 结构化模型适配。

全仓库最终验收：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
docker compose config --quiet
git diff --check
```

结果：Ruff、格式和 mypy 严格模式通过；全仓库 107 个测试全部通过，其中 31 个测试使用 Testcontainers 连接真实 PostgreSQL 17。Compose 配置、Markdown 相对链接和 Git 空白检查均通过，本地持久 PostgreSQL 容器继续保持健康运行。

## 限制与下一步

- 当前只有一个冻结场景，单 Agent 和多 Agent 均为满分，不能证明准确率提升。
- 没有记录模型调用数、Token、成本和端到端耗时，暂时无法判断多 Agent 是否经济。
- 图尚未接入持久 Worker，多 Agent 结果不会写入 `investigation_runs`。
- 没有 PostgreSQL checkpoint，图节点无法跨进程恢复。
- 没有质疑、证据不足重试和条件循环。

下一切片优先实现独立 checkpoint 数据库和 `graph_thread_id` 关联，验证进程重启后从已完成节点继续，再接入 Worker 多模式执行。

## 面试表达

我用 LangGraph Graph API 把固定的三类调查显式并行化，而不是让模型重复规划已知任务。三条分支通过 reducer 汇合，但调度顺序不稳定，所以综合前稳定排序。每个专业节点和最终综合都校验证据引用。单场景下单 Agent 和多 Agent 都是满分，因此我只把它描述为并行编排基线，后续要用更大评测集、延迟和成本证明价值。
