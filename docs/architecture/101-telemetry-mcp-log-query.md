# 第 101 步：只读 telemetry MCP 日志查询

## 1. 目标与边界

本切片把新建 `v2 + live` 调查的日志专业任务迁移到独立 `telemetry-mcp` 进程，并保留既有固定窗口、调用预算、任务尝试、证据快照与恢复语义。业务价值是先建立一个可审计、失败关闭的 MCP 信任边界，再逐项迁移其余遥测能力。

本片只交付 `query_service_logs`。指标仍由 Prometheus 适配器直连，变更仍由本地变更服务适配器直连，Git、v3、人工恢复、执行工具及真实生产连接均不在本片范围内。MCP 不接受运行、任务、操作者或授权身份，也不访问业务数据库、checkpoint、模型或执行端口。

## 2. 运行与恢复契约

新建 `v2 + live` 运行固定 `evidence_policy_version=task-evidence-live-mcp-log-v1`。该策略表示日志经 MCP、指标和变更保持既有直连路径。历史 `task-evidence-live-v1` 运行继续使用 Loki 直连，恢复时不能根据当前环境静默换路。

不新增 `tool_policy_version` 列。当前 `evidence_policy_version` 已同时约束来源模式、任务材料、固定窗口和恢复装配；再增加一份并行策略会产生组合状态和重复校验。迁移只扩展该字段的允许值及现场截止时间约束，降级在存在新策略运行时明确拒绝。

工具调用继续先写 `investigation_call_reservations`，成功后由 `agent_task_evidence.call_id` 绑定规范化证据。任务、尝试、调用和证据形成完整业务审计链，不保存原始 MCP 请求、Loki 响应或 stderr，也不新增重复调用表。只读请求采用至少一次语义：调用成功后、证据提交前崩溃可能重新查询；稳定任务范围、调用身份和不可变证据提交保证恢复收敛，系统不宣称恰好一次。

## 3. MCP 协议与信任边界

服务身份固定为名称 `opsswarm-telemetry-mcp`、版本 `1.0.0`，传输使用 MCP Python SDK v2 的 Streamable HTTP，Compose 内部地址固定为 `http://telemetry-mcp:8000/mcp`。服务使用无状态 JSON 响应，并显式限制 Host；不挂载到控制面 FastAPI，也不发布宿主端口。

唯一工具 `query_service_logs` 的输入只有：

- `service_name`：只允许 `order-service`；
- `window_start`、`window_end`：必须是带时区时间并归一化为 UTC；
- 时间窗必须为正且不超过 60 分钟，Worker 实际分派仍固定为 15 分钟。

客户端和服务端均拒绝未知字段。协议不允许 URL、LogQL、limit、方向、任意工具名或业务身份参数。服务端复用 `LokiOnlyInvestigationDataSource.query_logs_in_window`，因此固定 LogQL、100 条原始日志上限、2 KiB 单行上限、标签、事件结构和双时间戳校验只有一套实现。

返回契约固定 `opsswarm.telemetry.query-service-logs.v1`，成功结果最多三条规范化 `EvidenceObservation`。空集合只表示窗口内没有匹配日志。服务端把已知输入、超时、不可用和来源响应错误映射为有限错误码；客户端再次核对输入与输出 JSON Schema、契约版本、服务、窗口、`log` 类型、`loki-fault-lab` 来源、证据属性、数量和唯一 ID。连接失败、超时、工具发现不匹配、`is_error`、缺失或非法结构化结果全部失败，不回退冻结材料或 Loki 直连。

## 4. 进程与网络

Compose 新增独立 `telemetry-mcp` 服务。它只获得受限 Loki 地址、请求超时和 MCP 监听配置，使用只读文件系统、临时目录、`cap_drop: ALL` 与 `no-new-privileges`。服务只加入 `telemetry` 网络；Loki 和采集 Loki 日志的 Alloy 同时加入该网络。现场 Worker 通过 `control`、`lab` 和 `telemetry` 分别访问业务库、指标/变更与 MCP。为恢复已经持久化为 `task-evidence-live-v1` 的历史运行，Worker 暂时保留 Compose 内 Loki 地址，且启动不硬依赖 MCP 健康；新建运行固定为 MCP 策略，MCP 不可用时由任务失败与重试语义收敛，禁止回退到直连路径。

SDK 客户端是异步 API。现有固定图仍通过受限 `ReadOnlyQueryExecutor` 在线程中执行同步来源端口，因此 MCP 适配器只在该受限线程内创建短生命周期事件循环和 SDK 客户端；它不会阻塞 LangGraph 事件循环，也不会绕开现有并发、运行时限、取消和迟到结果隔离。后续指标迁移不借本片改造整套来源端口。

当前日志策略固定校验服务版本 `1.0.0` 和仅含 `query_service_logs` 的工具清单。后续指标迁移必须为该历史策略保留兼容清单视图，或使用版本化端点与对应客户端；不能直接向当前端点追加工具，否则已经持久化的日志策略将无法恢复。

## 5. 状态、失败与审计

MCP 不持有业务状态。运行策略、任务窗口、尝试身份、调用预留、证据快照和最终报告仍以 PostgreSQL 为事实来源，LangGraph checkpoint 只保存工作流恢复位置。

错误分为有限类别：请求非法、连接不可用、调用超时、协议/工具清单不匹配、工具返回失败、结果契约非法和上游来源失败。对调用账本只记录稳定错误码，不记录异常正文、URL、响应体或 traceback。运行租约失效、取消或调用迟到时沿用现有预算和任务规则，迟到结果不能保存证据或推进流程。

## 6. 验收标准

- 契约单测拒绝未知字段、错误服务、无时区、倒置或超长窗口和超限结果。
- SDK 工具发现只返回 `query_service_logs`，并验证严格输入/输出 schema 与只读标记。
- 假 Loki 传输证明服务仍发送固定 LogQL、精确窗口、100 条上限和 backward 方向。
- 客户端对连接失败、超时、工具错误、非法 structured content、错误来源/窗口/类型及超限结果全部失败关闭。
- 真实 PostgreSQL、checkpoint 与真实 Streamable HTTP MCP 进程证明：日志任务经 MCP 取证，指标和变更保持原路径，调用预留、证据快照及专业结果可追溯。
- 调用成功后、证据提交前中断可以在新尝试恢复；历史 `task-evidence-live-v1` 仍走直连来源；MCP 失败不产生冻结成功。
- Compose 配置证明 MCP 无业务数据库/模型密钥、无宿主端口、只有受限网络和安全选项。
- 迁移升级/降级保护、相关单元与 PostgreSQL 集成测试、Ruff、Mypy、Alembic 单 head、文档链接和 `git diff --check` 通过。

## 7. 后续切片

下一片独立迁移 `query_service_metrics`，复用本片的进程、客户端信任边界和运行策略版本演进。日志切片验收不代表路线第 6 阶段的日志与指标 MCP 已全部完成。
