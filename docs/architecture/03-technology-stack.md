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
| `LANGSMITH_TRACING` | 是否启用可选 LangSmith 追踪 |

配置对象只能出现在基础设施层，领域层通过模型网关协议调用，不读取环境变量。

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

Psycopg 3 异步模式在 Windows 上不支持默认 Proactor 事件循环。Alembic 和 pytest 在 Windows 明确使用 Selector 事件循环，Linux 部署保持平台默认实现；这属于基础设施兼容策略，不进入领域代码。

Starlette 当前仍兼容旧版 HTTPX，但已将其标记为弃用；项目测试使用 HTTPX2，避免在新工程中引入已知迁移债务。

Agent 测试分三层：

1. 使用假模型与固定工具结果验证图路由和状态更新。
2. 使用冻结故障数据集离线评测根因、证据和计划。
3. 显式开启时才运行真实模型端到端评测，并记录模型、提示版本、Token、成本和延迟。

当前已经把 `langgraph` 和 `langgraph-checkpoint-postgres` 声明为直接生产依赖，实现 Graph API 并行调查和 `AsyncPostgresSaver` 跨连接恢复。并行测试不能依赖分支完成顺序，所有进入综合提示的证据和报告都按稳定业务标识排序。checkpoint 反序列化使用显式项目类型白名单，不允许数据库内容触发任意 Python 类型加载。

持久 Worker 当前通过业务枚举在 LangChain 单 Agent 与 LangGraph 多 Agent 之间确定性路由。多 Agent 处理器先查询线程 checkpoint 是否存在，再决定首次运行或恢复；模式选择不进入模型提示词。

Agent 输出先通过 Pydantic `InvestigationArtifacts` 校验，再由 SQLAlchemy 在当前任务租约事务中写入规范化 Evidence Board。PostgreSQL 复合外键阻止专业报告跨调查引用证据；这类业务完整性不依赖 LangGraph checkpoint 或模型自觉。

根因质疑使用版本化纯 Python 规则，不增加第二个 LLM 裁判。LangGraph 负责把规则节点放入可恢复序列，Pydantic 校验输出结构，PostgreSQL 保存假设和逐证据关系；业务规则仍可在毫秒级纯单元测试中覆盖。

处置计划当前使用确定性模板建立安全基线：Pydantic 只接受白名单动作和精确参数模型，纯 Python `action-risk-v1` 策略重算风险，PostgreSQL 保存不可变版本并用检查约束阻止高风险动作绕过审批。未来 LangChain 可以生成相同 `ActionPlan` 契约的候选计划，但不能替代风险策略或审批。

## 5. 可观测性

| 技术 | 目标 |
| --- | --- |
| structlog | 结构化 JSON 日志和统一上下文字段 |
| Prometheus Client | API、Worker、工具、任务与评测指标 |
| OpenTelemetry | 跨 API、Worker、Agent 节点和工具调用的 trace |
| Loki | 本地演练服务日志查询目标 |
| LangSmith | 可选查看模型输入输出和图执行，不保存业务唯一事实 |

日志和 trace 使用 `incident_id`、`run_id`、`work_item_id` 和 `trace_id` 关联。密钥、审批令牌和原始敏感数据不得写入日志。

## 6. 故障环境与前端

| 类别 | 选择 | 引入阶段 |
| --- | --- | --- |
| 容器编排 | Docker Compose | 阶段 1 |
| 被管服务 | gateway-service、order-service | 阶段 1 起逐步增加 |
| 故障依赖 | PostgreSQL 17、Redis 7.x | 对应故障切片引入 |
| 遥测 | Prometheus、Loki | 先提供简化数据，再接真实服务 |
| 前端 | Vue 3、TypeScript、Vite | 时间线与审批阶段 |
| 前端状态 | Pinia、Vue Router | 与首个 UI 切片一起引入 |

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
- checkpoint 包与 LangGraph 版本一起升级，并运行暂停、恢复和旧 checkpoint 兼容测试。
- Docker 镜像固定主次版本，不使用 `latest`。
