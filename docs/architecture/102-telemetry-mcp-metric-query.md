# 第 102 步：只读 telemetry MCP 指标查询

## 1. 目标与边界

本切片让新建 `v2 + live` 调查的指标专业任务通过只读 MCP 查询本地 Prometheus，并保留第 101 步日志 MCP、固定窗口、调用预算、任务尝试、证据快照和恢复语义。完成后，新运行的日志与指标均经 MCP，变更记录仍由现有应用适配器读取。

本片不迁移变更或 Git 能力，不改变 v3 动态图，也不引入生产地址、租户鉴权、写工具或通用 PromQL。MCP 不访问业务数据库、checkpoint、模型或执行接口。

## 2. 兼容视图与运行策略

第 101 步客户端固定校验 `opsswarm-telemetry-mcp/1.0.0` 和唯一 `query_service_logs` 工具。为保证已持久化的 `task-evidence-live-mcp-log-v1` 运行可恢复，原服务、端点、身份、工具清单和 Schema 保持不变。

指标使用独立 `opsswarm-telemetry-metrics-mcp/1.0.0` 服务身份和唯一 `query_service_metrics` 工具。Compose 地址固定为 `http://telemetry-metrics-mcp:8000/mcp`；本机测试地址固定为 `http://127.0.0.1:18005/mcp`。独立单工具视图比组合式双工具端点权限更窄，也无需复制或重新版本化已验收日志协议。

新建现场 v2 固定 `task-evidence-live-mcp-v1`。三种策略按持久化值装配：

| 策略 | 日志 | 指标 | 变更 |
| --- | --- | --- | --- |
| `task-evidence-live-v1` | Loki 直连 | Prometheus 直连 | 变更服务直连 |
| `task-evidence-live-mcp-log-v1` | 日志 MCP v1 | Prometheus 直连 | 变更服务直连 |
| `task-evidence-live-mcp-v1` | 日志 MCP v1 | 指标 MCP v1 | 变更服务直连 |

策略名不超过既有 `VARCHAR(32)`。迁移 `0059` 只扩展检查约束，不回填历史运行；存在新策略事实时拒绝降级到 `0058`。来源失败后禁止换用另一行的适配器。

## 3. 指标工具契约

工具输入只有固定的 `order-service` 以及 UTC `window_start`、`window_end`。窗口必须为正、使用完整分钟且不超过 60 分钟；Worker 仍固定查询 15 分钟。协议拒绝 URL、PromQL、指标名、标签、step、limit、运行身份和额外字段。

服务复用 `PrometheusOnlyInvestigationDataSource.query_metrics_in_window`，继续执行固定抓取健康查询和三项白名单区间查询。标签、时间、有限数值、样本顺序和每序列 121 个样本上限仍只有一套实现。

返回契约固定为 `opsswarm.telemetry.query-service-metrics.v1`。成功必须恰好包含一条 `metric`、`prometheus-fault-lab` 证据；客户端再次校验服务和窗口、观察时间、范围属性、聚合语义、三项指标值及样本数。连接、超时、身份、工具清单、Schema、`is_error` 或结构化结果的任何偏差均失败关闭。错误只写稳定 `telemetry_mcp_*` 代码，不保存异常正文、PromQL、URL 或原始响应。

## 4. 进程、网络与恢复

Compose 新增 `telemetry-metrics-mcp`，只获得 Prometheus 固定地址、超时和监听配置；它不发布宿主端口，只加入 `telemetry` 网络，并使用只读文件系统、临时目录、移除 Linux capabilities 和禁止提权。Prometheus 同时加入 `lab` 与 `telemetry`，指标 MCP 不能访问控制数据库或业务服务。

现场 Worker 保留 Loki 和 Prometheus 直连配置及启动依赖，只为恢复两种历史策略。Worker 不硬依赖任一 MCP 服务健康，新策略的 MCP 故障由既有任务重试、预算和租约规则收敛，不回退直连或冻结材料。

指标调用仍由 `investigation_call_reservations` 以 `query_metrics` 记录，证据通过 `agent_task_evidence.call_id` 绑定。只读查询采用至少一次语义；已保存材料按固定策略、窗口和内容散列复用。

## 5. 验收标准

- 原日志 MCP 的身份、唯一工具、Schema 与端点回归不变。
- 指标 MCP 只发现一个只读、幂等、封闭世界工具，并拒绝所有扩权参数。
- 服务端继续发出固定 Prometheus 查询和精确窗口，成功结果恰好一条；上游及契约错误脱敏失败。
- 客户端严格校验身份、工具 Schema、结果范围和值域，协议偏差不能变成空证据。
- 真实 MCP 子进程、PostgreSQL 和 checkpoint 证明新策略的日志与指标都经 MCP，变更保持直连，调用与证据引用一致。
- 两种历史策略按原路径恢复；指标 MCP 不可用时不调用 Prometheus 直连来源。
- `0059` 升降级保护、Compose 隔离、相关单元与集成测试、Ruff、Mypy、Alembic 单 head 和 `git diff --check` 通过。

## 6. 后续切片

日志与指标 MCP 完成后，下一片回到完整路线的 Harness 与协作能力缺口；变更和 Git 工具只有在明确事故场景与可信身份边界下再迁移。
