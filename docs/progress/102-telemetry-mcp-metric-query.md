# 第 102 步：只读 telemetry MCP 指标查询

## 1. 切片目标

让新建 `v2 + live` 调查的指标专业任务通过独立、只读、单工具 MCP 查询本地 Prometheus，并继续使用第 101 步冻结的日志 MCP。历史直连与仅日志 MCP 运行必须按原路径恢复，任何 MCP 失败都不能回退到直连或冻结材料。

## 2. 设计与实现

- 新增 [第 102 步设计](../architecture/102-telemetry-mcp-metric-query.md)，明确兼容视图、策略矩阵、协议、网络、持久化和失败边界。
- 保留 `opsswarm-telemetry-mcp/1.0.0`、原 `/mcp` 端点和唯一 `query_service_logs` 工具不变；新增独立 `opsswarm-telemetry-metrics-mcp/1.0.0` 与唯一 `query_service_metrics`，避免历史日志客户端因工具清单漂移而拒绝恢复。
- 指标请求只接受 `order-service` 和不超过 60 个完整分钟的 UTC 窗口。客户端与服务端均拒绝 PromQL、URL、指标名、step、limit、业务身份和额外字段。
- 服务端复用 `PrometheusOnlyInvestigationDataSource.query_metrics_in_window`，保留固定健康查询、三项白名单指标、固定标签、时间、有限值和每序列 121 个样本上限。成功必须恰好返回一条规范化指标证据；错误仅暴露有限、脱敏结果。
- 客户端固定本机 `18005` 或 Compose 内 `telemetry-metrics-mcp:8000/mcp`，严格核对服务身份、唯一工具、输入输出 Schema、只读标记、契约版本、窗口、来源、观察时间、范围属性、聚合关系、数值和值域。协议偏差继续映射既有稳定 `telemetry_mcp_*` 账本错误码。
- `WindowBoundLiveDataSource` 抽出窗口指标协议。三种持久策略分别选择全直连、日志 MCP 加指标直连、日志与指标均 MCP；环境配置不能改变历史运行的数据路径。
- 新建现场 v2 固定 `task-evidence-live-mcp-v1`。迁移 `0059_telemetry_mcp_metric_policy` 只扩展既有检查约束，不回填历史运行；存在新策略运行时拒绝降级，两个历史策略均可跨越该迁移。
- Compose 新增不发布宿主端口的 `telemetry-metrics-mcp`，只连接 `telemetry` 网络并只获得 Prometheus 地址和超时。Prometheus 同时加入 `lab` 与 `telemetry`；现场 Worker 保留直连地址与启动依赖用于历史恢复，但不硬依赖 MCP 服务健康。

## 3. 状态、持久化与失败路径

指标 MCP 不保存业务状态。运行策略、UTC 截止时间、任务和尝试、调用预留、证据及 checkpoint 继续使用原持久化边界。`query_metrics` 调用成功后，证据由 `agent_task_evidence.call_id` 绑定；调用成功后、证据提交前崩溃仍按至少一次只读语义恢复。

新策略缺少任一 MCP 来源时在任务装配阶段失败。连接、超时、身份、Schema、工具失败、错误来源、窗口和值域均不能伪装为空证据；实际集成测试证明指标 MCP 不可用时不触发 Worker 的 Prometheus 直连适配器。并行节点已经成功提交的其他材料仍按原规则保留，失败任务使用新的尝试身份且不清空运行预算。

## 4. 验证

最终代码执行了以下验证：

- `uv run pytest tests/unit/telemetry_metrics_mcp -q`：46 项通过。
- `uv run pytest tests/unit/telemetry_metrics_mcp tests/unit/telemetry_mcp tests/unit/investigation/test_live.py tests/unit/investigation/test_live_task_evidence.py -q`：相关协议、历史日志与来源装配回归全部通过。
- `uv run pytest tests/unit -q`：整仓单元测试全部通过。
- `uv run pytest tests/integration/test_telemetry_mcp_policy_migration.py tests/integration/test_telemetry_mcp_live_worker.py tests/integration/test_live_task_graph.py tests/integration/test_live_task_migration.py tests/api/test_live_task_api.py tests/integration/test_migrations.py -q`：33 项通过。
- `uv run mypy src`：304 个源文件通过。
- `uv run ruff check .`：通过；`uv run ruff format --check .`：767 个文件格式正确。
- `uv run alembic heads`：唯一 head 为 `0059_telemetry_mcp_metric_policy`。
- `docker compose config --quiet` 与 `git diff --check` 通过；后者只有既有换行符提示。

另启动了 `uv run pytest -q` 整仓扩展回归；运行越过约 15% 且未出现失败，随后因既有超时类集成测试逐项耗时约 30 秒、预计继续数小时而人工停止。该未完成运行不计为通过，切片验收以上述完整单元组和 33 项相关真实集成为准。

真实 MCP 集成同时启动日志和指标两个 Streamable HTTP 子进程，并通过 PostgreSQL、checkpoint 与完整现场 v2 Worker 验证：新策略只有变更请求走原 HTTP 夹具；日志和指标调用均成功入账并绑定各自证据。另有独立用例覆盖指标 MCP 不可用、历史仅日志 MCP 和历史全直连路径。

## 5. 限制与下一步

本片只把本地演练环境的日志和指标纳入 MCP。变更服务与 GitHub 仍由内部适配器读取；MCP 不面向外部客户端，也不具备生产地址、租户鉴权、写权限或执行能力。当前固定 v2 用 MCP 验证协议与恢复边界，v3 专业任务仍读取冻结材料，通用 Worker 尚未接管 v3。

下一片进入调查手册、可信身份与经验记忆阶段，先实现一个明确事故场景的 Git 版本化调查手册和受限读取链路。经验审核接口必须等待可信身份，不能把调用方自填标识当成认证结果。

## 6. 面试表达

> 我把日志和指标拆成两个冻结的单工具 MCP 视图。这样新增指标不会改变已持久化日志策略所校验的服务身份和工具清单。新运行把两类 MCP 路径固定进取证策略，两个历史策略仍按原路径恢复。指标工具只接受固定服务与 UTC 窗口，服务端复用既有 Prometheus 白名单查询，客户端再次校验 Schema、证据范围和值域。调用预算和证据引用仍由 PostgreSQL 管理，所以 MCP 超时会形成可审计失败，不会静默回退到直连或冻结数据。
