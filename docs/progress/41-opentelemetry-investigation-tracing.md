# 第 41 步：OpenTelemetry 跨进程调查追踪

## 本步目标

让值班工程师能从一次控制面请求定位到调查 Worker、Agent 节点和只读工具请求，同时保持追踪与业务状态、安全审计解耦。

## 设计决策

- OpenTelemetry trace 默认关闭，显式开启后只允许发送到本机或 Compose 内 Jaeger。
- FastAPI 与 HTTPX 使用官方自动埋点，持久任务、单/多 Agent 和 LangGraph 节点使用显式 span。
- `work_items.trace_context` 只保存 W3C `traceparent` 与可选 `tracestate`；不传播 baggage。
- Worker 每次重试都从持久父上下文创建新消费 span，使失败尝试可区分且仍属于同一 trace。
- `X-Trace-ID` 只帮助调用方定位 Jaeger，不参与认证、幂等或业务状态。
- Span 属性只允许有限标识、模式、计数和异常类型；不记录密钥、提示词、模型响应、异常消息或原始证据。
- Jaeger 使用本地内存存储，容器重启丢失 trace 不影响 PostgreSQL 中的任何业务事实。

## 实现内容

1. 新增 `opsswarm.observability`，集中管理配置白名单、TracerProvider、OTLP/HTTP exporter、FastAPI/HTTPX instrumentation、W3C 上下文与安全错误标记。
2. 新增 Alembic `0022_investigation_trace_context`，为 `work_items` 增加可空 JSONB 字段和对象检查约束。
3. 开始调查路由捕获当前 FastAPI span 上下文，并与 Incident 迁移、调查运行和任务在同一事务提交。
4. 调查 Worker 领取后恢复父上下文，创建包含任务、运行、模式和尝试次数的 `CONSUMER` span；失败只记录异常类型。
5. 单 Agent、多 Agent、三个只读工具及 LangGraph 协调、三专业、综合、质疑节点增加显式 span；真实 HTTP 请求由 HTTPX instrumentation 形成子 span。
6. 控制面追踪开启时返回 `X-Trace-ID`，健康检查不生成 trace。
7. Compose 新增 Jaeger 2.20 容器，只绑定本机 `16686` UI 和 `14318` OTLP/HTTP 端口。
8. 新增无业务内容的 `python -m opsswarm.observability.smoke` 冒烟命令。
9. 更新系统架构、技术栈、数据库、API、README 和环境变量模板。

## 测试与验收

后端完整验收：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

结果：316 个文件格式检查通过，Ruff 通过，mypy 严格检查 137 个源码文件通过，459 个测试全部通过。测试覆盖地址白名单、W3C 父子关系、错误内容脱敏、FastAPI 响应头、真实 PostgreSQL 持久化、Worker 跨队列关联、工具 span、并行 Agent 节点 span，以及迁移升级、降级和 ORM 一致性。

前端回归：

```powershell
cd web
npm run typecheck
npm test
npm run build
```

结果：类型检查通过，7 个测试文件中的 14 个测试通过，生产构建成功。

真实本地链路：

```powershell
docker compose config --quiet
docker compose up -d jaeger
uv run python -m opsswarm.observability.smoke
```

结果：`ops-swarm-jaeger-1` 正常运行，OTLP/HTTP 冒烟发送成功，Jaeger 查询 API 能检索到 `opsswarm-observability-smoke` 服务。

## 为什么这样设计

异步任务不能依赖 HTTP 请求进程中的内存上下文；如果只给 FastAPI 自动埋点，Worker 会变成另一条无法关联的 trace。把最小 W3C 上下文与任务一起持久化，才能在进程重启和租约重试后保持关联。它仍然不是业务状态：关闭追踪、Exporter 故障或 Jaeger 丢数据都不能改变调查结果。

## 限制与下一步

- Jaeger 当前不做持久存储和生产安全配置。
- 后续切片已把调查 Worker 部署为冻结与现场两个常驻工作池。
- 审批、执行和验证 Worker 尚未加入跨进程 trace；应依据实际诊断价值逐条扩展。
- 常驻工作池、逐次尝试审计和作战室展示均已在后续切片完成；下一步评估新的只读变更来源。

## 面试表达

我使用 OpenTelemetry 自动采集 FastAPI 和 HTTPX，但没有止步于请求级埋点。由于 Agent 调查通过 PostgreSQL 队列跨进程执行，我把严格校验的 W3C Trace Context 随任务持久化，让每次 Worker 重试接回原 API trace；再用显式 span 展开单/多 Agent、LangGraph 节点和只读工具。遥测失败不影响业务事务，Jaeger 不替代审计库，同时通过地址和属性白名单避免敏感模型内容外泄。
