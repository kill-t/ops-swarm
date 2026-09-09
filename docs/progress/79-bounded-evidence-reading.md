# 79 - 专业 Agent 有界按需证据读取

## 目标

让专业 Agent 根据本任务目录选择要读取的证据详情，再提交结构化报告。此前第 76 步只有已保存调用的证据查询，第 77 步只有重复调用槽位；本片将它们接入实际框架循环，并补齐每次调用的消息、权限、预算和持久恢复边界。

## 设计与边界

详细契约见 [专项设计](../architecture/79-bounded-evidence-reading.md)。先核对 AGENTS.md、项目交接、第 77/78 步记录和 main 的 `4c8e67f`，开始时工作区干净。使用仓库锁定的 LangChain create_agent/ToolStrategy 和 middleware，不新增循环调度器。

新策略显式开启，旧运行的输入、散列和 checkpoint 语义保留。首轮从业务材料投影目录，后续从成功账本重建配对历史；每次真实请求前预留，结果与结算原子提交。工具只接收证据 ID 与快照散列，其他范围由系统绑定；输出报告只能引用本尝试成功读取过的证据。

专业循环不新增 checkpoint。中断后已有专业结果复用，未完成任务以新尝试从原快照目录开始，所有旧调用继续占用共享预算。原始取证与读取共用每任务六次工具额度；结构化报告的工具确认属于模型输出协议，不额外计业务工具调用。综合节点保留单次结构化调用。

## 实际改动

- 增加 evidence-read-v1 启动策略校验和 Worker 装配，要求 v2、模型预算和匹配的 function_calling profile；省略新字段维持原选择。
- 独立 LoopModelInput/交互契约、消息转换、固定提示、工具适配、纯结果规则、上下文组装、业务服务和框架 middleware 按职责分开。
- 抽取新旧入口共用的目录投影；首轮无需伪造 call_id，后续详情复用已有受控证据服务。
- 扩展三类按专业归属的读取操作；预留事务核对真实输入、完整历史、来源模型调用及工具参数，成功结算核对响应和快照内容。
- 迁移 0038 增加 tool_request/call_result JSONB、操作与大小约束，以及新策略/交互的防丢失降级保护；旧行不回填新策略。
- 新增只读 interaction API，原 model-input 支持独立新版本；不开放预算写入或身份扩张入口。
- 专业模型解析失败前通过回调保留供应商原始用量；非法、超限结果不进入可继续历史。未知用量不补零。
- 同步数据库/API、路线、README、进度索引与项目交接。旧迁移测试只更新当前 head 断言，保留历史升级/降级验证。

开发协作使用两名 gpt-5.6-sol / high 子智能体，分别处理框架消息适配与审查、启动策略/迁移与 API 测试。主 Agent 完成整合和独立验证，并修复审查发现的终态精确结算重放问题。未测量 Token 或费用收益，不宣称节省比例。

## 测试方法与结果

验收日期：2026-09-09。模型与外部来源使用假传输，实际执行 LangChain、ChatOpenAI SDK、LangGraph 和 Testcontainers PostgreSQL 17.6；冻结与现场模式回归不等于真实现场发布验收。未读取真实 .env、调用真实模型或迁移用户的 Compose 业务数据库。

统一验收命令通过 **549 项测试，耗时 358.96 秒**。此前 29 项图/账本/迁移和 40 项专业循环/账本/API 定向验证范围有重叠，不额外计入总数。Ruff 检查通过，573 个文件格式检查通过，mypy 检查 231 个源码文件无问题；迁移唯一 head 为 `0038_evidence_read_loop`。8 篇更新文档的 UTF-8、标题、代码围栏及 295 个本地链接检查通过，`git diff --check` 通过。

```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation tests/integration/test_evidence_loop.py tests/integration/test_loop_graph.py tests/integration/test_loop_ledger.py tests/integration/test_loop_migration.py tests/integration/test_repeated_calls.py tests/integration/test_call_identity_migration.py tests/integration/test_call_budget_store.py tests/integration/test_budgeted_call_runner.py tests/integration/test_budgeted_graph.py tests/integration/test_model_budget_store.py tests/integration/test_model_budget_migration.py tests/integration/test_model_budget_graph.py tests/integration/test_context_migration.py tests/integration/test_context_reservations.py tests/integration/test_context_graph.py tests/integration/test_fixed_task_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_migration.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_live_task_migration.py tests/integration/test_live_task_graph.py tests/api/test_call_budget_api.py tests/api/test_model_budget_api.py tests/api/test_context_manifest_api.py tests/api/test_context_evidence_api.py tests/api/test_loop_api.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py --tb=short
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

覆盖重点：真实传输前已有输入/预留；目录→单次/重复读取→报告；非法工具、跨事故/任务、错散列、额外权限参数、未读取引用、超大响应、重复工具 ID；模型次数、任务工具、输入/输出/费用/时限预算；未知和无效用量；事务内拒绝伪造历史/清单/结果；同槽位并发只预留一次；失效租约和终态迟到写入；任务终态后的精确结算重放；两个事故在冻结和现场模式下并发隔离；三个持久化边界中断后复用快照、创建新尝试并保留原支出；迁移保护和只读 API。

测试中发现并修复了目录工具 schema 与 SDK 实际绑定格式不一致的问题。数据库验收曾因 Docker 后端不可用停在 fixture 准备阶段；另一次长回归会话无法取回最终输出，均未计为通过。重新启动并确认 Docker 连接后，完整重跑上述命令，保存日志和退出码，得到本次 549 项通过的结果。

## 限制与下一步

- 按需读取仅选择已有有界快照，未新增动态遥测查询、工作摘要、补查协调、MCP 或图内人工恢复。
- 当前每轮最多一个工具调用，技术重试仍由原任务/Worker 有界处理。未通过校验的供应商正文不保存为完整原始审计，只保存失败状态和可取得的用量。
- 默认预算没有提高。三专业各读取一次再综合至少需要七次模型调用，4096 × 7 超过默认累计输出 24000，开启者须显式配置，例如单次输出 1024。原结果/评测的 tool_call_count 保留三次取证语义，累计读取与重试次数以 call-budget 为准。
- 没有测量或证明 Token、延迟或成本下降，不把目录内加载完整有界快照称为已优化检索。
- 下一片为结构化工作摘要，先定义来源引用、不可变版本、必要材料和失败边界，再考虑历史裁剪与后续动态协作。

## 面试表达

> 我让专业 Agent 先看自己任务的证据目录，再通过一个只读工具选择详情。LangChain 负责工具循环，我的 Harness 在每次真实调用前检查租约并原子预留预算，调用后把消息配对和结果一起落库。下一轮输入从成功账本重新组装，模型不能靠传入另一个任务 ID 扩张权限。进程中断后复用原证据快照，以新尝试重新调查，但旧调用继续计费；已完成专业报告直接复用。测试用真实框架和 PostgreSQL，覆盖了跨事故隔离、预算竞争、非法引用和多个提交边界恢复。
