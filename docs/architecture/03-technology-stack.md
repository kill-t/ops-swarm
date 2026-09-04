# OpsSwarm 技术栈选型

## 1. 选型原则

- 优先支持显式状态、持久化、失败恢复和审计。
- 优先选择求职市场常见、文档成熟、可在本地运行的技术。
- 每个依赖必须有清晰职责，不为简历关键词引入重复框架。
- 生产依赖在 `pyproject.toml` 声明兼容范围，由 `uv.lock` 固定可复现版本。
- 新的主要版本必须通过现有测试和评测后再升级。

## 2. 后端与数据

| 类别 | 选择 | 职责与原因 |
| --- | --- | --- |
| 语言 | Python 3.12 | 与 AI 生态兼容，支持现代类型语法，本机已安装 |
| 依赖管理 | uv | 解析和安装速度快，使用锁文件保证环境可复现 |
| Web 框架 | FastAPI | 类型驱动、异步友好、自动生成 OpenAPI，适合 API 与 SSE |
| 数据校验 | Pydantic 2 + pydantic-settings | 统一 API、Agent 结构化输出和环境配置校验 |
| ORM | SQLAlchemy 2 异步模式 | 显式事务、成熟映射和 PostgreSQL 支持 |
| 驱动 | Psycopg 3 | SQLAlchemy 与 LangGraph PostgreSQL checkpoint 均可使用其异步能力 |
| 迁移 | Alembic | 只管理业务数据库，迁移可审查、可回滚 |
| 业务数据库 | PostgreSQL 17 | 保存业务事实、审计、事件和持久任务，支持 JSONB、部分索引和行锁 |
| 任务调度 | PostgreSQL 租约队列 | MVP 无需额外消息中间件，支持 `SKIP LOCKED` 和故障接管 |
| 实时进度 | SSE + REST | 进度单向推送，审批使用 REST；比 WebSocket 更符合实际交互 |
| 受控 HTTP | HTTPX2 客户端 | 执行固定本地白名单请求或只读遥测查询；关闭环境代理继承并限制地址和超时 |

## 3. Agent 技术栈

| 技术 | 使用位置 | 不使用的位置 |
| --- | --- | --- |
| LangChain 1.x | 模型适配、工具定义、结构化输出、单 Agent 基线 | 业务状态机、数据库事务、安全授权 |
| LangGraph 1.x | 状态图、并行节点、循环、checkpoint、暂停恢复 | 业务事实与审计的唯一存储 |
| langchain-openai | 默认 OpenAI 兼容模型适配器 | 不把供应商配置散落在领域代码中 |
| langgraph-checkpoint-postgres | 长任务 checkpoint | 不替代 Incident、证据、审批和执行表 |
| LangSmith | 可选本地开发追踪 | 不作为系统运行和自动化测试前提 |

### 3.1 为什么适合使用 LangChain

OpsSwarm 的单 Agent 基线需要模型选择工具、读取工具结果并返回稳定结构。LangChain 可以减少不同模型接口、工具协议和结构化输出的重复代码，因此适合快速开发，也适合在简历中展示“模型与业务解耦”。

但完整事故响应包含确定性状态、并行调查、失败循环、人工审批和长时间暂停，不能只依赖通用 Agent loop。LangGraph 才是主编排运行时，业务状态机仍由领域代码控制。

推荐的面试表达：

> 我没有为了使用 LangChain 把所有代码包装成 Chain。LangChain 只解决模型、工具和结构化输出适配；LangGraph 解决可恢复编排；领域层和白名单执行器解决一致性与安全。这样既提高开发速度，也避免框架侵入核心业务。

### 3.2 模型配置

单 Agent 基线已经使用以下环境变量定义可选真实模型配置：

