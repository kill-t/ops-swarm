# 67 - 固定图接入专业任务、结果与进度事件

## 目标

让固定并行多 Agent 图实际产生专业任务事实，在最终综合前即可查询报告与证据；业务提交与 checkpoint 保存分离时，能够核对并复用已有结果，而不依赖聊天或 checkpoint 推断任务已完成。

## 设计与边界

先更新 [专项设计](../architecture/67-fixed-graph-task-integration.md)、[数据库设计](../architecture/04-database-design.md) 和 [API 设计](../architecture/05-api-design.md)，再实现迁移、运行器和接口。此次提供显式 `workflow_version=v2` 的冻结固定图，保留默认 v1；新入口必须同时指定 `mode=multi_agent`、`data_source_mode=frozen`。没有开放现场 v2、动态分派或新增模型角色。

每次父 Worker 尝试装配独立图实例，把运行身份与租约注入任务运行器，不把连接或令牌写入 checkpoint。分派绑定冻结场景内容摘要和场景时间，冻结材料不能被描述为事故创建时的现场证据。结果与任务状态、进度事件通过同一业务事务提交，图恢复和父运行完成时均核对专业事实。

## 实际改动

- 迁移 `0031_fixed_graph_tasks` 增加运行版本、专业结果表及事件表。结果绑定运行/任务/尝试的复合身份，应用限制单结果 128 KiB、20 条证据；数据库另设 JSON 对象与 256 KiB 上限。结果只能随成功尝试创建，重复结算校验内容一致，不能覆盖既有报告。
- `FixedTaskRuntime` 组织准备、尝试获取、结果复用与结算；`TaskGraphFactory` 按父尝试装配依赖；工作流负责固定扇出与综合。结果契约、映射、收尾、事件、SSE 和查询各自保留内聚职责，没有把 SQL 或提示逻辑堆入运行器。
- 专业结果中的角色、引用、来源、快照身份及大小经过校验。阶段结果按需查询，最终 Evidence Board 仍在父调查完成时生成。三个专业结果不完整或与图输出不一致时，v2 不得提交最终成功。
- 新协作事件在父任务锁内分配运行内递增游标，只有业务提交后才可读取。新增结果读取、事件列表和 SSE 端点；将原 SSE 的通用游标轮询、心跳提取为共享传输组件，旧事故时间线继续兼容。
- 父尝试失败或租约接管时，同事务收尾尚未完成的专业尝试；还有次数则任务回到 pending，否则 failed。父运行终止时，未完成轮次随之收敛，已成功专业结果保留。接管现在会提前递增子任务版本，相关旧测试按新状态边界更新。
- checkpoint 保存工作流版本，缺标记的旧状态按 v1 处理；双向跨版本恢复拒绝。支持只有初始输入的 checkpoint；场景内容变更后拒绝原任务重放。业务已提交但图节点随后失败时，恢复直接读取已有专业结果，不重复该专业的取证和模型分析。

## 测试方法与结果

在仓库根目录使用 Testcontainers 的真实 PostgreSQL 17.6；业务库与 checkpoint 库分离，模型与数据源使用假模型和冻结场景。没有调用真实模型，没有向用户现有业务数据库应用迁移。

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy

uv run pytest tests/unit/investigation/test_task_contracts.py tests/unit/investigation/test_task_results.py tests/integration/test_investigation_task_store.py tests/integration/test_task_result_transactions.py tests/integration/test_fixed_task_graph.py tests/api/test_collaboration_api.py tests/integration/test_migrations.py tests/unit/control_plane/test_sse.py

uv run pytest tests/integration/test_investigation_store.py tests/integration/test_multi_agent_worker.py tests/integration/test_checkpoint_recovery.py tests/integration/test_sse_stream.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py tests/unit/investigation/test_parallel_graph.py tests/unit/investigation/test_worker_routing.py tests/unit/investigation/test_worker_main.py

git diff --check
```

本片、任务底座、迁移及 SSE 单元测试 **67 passed**；既有调查仓储、v1 Worker、checkpoint、API 与 SSE 回归 **70 passed**，两组共 **137 项通过**。覆盖专业失败恢复、业务先提交、初始输入恢复、缺失业务结果拒绝、版本冲突、冻结快照变更、父失败/租约耗尽收尾、迟到结果、事务回滚、并发幂等、跨运行隔离、API 参数校验和 Last-Event-ID 重放。迁移测试验证模型元数据一致与升级/回退。

Ruff 检查通过，格式检查覆盖 438 个文件；mypy 检查 180 个源文件通过。另用 Python 检查本次十份 Markdown 的 UTF-8、代码块闭合、代码块外单个一级标题及 198 个本地链接，全部通过；`git diff --check` 无错误。不依赖真实现场演示或前端测试作为本片验收：前端、Compose 配置及现场适配器未变。

## 限制与下一步

- 当前是显式选择的冻结固定 v2 图，默认请求和现有界面仍使用 v1；协作工作台尚未展示新增接口。API 版本字段是开发入口，完整体验留到界面切片。
- 有任务生命周期运行器不代表完整 Harness 已交付；运行级共享预算、调用前预留、角色/工具/上下文/预算策略清单与受限异步现场取证仍待开发。下一片先完成预算及其失败语义，随后接现场 v2。
- 专业尝试共享父租约，不是独立 Worker 队列。GET 返回最后提交的状态，不主动接管或续租。模型请求发生后、业务结果提交前崩溃仍可能重复调用；不宣称模型恰好调用一次，也不把未知用量记成零。
- 最终 `tool_call_count` 保留有效专业查询数的原口径，不是跨重试的外部调用账本；逐次工具调用记录、预算预留与完整策略版本清单在 Harness 切片交付。
- 已提交专业结果只是调查材料，审查、审批与执行资格仍由原有边界控制。动态协调、专业工具循环、分层记忆、ContextBuilder、MCP 和图内人工暂停恢复均未因此完成。

## 面试表达

> 我把 LangGraph 的恢复快照和 Agent 的业务事实分开管理。专业 Agent 完成后，会在一个 PostgreSQL 事务里提交报告、证据、任务状态和进度事件；如果随后 checkpoint 写入失败，恢复节点会先复用已提交结果。父 Worker 接管租约或失败时，同事务收尾子任务，旧结果无法继续推进业务。这个切片已验证固定冻结场景的恢复与查询，下一步再补调用前共享预算和动态工具调查。
