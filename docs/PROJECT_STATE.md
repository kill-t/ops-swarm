# OpsSwarm 项目状态与开发交接

核对日期：2026-09-08。本快照对应最新业务代码提交 `090a44e`（第 77 步），交接文档为第 78 步独立文档切片。开始交接时分支为 `main`，Git 工作区干净，暂存与未暂存差异均为空。后续会话必须再次运行 Git 检查，不能把本段当成实时状态。

## 1. 新会话阅读顺序

1. 阅读 [AGENTS.md](../AGENTS.md) 和本文件，确认项目根目录、单一职责、测试与提交要求。
2. 阅读 [进度索引](progress/README.md)、[第 77 步验收](progress/77-repeated-call-identity.md) 与 [第 78 步交接记录](progress/78-development-handoff.md)。最新实现与最新文档切片分别记录。
3. 阅读 [第 64 篇总路线](architecture/64-full-multi-agent-roadmap.md) 和 [第 65 篇 Agent 设计](architecture/65-agent-system-design.md)，按下一切片涉及的主题按需读取，不需要每次通读全部历史文档。
4. 用本文件第 6 节定位实际代码，再核对 [数据库设计](architecture/04-database-design.md) 和 [API 设计](architecture/05-api-design.md)。发现文档与代码冲突时先查对应提交和测试，保留历史事实并修正当前入口。

架构文档规定目标与契约，代码和迁移展示实现，进度记录提供当次验收证据。本文件负责导航和当前事实摘要，不重复维护完整设计。

## 2. 项目当前目标

在已交付 MVP 上建设完整多 Agent 事故响应平台，用于学习、现场演示及简历表达，重点区别于用户已有的 RAG 项目：任务分解、专业工具调查、共享事实、分层记忆、上下文管理、动态补查、独立审查、人工介入和持久化恢复。

多 Agent 是既定方向，不再用单、多 Agent 收益比较决定是否继续。继续按可运行、可测试、可解释的小切片推进；当前全部真实环境操作仅限本地 Docker Compose 演练系统。

## 3. 当前架构与技术栈

| 层次 | 当前实现与事实来源 |
| --- | --- |
| 后端 | Python 3.12、FastAPI、Pydantic 2，模块化单体；依赖范围见 [pyproject.toml](../pyproject.toml)，精确解析版本见 [uv.lock](../uv.lock) |
| 持久化 | PostgreSQL 17、SQLAlchemy 2 异步、Psycopg 3、Alembic；业务数据库和 LangGraph checkpoint 分离 |
| 调度 | 独立调查 Worker 与恢复验证 Worker；PostgreSQL 持久任务、租约、幂等与尝试审计 |
| Agent | LangChain/LangGraph 1.x；多 Agent 当前为固定并行专业调查与综合，保留 v1 和独立 v2 路径；内部 Harness 已有任务、预算、上下文组件，整体仍未完成 |
| 取证 | Loki、Prometheus、本地变更服务只读适配器；冻结与现场来源显式区分；现场多 Agent 取证经受限异步执行器，尚未走 MCP |
| 前端与观测 | `web/` 下 Vue 3、TypeScript、Pinia、Vue Router、Vite，REST/可重放 SSE；OpenTelemetry 与 Jaeger；依赖以 [web/package.json](../web/package.json) 和锁文件为准 |
| 部署 | [compose.yaml](../compose.yaml) 包含控制面、前端、数据库、调查/验证 Worker、监控与故障演练服务；尚无 telemetry-mcp 服务 |

业务迁移当前唯一 head 为 `0037_call_invocation_index`，来自 [第 37 个迁移](../alembic/versions/20260908_0037_call_invocation_index.py)。这是仓库迁移状态，不代表任意本地演练数据库已升级；本次交接未连接或修改演练数据库。

## 4. 已完成内容与本会话成果

MVP 历史交付见 [第 63 步](progress/63-mvp-compose-release.md)：调查、证据、审批、白名单执行、恢复验证、人工关闭、不可变复盘报告、改进行动项与作战室已形成演练闭环。原单 Agent 路径、固定多 Agent 路径、评测数据和历史结果保留。本次交接没有重新执行完整 MVP 发布演示。

本次长会话推进的工程版记录可由 Git 历史逐项核对：

