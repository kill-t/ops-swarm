# 第 101 步：只读 telemetry MCP 日志查询

## 1. 切片目标

为新建 `v2 + live` 调查建立独立只读 telemetry MCP 进程，并让日志专业任务通过固定 Streamable HTTP 工具读取本地 Loki。历史现场运行继续按旧策略直连恢复，指标、变更和 Git 不在本片迁移。

验收边界是：协议参数不能扩权，服务没有业务数据库、模型或执行权限；运行必须固定数据路径；调用、任务尝试和证据可以追溯；连接、协议或来源失败必须失败关闭且不能回退冻结材料。

## 2. 设计与实现

- 新增 [第 101 步设计](../architecture/101-telemetry-mcp-log-query.md)，明确进程、网络、契约、持久化、恢复和失败边界。
- 锁定 MCP Python SDK v2，新增 `opsswarm.telemetry_mcp` 包。服务身份固定为 `opsswarm-telemetry-mcp/1.0.0`，只公开 `query_service_logs`。
- 输入只有 `order-service` 与显式 UTC 起止时间，窗口为正、不超过 60 个完整分钟；固定图仍只分派 15 分钟。SDK 默认参数模型会忽略未知字段，因此服务构造时显式把参数模型改为 `extra=forbid`，Schema 与运行时都拒绝 URL、LogQL、limit 和任意附加参数。
- 服务复用既有 `LokiOnlyInvestigationDataSource.query_logs_in_window`，保留固定 LogQL、100 条原始日志、2 KiB 单行、标签、事件和双时间戳校验。返回 `opsswarm.telemetry.query-service-logs.v1` 严格结构化结果，最多三条规范证据；错误响应只含有限代码和脱敏消息。
- 客户端只允许本机 `18004` 或 Compose 内 `telemetry-mcp:8000/mcp`，使用短超时并复核服务身份、唯一工具、严格 Schema、只读标记、结果版本、服务、窗口、类型、来源、数量、唯一 ID 与观察时间。任何偏差都抛出固定 `telemetry_mcp_*` 错误，不回退。
- 新建现场 v2 固定 `task-evidence-live-mcp-log-v1`；历史 `task-evidence-live-v1` 保留 Loki 直连。迁移 `0058_telemetry_mcp_log_policy` 只扩展现有策略和截止时间约束，存在新策略运行时拒绝降级。
- 调用审计复用 `investigation_call_reservations`、任务尝试和 `agent_task_evidence.call_id`。预算运行器只允许六个固定 MCP 错误码进入账本，不保存异常正文、URL、响应体或 traceback。
- Compose 新增不发布宿主端口的 `telemetry-mcp`，只连接隔离 `telemetry` 网络并只获得 Loki 地址和超时；Loki 与 Alloy 移入该网络。现场 Worker 通过固定 MCP 地址访问新运行的日志；为恢复旧 `task-evidence-live-v1` 运行而保留的 Loki 地址只由持久化策略选中，Worker 启动不硬依赖 MCP 健康，MCP 失败不会回退，指标和变更路径保持原状。

## 3. 状态、持久化与失败路径

运行策略决定恢复装配，环境变量不能把历史运行切换到新路径。日志 MCP 调用仍采用至少一次只读语义：若调用成功后、证据提交前中断，新任务尝试可以重新查询；已提交证据则按原散列和调用引用复用。

空证据表示窗口内没有匹配日志。连接失败、超时、服务身份或工具清单不匹配、工具失败、非法结构化结果、来源超限和租约失效均不能伪装为空结果或冻结成功。MCP 进程不接收运行、任务、尝试、操作者或授权身份，业务范围由 Worker 和 PostgreSQL 事实注入并复核。

## 4. 验证

本片验证覆盖：

- 协议、客户端、服务端和 Compose 隔离单测；真实 ASGI Streamable HTTP 联调；固定 Loki 查询参数与输出脱敏。
- 真实独立 MCP 子进程 + PostgreSQL + checkpoint + v2 Worker：日志经 MCP，指标和变更仍走原路径，调用与证据引用一致。
- MCP 不可用时保存稳定失败码且不产生直连或冻结证据；历史现场策略继续直连 Loki。
- 新策略升级、降级拒绝和旧策略跨迁移兼容；调用成功后证据提交窗口、预算、租约和现场 API 回归。
- Ruff、Mypy、Alembic 单 head、Compose 配置和 `git diff --check`。

2026-10-06 最终复核结果：

- `uv run pytest tests/unit/telemetry_mcp -q`：38 项通过。
- `uv run pytest tests/unit -q`：758 项通过。
- `uv run pytest tests/integration/test_telemetry_mcp_policy_migration.py tests/integration/test_telemetry_mcp_live_worker.py tests/integration/test_live_task_graph.py tests/integration/test_live_task_migration.py tests/integration/test_budgeted_call_runner.py tests/api/test_live_task_api.py tests/integration/test_migrations.py -q`：34 项通过。
- `uv run pytest "tests/integration/test_dynamic_third_round_routing_graph.py::test_v14_cannot_save_third_round_route_outside_graph" "tests/integration/test_dynamic_third_round_routing_graph.py::test_third_round_route_fact_blocks_downgrade_without_v15_run" "tests/integration/test_dynamic_workflow_migration.py::test_dynamic_policy_constraint_rejects_graph_version_mutation" -q`：3 项按真实顺序通过，验证成功降级后恢复 head，不再污染 v15 约束。
- `uv run ruff format --check .`：755 个文件格式正确；`uv run ruff check .` 通过。
- `uv run mypy src`：299 个源文件通过。
- `uv run alembic heads`：唯一 head 为 `0058_telemetry_mcp_log_policy`。
- `docker compose config --quiet` 与 `git diff --check` 通过；后者只有既有换行符提示。

另执行过整仓 `uv run pytest -q`。它在无测试失败的情况下越过此前约 21% 的迁移污染位置，随后因本机 Docker Desktop 引擎退出、Testcontainers PostgreSQL 消失而中断；该次未完成运行不计为通过。上述单元、切片集成和三项污染链均在最终代码上重新独立通过。

## 5. 限制与下一步

本片只完成日志 MCP。Prometheus 指标仍由现场 Worker 直连，变更与 Git 也未迁移；因此完整路线的“日志和指标均经 MCP”仍未验收。当前 MCP 只服务隔离 Docker Compose 演练环境，不具备生产地址、租户鉴权或外部 AI 客户端入口。

下一切片增加 `query_service_metrics`，复用固定端点、错误边界和审计链，并为新运行演进独立取证策略。当前日志客户端固定校验 `1.0.0` 身份和唯一工具清单；第 102 步必须为历史日志策略保留兼容的服务版本与工具清单视图，或提供版本化端点，不能直接向当前清单追加工具而破坏恢复。

## 6. 面试表达

> 我没有把 MCP 当成新的业务编排或审计数据库，而是把它限制为本地遥测只读边界。新运行把日志 MCP 路径固定进证据策略，旧运行继续直连恢复；工具只接收固定服务和 UTC 窗口，服务端仍复用原 Loki 白名单查询，客户端再次校验身份、Schema 和证据范围。调用预留、任务尝试和证据快照继续由 PostgreSQL 关联，所以进程崩溃后可以按至少一次语义恢复，也不会把 MCP 超时误写成空证据或回退到冻结数据。