| 变量 | 含义 |
| --- | --- |
| `LLM_API_KEY` | 模型服务密钥 |
| `LLM_MODEL` | 默认模型名称 |
| `LLM_BASE_URL` | 可选的 OpenAI 兼容地址 |
| `LLM_TEMPERATURE` | 默认使用确定性较高的低温度配置 |
| `LLM_THINKING_MODE` | 是否沿用供应商默认思考模式，或显式启用/关闭 |
| `LLM_STRUCTURED_OUTPUT_METHOD` | 在 `json_schema`、`json_mode` 和 `function_calling` 中显式选择供应商支持的结构化输出方式 |
| `LANGSMITH_TRACING` | 是否启用可选 LangSmith 追踪 |

配置对象只能出现在基础设施层，领域层通过模型网关协议调用，不读取环境变量。不同供应商的思考与结构化输出能力不能靠模型名称隐式猜测；两项兼容配置会进入真实评测的模型参数哈希，防止不同请求语义被合并为同一实验。

## 4. 测试与质量

| 工具 | 用途 |
| --- | --- |
| pytest | 单元测试与集成测试入口 |
| pytest-asyncio | 异步应用服务、数据库和 Worker 测试 |
| HTTPX2 | FastAPI/Starlette TestClient 的当前 HTTP 客户端依赖 |
| Testcontainers | 启动隔离的真实 PostgreSQL 测试实例 |
| Ruff | 格式与静态规则检查 |
| mypy | 关键领域模型和接口的静态类型检查 |
| coverage.py | 观察关键分支覆盖，不追求无意义的总覆盖率数字 |

SQLite 不用于替代 PostgreSQL 集成测试，因为部分唯一索引、JSONB、`TIMESTAMPTZ`、行锁和 `SKIP LOCKED` 都是关键行为。

Psycopg 3 异步模式在 Windows 上不支持默认 Proactor 事件循环。控制面正式入口、Worker、Alembic 和 pytest 在 Windows 明确使用 Selector 事件循环，Linux 部署保持平台默认实现；这属于基础设施兼容策略，不进入领域代码。Windows 宿主调试控制面时应通过 `uv run python -m opsswarm.control_plane.main` 启动；Linux Compose 容器使用平台默认事件循环，可直接由 Uvicorn 启动同一个应用对象。

Starlette 当前仍兼容旧版 HTTPX，但已将其标记为弃用；项目测试使用 HTTPX2，避免在新工程中引入已知迁移债务。

Agent 测试分三层：

1. 使用假模型与固定工具结果验证图路由和状态更新。
2. 使用冻结故障数据集离线评测根因、证据和计划。
3. 显式开启时才运行真实模型端到端评测，并记录模型、提示版本、Token、成本和延迟。

当前已经把 `langgraph` 和 `langgraph-checkpoint-postgres` 声明为直接生产依赖，实现 Graph API 并行调查和 `AsyncPostgresSaver` 跨连接恢复。并行测试不能依赖分支完成顺序，所有进入综合提示的证据和报告都按稳定业务标识排序。checkpoint 反序列化使用显式项目类型白名单，不允许数据库内容触发任意 Python 类型加载。

持久 Worker 通过业务枚举在 LangChain 单 Agent 与 LangGraph 多 Agent 之间确定性路由，并按持久化的 `frozen` 或 `live` 数据源模式部署为两个 Compose 常驻工作池。工作池使用可中断空闲轮询，处理硬超时小于租约，容器宽限期大于处理超时；收到停机信号后不再领取新任务。多 Agent 处理器先查询线程 checkpoint 是否存在，再决定首次运行或恢复；Agent 模式和数据源模式都不进入模型提示词，也不由模型选择。

每次 Worker 租约领取会在同一事务创建 `investigation_attempts`。LangChain 回调以单次尝试为边界收集模型请求数和供应商用量元数据，超时后的迟到回调不会跨尝试串账。价格快照随尝试持久化，费用由仓储层重新计算；元数据不完整时保留已观察 Token，但费用明确为空，不把未知成本解释为零。

单 Agent 使用 LangChain `ToolStrategy` 输出结构化结论。三个必需只读工具均完成后，`wrap_model_call` 中间件会把下一轮可用工具收窄为结构化结论工具，并强制工具选择；这既保留真实工具循环，也防止兼容模型对已完成查询反复调用。默认调查工具预算为 6 次，递归限制仍作为第二道终止边界。