| 切片 | 提交 | 已交付边界 |
| --- | --- | --- |
| [64](progress/64-full-multi-agent-roadmap.md)、[65](progress/65-agent-system-design.md) | `7d4a265`、`01469ee` | 总路线、长期约束与 Agent 专项设计落盘；属于设计验收 |
| [66](progress/66-investigation-task-persistence.md) | `01c82b0` | 轮次、专业任务、尝试事实与必要查询 |
| [67](progress/67-fixed-graph-task-integration.md) | `f61386f` | 冻结 v2 固定图接入任务、结果、事件和恢复核对 |
| [68](progress/68-shared-call-budget.md)、[69](progress/69-model-input-budget.md) | `6b5c845`、`14c4b42` | 次数/时限预算；显式模型输入、Token 和估算费用预留，未知用量不补零 |
| [70](progress/70-bounded-async-evidence.md)、[71](progress/71-task-evidence-snapshot.md) | `da72125`、`089ead7` | 有界异步取证、迟到隔离；模型前持久取证快照与重试复用 |
| [72](progress/72-explicit-observation-windows.md)、[73](progress/73-live-task-evidence.md) | `daeda09`、`487ef9f` | 显式观测窗与现场 v2 接入；假传输现场回归不等于新的真实现场验收 |
| [74](progress/74-stable-frozen-identity.md) | `34bcbf9` | 冻结身份跨进程确定性、已发布内置版本的有限历史兼容 |
| [75](progress/75-task-context-builder.md) | `6fc4f67` | 最小 ContextBuilder、任务隔离、不可变材料清单与输入预留原子提交 |
| [76](progress/76-context-evidence-reader.md) | `05844ca` | 已保存调用范围内的证据目录与受控详情读取服务/API |
| [77](progress/77-repeated-call-identity.md) | `090a44e` | 同尝试重复调用槽位、兼容旧幂等键、共享预算和迁移保护 |

## 5. 正在进行、尚未完成及已知限制

交接时没有半成品业务代码或待提交业务修改；本轮仅整理文档。下一功能切片尚未开始实现。第 78 步占用交接记录编号，后续功能切片从第 79 步继续。

以下能力不能写进简历或交接为“已完成”：

- 专业 Agent 主动选择证据详情、多步工具循环和完整工具消息配对。当前目录 API 供复核保存材料使用，模型仍接收完整有界证据。
- 结构化工作摘要、历史裁剪与压缩；现有清单中的工具集合和裁剪记录被明确限制为空。
- 动态协调分派、跨专业补查、开放未知假设的完整流程、独立审查 Agent 和有界多轮调查。目标六个逻辑角色不等于六个动态 Agent 已部署。
- 日志/指标 MCP、版本化调查手册按需加载、可信操作员身份、经验候选提取/审核/检索/停用闭环。
- 图内人工暂停、补充、取消和恢复；已有业务审批不等于 LangGraph interrupt/resume 已接入。
- 规划 Agent、多步骤处置、部分失败停止、验证失败回流、跨服务复合场景工作台与完整工程版发布验收。已有 MVP 白名单动作和复盘报告不能代替这些目标能力。

已知限制和后续风险：

- v2 需启动 API 显式选择；默认仍是 v1，前端没有版本选择器。Token/费用预算由 model_budget 单独启用；未启用时不能宣称保存了完整输入预算清单。
- 新建且启用模型预算的 v2 固定 `task-context-v1` / `fixed-task-context-v1`；历史策略保持原值或 null。升级不能静默改变旧输入散列与 checkpoint 语义。
- 现场 v2 固定 UTC 截止，日志/指标回看 15 分钟，变更回看 6 小时；Worker 当前只接受本地 simulator 变更配置，GitHub 组合来源明确拒绝。现场失败不回退冻结证据，历史变更不冒充当前部署证明。
- 输入预算采用保守字节估算，输出和费用仍受供应商实际行为影响；未知用量留作未知，不把预算预留数称为模型精确调用次数。
- 当前 v2 默认 12 次模型调用、18 次工具调用、每专业任务 6 次工具调用、300 秒总时限；模型预算另有累计输出上限。接入循环不能静默提高配置，取证与后续工具读取是否共用任务额度须按既有预算语义落实。
- 证据目录内部仍加载最多三个完整有界快照进行校验；本轮没有证明 Token 或检索成本下降。
- 没有确认中的测试失败。第 77 步 448 项回归通过是该次范围内结果，不能当成全仓、所有生产连接或下一次提交的验收。

