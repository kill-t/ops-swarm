# 本地 Compose 启动与现场调查冒烟

2026-11-06，以 `2ba62ba` 为代码基线，从当前源码构建前后端镜像。在独立 Compose 项目 `opsswarm-smoke` 使用新的业务卷、独立 checkpoint 库和本机端口，数据库会话采用 UTC。原项目业务卷未用于本次写入。

- 新库正式迁移到 `0099_live_reflow_handoff`，初始化 checkpoint 表。Web、API、订单服务、Redis、Prometheus、Loki 和两个 MCP 服务均启动。执行 `scripts/verify-mvp.ps1`，发布 500、连接池错误、Redis 不可用三个演练都按预期触发并恢复。同源代理与前端详情路由通过。
- 实际 MCP 返回 3 条日志和 1 条指标证据。告警首次提交返回 201，重放返回 200 且事故 ID 相同。停止并重新启动本次隔离环境的 Web、API、数据库，原事故仍可读取。
- 临时装配现场 v3 Worker，模型请求只发往容器网络内的脚本模型。首次试跑在专业分析失败。脚本模型改为按工具名提供独立调用 ID 后，新事故通过真实日志 MCP 到达 `await_human`，消耗 5 次模型预留和 2 次工具预留，持久交接事件恰好一条。初次重试还出现 checkpoint 反序列化白名单告警，进程中断恢复需单独验收。
- API 切换可信模式后，浏览器同源接口通过操作者认证、协作导航和预算读取。无凭据请求被拒绝。经 Web 代理连接 SSE 并用游标重连，收到下一条事件。页面路径 HTTP 200，本次未逐项执行浏览器表单操作。

复现时使用 `compose.yaml` 与项目缓存中的独立覆盖文件 `.pytest_cache/compose-local-smoke/compose.smoke.yaml`、`compose.live.yaml` 和 `smoke.env`。基本启动目标为 `web change-service alloy telemetry-mcp telemetry-metrics-mcp verification-worker`。使用 `verify-mvp.ps1 -ControlBaseUrl http://127.0.0.1:18080 -WebBaseUrl http://127.0.0.1:15173 -OrderBaseUrl http://127.0.0.1:28001` 复测三类故障时，控制面需处于测试用的 `legacy_local` 模式。运行日志留在该缓存目录。

本次验收覆盖本地启动、三类故障探针、保留卷重启、真实取证与现场 v3 单轮交接。可信人工续接、审批与执行、失败后再调查及跨进程恢复仍需单独串联。面试可以展示实际采集的证据与持久交接，再明确指出脚本模型用于控制实验行为。