Agent 输出先通过 Pydantic `InvestigationArtifacts` 校验，再由 SQLAlchemy 在当前任务租约事务中写入规范化 Evidence Board。PostgreSQL 复合外键阻止专业报告跨调查引用证据；这类业务完整性不依赖 LangGraph checkpoint 或模型自觉。

根因质疑使用版本化纯 Python 规则，不增加第二个 LLM 裁判。LangGraph 负责把规则节点放入可恢复序列，Pydantic 校验输出结构，PostgreSQL 保存假设和逐证据关系；业务规则仍可在毫秒级纯单元测试中覆盖。

处置计划当前使用确定性模板建立安全基线：Pydantic 只接受白名单动作和各自的精确参数模型，纯 Python 版本化风险策略重算风险；新计划使用 `action-risk-v3`，读取时仍支持历史 `action-risk-v1` 与 `action-risk-v2`。PostgreSQL 保存不可变版本并用检查约束阻止高风险动作绕过审批。审批请求与决定使用 SQLAlchemy 事务和 PostgreSQL 行锁同步计划、Incident 与时间线，以 SHA-256 检测内容漂移，并通过数据库检查约束保证终态审计完整；LangChain 和 LangGraph 都不参与审批有效性判断。未来 LangChain 可以生成相同 `ActionPlan` 契约的候选计划，但不能替代风险策略或审批。

白名单执行使用 HTTPX2 访问固定本地管理路径，并以 Pydantic 校验有限响应。SQLAlchemy/PostgreSQL 在环境操作前保存全局幂等键与短租约，完成时用新读取的 UTC 时间重新校验租约，再原子提交执行终态、Incident 迁移和时间线。该设计承认数据库与 HTTP 之间没有分布式事务，通过幂等的目标状态设置实现安全重放，而不是错误宣称物理恰好一次。

恢复验证复用 HTTPX2，但只向项目内 Prometheus 发送代码中预注册的三条只读查询。响应由 Pydantic 校验，并额外验证唯一序列、标签、值域和样本新鲜度；PostgreSQL 行锁只覆盖最终样本追加和 Incident 判定，不跨越网络调用。测试通过依赖注入时钟覆盖 60 秒窗口，无需真实等待，也不修改正式规则。Prometheus 适配器继续实现同一 `VerificationAdapter` 契约，没有改变领域终态语义。

调查指标、日志和变更同样复用 HTTPX2，但使用各自独立的 `InvestigationDataSource` 适配器。指标适配器先执行固定 `up`，再执行固定 `query_range`，把每条序列最多 121 个点压缩为一条 Evidence；日志适配器执行固定 LogQL，对最多 100 条 JSON 日志做标签、类型和双时间戳校验，再按故障聚合；变更适配器只访问固定本地服务、固定路由和固定服务名，对最多 100 条记录做精确联合类型、时间窗、重复与截断校验。实时调查结果不计算冻结场景的准确率，评测仍通过 `FrozenScenarioDataSource` 运行。

## 5. 可观测性

| 技术 | 目标 |
| --- | --- |
| structlog 26 | 故障服务有限字段 JSON 事件日志 |
| Prometheus 3.13 LTS、prometheus-client | 故障服务指标抓取与固定恢复查询 |
| OpenTelemetry Python 1.44 / instrumentation 0.65b0 | 跨 API、Worker、Agent 节点和工具调用的 trace |
| Jaeger 2.20 | 本地 OTLP/HTTP 接收、内存存储和 trace UI |
| Loki 3.7、Grafana Alloy 1.19 | 本地日志存储、固定文件采集与只读调查查询 |
| LangSmith | 可选查看模型输入输出和图执行，不保存业务唯一事实 |