## 6. 下一步必须知道的代码边界

| 入口 | 下一会话需要核实的事实 |
| --- | --- |
| [worker/task_graph.py](../src/opsswarm/worker/task_graph.py) | 每个父尝试装配 v2、固定来源和策略；模型预算 profile 必须匹配，SDK 重试为 0、n 为 1 |
| [parallel_model.py](../src/opsswarm/investigation/parallel_model.py)、[budget/model_caller.py](../src/opsswarm/investigation/budget/model_caller.py) | 专业/综合结构化模型调用，当前没有专业业务工具循环 |
| [tasks/runtime.py](../src/opsswarm/investigation/tasks/runtime.py) | 任务尝试、结果提交、取证复用和恢复边界；技术重试与补查任务不能混同 |
| [context/builder.py](../src/opsswarm/investigation/context/builder.py)、[context/reservation.py](../src/opsswarm/investigation/context/reservation.py) | 专业只用本任务，综合用三个已完成专业；预算事务内重新组装并比较真实输入/材料清单 |
| [context/contracts.py](../src/opsswarm/investigation/context/contracts.py)、[context/loading.py](../src/opsswarm/investigation/context/loading.py) | 只接受 task-context-v1；不能只放宽消息字段就称新策略已接入 |
| [context/evidence_reader.py](../src/opsswarm/investigation/context/evidence_reader.py) | 绑定已保存 run_id/call_id 的目录和详情；首轮模型前目录的构造需要独立设计，不能伪造已保存调用 |
| [budget/model_contracts.py](../src/opsswarm/investigation/budget/model_contracts.py) | 当前 ModelInput 只有 system/user 文本消息，不承载 assistant 工具调用及 tool 响应；演进时保留旧序列化和散列 |
| [budget/store.py](../src/opsswarm/investigation/budget/store.py)、[budget/runtime.py](../src/opsswarm/investigation/budget/runtime.py) | 请求前租约校验/原子预留、共享账本与结算；相同预留拒绝再执行 |
| [budget/contracts.py](../src/opsswarm/investigation/budget/contracts.py)、[budget/policy.py](../src/opsswarm/investigation/budget/policy.py) | 当前只登记三类取证操作和专业/综合模型操作，read_evidence 尚未进入操作白名单；新工具需要同步确定角色、计费分类和数据库约束 |
| [budget/identity.py](../src/opsswarm/investigation/budget/identity.py)、[budget/tables.py](../src/opsswarm/investigation/budget/tables.py) | invocation_index 为运行层指定的 0–99 槽位，默认 0 保留旧键；不是轮次、自动循环计数、工具参数或缓存身份；尚无完整模型输出/工具交互账本 |

本会话曾查看本地 LangChain 的 create_agent 与异步模型/工具 middleware 入口，以确认可以复用框架；没有完成工具循环选型实验。下一会话应按 uv.lock 和本地安装源码核实 create_agent、ToolStrategy 及 middleware 的实际契约，不凭记忆编造 SDK 参数。

**以下只是待验证的实现建议，并非已定契约：**先让专业 Agent 使用目录和一个受限 read_evidence 工具，综合节点可暂保留结构化单次调用；通过框架 middleware 对每次真实模型/业务工具调用预留与结算。策略名、开启方式、消息表结构、输出工具是否计入业务工具预算、错误配对及崩溃后从何处重启，都要先形成可测试设计，不能从聊天草案直接当成代码现状。

## 7. 重要决策及原因

| 决策 | 原因与边界 |
| --- | --- |
| LangGraph + LangChain + 轻量 Harness | 复用调度、工具循环和 checkpoint；项目代码专注上下文、预算、权限、业务审计，不重建框架 |
| PostgreSQL 事实 + checkpoint 快照 | 业务提交与 checkpoint 不假设跨库原子；恢复必须核对已提交材料和结果 |
| 内部结构化任务，不用 A2A | 当前没有独立部署的外部 Agent 互操作需求；遥测 MCP 属于后续明确交付项 |
| 经验人工确认、简单确定性检索 | 避免未经确认的历史经验变成当前事实或授权；先建立可信身份，暂不引入向量库或独立压缩模型 |
| 新能力固定新策略、保留旧路径 | 避免旧运行恢复时输入含义、预算和审计发生静默变化 |
| 先完成第 77 步调用身份 | 原幂等键会阻止同尝试重复操作；现在已有独立槽位，但完整消息与工具授权仍需下一片设计 |

全部长期约束以 AGENTS.md 和第 65 篇为准。本次新增的开发偏好是：按独立子任务难度选择子智能体、模型和思考强度，尽量减少重复上下文；主 Agent 负责验收和提交。它不改变应用中角色模型、预算配置或运行权限。

## 8. 下一步建议与验收目标

下一功能切片：**专业 Agent 有界按需证据读取**。先写第 79 篇设计，并按需更新数据库/API 设计，然后实现；允许依据新发现再拆为独立可运行切片，但应清楚说明必要性和剩余范围。

1. 明确新上下文策略、首轮目录、后续完整消息和工具调用配对契约，保护 v1/现有 v2 恢复。
2. 按职责复用证据服务、预算入口和 LangChain 循环；身份/范围由系统注入，工具只开放证据读取。
3. 模型返回、工具参数、响应、证据散列与调用槽位形成可追溯记录；先持久化或预留，再推进下一外部调用。复用调用槽位不能代替工具输入一致性和结果大小校验。
4. 每个角色、每次循环、技术重试共享原运行额度；错误、超时、非法工具、迟到结果、未知用量和恢复边界都必须可验证。
5. 使用假模型和固定工具结果验证目录→读取→报告、越权拒绝、消息配对、预算前置、两个事故隔离、崩溃恢复；真实 PostgreSQL 验证事务与迁移。同步文档并创建一个中文提交。

之后顺序保持：工作摘要 → 动态专业调查/协调/审查 → 只读遥测 MCP → 手册、可信身份与经验 → 人工介入/规划 → 多步骤处置与验证回流 → 复杂场景与协作工作台 → CI 与完整版本发布。细节以第 64/65 篇为准。

## 9. 可复现验证与环境提示

所有命令在仓库根目录执行。Windows PowerShell 使用仓库 `.venv/Scripts/`，不要依赖全局 Python。完整第 77 步命令及 448 passed / 234.30 秒记录在其验收文档；交接切片只重新做文档、静态和差异检查，不重新声明业务测试通过。

```powershell
git status --short
git diff --stat
git log -5 --oneline
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

数据库测试由 [tests/conftest.py](../tests/conftest.py) 启动 Testcontainers PostgreSQL 17.6，并创建独立 checkpoint 数据库，Docker 必须运行；不使用 SQLite 代替。Windows 已用 Selector 事件循环兼容 Psycopg；新增异步子进程测试可参考仓库现有线程桥接做法，不直接假设 Selector 支持异步子进程。

可复用的测试材料见 [context_helpers.py](../tests/context_helpers.py)、[model_budget_helpers.py](../tests/model_budget_helpers.py)、[task_evidence_helpers.py](../tests/task_evidence_helpers.py)、[task_facts_helpers.py](../tests/task_facts_helpers.py)。已有模型预算仓储测试会刻意清空上下文策略来覆盖历史契约，不要把它们改成新策略后误删旧路径回归。

运行日志、`.pytest_cache` 内临时检查器和工具会话 ID 均不是交接依赖。正式环境配置见 README、Compose 和变量示例，不读取、复制或提交真实 .env、密钥和个人本地配置。

## 10. 可直接发送给新会话的开场任务

> 请先阅读 AGENTS.md、docs/PROJECT_STATE.md、docs/progress/README.md 和最新交接/实现记录，核对 Git 状态、代码及第 64/65 篇计划。最新业务实现为第 77 步，交接为第 78 步；从下一功能切片推进专业 Agent 有界按需证据读取。先确认状态、接口、持久化与失败路径，再按单一职责实现、使用假模型和真实 PostgreSQL 验证、更新文档并提交。不要把证据目录 API、重复调用槽位或历史业务审批当成已经完成的模型工具循环和图内人工恢复；按任务难度合理委派子智能体，统一验收后再提交。