日志和 trace 使用 `incident_id`、`run_id`、`work_item_id` 和 `trace_id` 关联。FastAPI 与 HTTPX 使用官方自动埋点，任务消费和 Agent 节点使用显式 span；跨进程只持久化 W3C `traceparent`/`tracestate`。密钥、审批令牌、提示词、模型响应和原始敏感数据不得写入日志或 span。当前仍由 structlog/Loki 承担日志，OpenTelemetry 只启用稳定 trace 信号。

## 6. 故障环境与前端

| 类别 | 选择 | 引入阶段 |
| --- | --- | --- |
| 容器编排 | Docker Compose | 阶段 1 |
| 被管服务 | gateway-service、order-service | 阶段 1 起逐步增加 |
| 故障依赖 | PostgreSQL 17、Redis 7.4、redis-py 6 | 数据库切片、真实 Redis 切片 |
| 遥测 | Prometheus 3.13 LTS、Loki 3.7、Alloy 1.19 | 指标恢复验证与日志调查切片 |
| Trace 后端 | Jaeger 2.20 内存模式 | OpenTelemetry 调查追踪切片 |
| 变更事实 | FastAPI、PostgreSQL 17 | 固定发布、配置和依赖维护记录及只读调查切片 |
| 前端 | Vue 3、TypeScript、Vite | 时间线与审批阶段 |
| 前端状态 | Pinia、Vue Router | 与首个 UI 切片一起引入 |
| 前端测试 | Vitest、Vue Test Utils、jsdom | store、SSE 边界与组件测试 |

首个 UI 切片已经使用 Vue 3.5、TypeScript 6、Vite 8、Pinia 4 和 Vue Router 5。开发服务器通过同源 `/api` 代理连接本机控制面，不因此扩大 FastAPI CORS。Compose 生产镜像固定使用 Node 24.20 构建，并由非 root Nginx 1.29.8 托管静态资源、history fallback 和同源 API/SSE 代理。当前 `vue-tsc` 与 TypeScript 7 的编译器入口不兼容，因此锁定经过类型检查和生产构建验证的 TypeScript 6；升级必须再次通过相同验收。

Incident 作战室把固定六阶段摘要与增长型调查尝试分开读取。Pinia 只用聚合快照中的当前调查 ID 请求尝试历史，并通过 Incident ID、运行 ID 和请求 generation 防止迟到响应串线。费用展示继续遵守后端完整性语义，不把空值格式化为零。

Redis 只属于故障演练环境，不承担控制面队列或业务事实存储。订单服务通过 redis-py 经项目内固定 TCP 代理访问 Redis 7.4；代理仅控制连通性并主动清理已有连接，不解析命令，也不拥有 Docker Socket。

## 7. 未选择的方案

| 方案 | 暂不选择的原因 |
| --- | --- |
| 全微服务 | MVP 边界仍在变化，会增加部署、调用和一致性成本 |
| API 进程内后台任务 | 进程重启会丢失正在运行的调查 |
| Celery/Redis 控制面队列 | 增加基础设施，并容易与“Redis 不可用”演练目标混淆 |
| WebSocket | 当前主要是单向事件流，SSE 更易断线重放和运维 |
| SQLite 开发数据库 | 无法覆盖项目依赖的 PostgreSQL 语义 |
| 直接让 LLM 执行 Shell | 权限不可控、难以幂等、无法可靠审计 |
| 一次设计所有物理表 | Agent 工作流尚未通过实验验证，过早锁定字段会增加迁移成本 |

## 8. 版本策略

- Python 固定 3.12 小版本兼容范围。
- FastAPI、Pydantic、SQLAlchemy、LangChain 和 LangGraph 使用当前稳定主版本，禁止无约束安装。
- `pyproject.toml` 表达主版本兼容范围，`uv.lock` 固定精确解析结果。
- 前端兼容范围写入 `package.json`，`package-lock.json` 固定实际解析结果，禁止只提交未锁定的依赖声明。
- checkpoint 包与 LangGraph 版本一起升级，并运行暂停、恢复和旧 checkpoint 兼容测试。
- Docker 镜像固定主次版本，不使用 `latest`。
