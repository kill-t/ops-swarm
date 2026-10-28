# 134 - 独立回流专业执行与原账本调用

## 目标

基线main的fcd230b。核对AGENTS、PROJECT_STATE、进度索引、133设计/验收、64/65、Git及现有任务/预算/MCP代码后，消费133待执行任务，建立真实独立尝试，在验证后的新窗口重新采集、冻结完整专业输入并在原账本调用模型，保存可追溯报告。初始仅用户output/未跟踪目录，本片不读取或提交该目录。

## 设计与边界

事实来源为[专项设计](../architecture/134-reinvestigation-execution.md)。原队列不可改写，因此134独立保存执行投影；133继续表达pending分派历史，实际执行进度由134接口读取。原run继续settled并持槽，原work/attempt、旧图END/checkpoint、原Round/Task和128～133正文保持，不新建run或复制预算。

日志/指标沿用只读MCP v1，变更沿用本地窗口适配器。原分派带微秒或不足完整分钟，实际窗口只能向内取整，持久化requested/effective及round_seconds/inward_minutes原因；短窗明确blocked、零调用，不向失败验证前扩展。模型输入保留未观测区间，不能据此声称覆盖完整原请求。精确任意秒MCP属于后续能力，不静默改变旧协议。

一次执行尝试使用固定预取证加结构化专业报告，复用LangChain结构化输出，当前没有模型业务工具或新工具循环。完整132来源、人工约束、反证/失败查询/未解问题、133任务和实际新快照进入ModelInput及manifest；必要材料超预算停止，不只传goal。报告复用SpecialistReport，不强制已知根因枚举，允许空证据报告缺口，引用只能属于本任务新快照。

## 实际改动

- 新增reinvestigation_execution，按严格契约、轻量模型结果包装、内收窗口、异步只读端口、上下文、模型适配、纯预算规则、来源/材料重建、独立任务存储、账本、Worker、查询/API及CLI分工。主Agent负责设计、运行层/账本/查询整合、独立验收和提交；子Agent分别做只读边界核查、材料/SDK与纯运行测试、原生迁移。
- 0089_reflow_execution增加work、attempts、evidence、packets、results五表，稳定task.id沿用133，冻结来源/分派hash、版本、实际窗口及max_attempts。最多三次真实UUID/token尝试，默认180秒租约、最多300秒，同根最多3个未失租运行任务。父work→134 work→132job/candidate→Incident锁序及SKIP LOCKED、有界扫描保护领取，网络在数据库事务之外执行。
- 日志/指标使用固定本地MCP异步客户端，变更使用ReadOnlyQueryExecutor，底层线程超时后继续占用容量直到实际结束；失败关闭、不回退冻结材料。新快照复用live-mcp-v1契约，保留角色/服务/窗口/来源、20条及128KiB限制，成功取证与快照同事务保存，packet随后独立冻结。
- 原账本新增collect_reinvestigation和analyze_reinvestigation，reflow_task_id/reflow_task_attempt_id绑定真实新任务和尝试。旧task/attempt字段为空，原investigation_attempt_id只作来源。所有角色/重试共享原策略、总期限、次数、输入/输出及费用；旧工具与新工具按各自稳定业务task计数。并发预留全部保守计入，自己的已有预留不重复要求容量；旧预留/结算/父收尾不能冒用新owner。
- 新当前SQL快照支持新操作/owner，原131/132历史函数保持；新增调用严格晚于133捕获时刻，历史前缀不回填。handoff只加入真实133/134归属窄JOIN，不调用完整134来源，避免递归。133最低容量availability可因自身在途调用而blocked，134只使用其不可变历史校验，另按当前事故/告警/原期限和实际增量预算判定执行资格。
- 成功取证与成功模型调用均可跨尝试复用；业务结果保存前崩溃不重复已确认成功的外部调用。超期接管将未结算调用记unknown，原allocation保留；未知usage不补零，超预留实际用量保留审计而不发布报告。unknown重试可能产生新调用，不承诺模型恰好一次。
- 原生独立重建133事件前缀、完整来源/任务/预算、新证据和packet，拒绝伪造owner/窗口/版本/正文/hash/报告引用。Incident字段与最后状态事件不一致按损坏拒绝。成功快照/取证调用及最终work/当前attempt/result分别要求同事务/同xmin，支持同SAVEPOINT；真实时钟提交门禁拒绝迟到及当前失效，终态与材料不可变，有数据禁降级。
- 新增可信GET /api/v1/investigations/{run_id}/reinvestigation-execution，最多3个任务的投影/尝试/快照/packet/结果；新调用的model-input、context-manifest和interaction也要求可信Bearer/read，旧公开演练路径保留。CLI显式--once/--continuous，不加入默认Compose、不装配写工具。35个历史集成文件仅更新最新head断言。

## 测试方法与结果

验收日期2026-10-28。按classname及测试名去重、取最后结果，共2,585项不同用例有通过记录：单元2,571、API4、原生4及旧链路6。重复的材料/运行层定向与来源补验不重复计数。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 2,571项通过，40.423秒，新增材料39项及运行/预算39项；包含真实LangChain SDK固定HTTP、日志/指标MCP SDK+ASGI、异步并发3与阻塞线程迟到容量、空证据/越界引用/超窗、未知/部分/非法usage和预算竞争 |
| API执行与恢复 | 4项通过，836.618秒；两类来源各执行log/metric/change三个专业，原账本真实新增工具/模型调用，旧历史在途可读，成功调用及快照崩溃复用、新告警后unknown/真实usage审计且不发布，原父及133分派保持 |
| 原生迁移与独立主验收 | 最终完整4项通过，346.036秒；metadata/空升级降级/command.check、三角色窗口对照、来源/hash/版本与原生不可变、真实三任务并发工具/模型预留、SQL/Python完整budget和packet对照、伪owner/来源/报告引用拒绝、成功collect缺同事务snapshot拒绝、真实30秒租约到期提交回滚及第三次尝试接管完成、全材料禁删/有数据禁降级 |
| 来源完整性补验 | 1项通过，132.948秒，与原生4项中的来源测试重叠；补强Incident无事件version变化拒绝，合法后续告警后原历史仍可重建，同SAVEPOINT准备保持 |
| 旧链路回归 | 6项通过，35.844秒；通用迁移3项及MVP配置/Redis/可信审批幂等和主体审计3项 |
| 静态与导航 | Ruff、987文件格式、Mypy587源文件、唯一head0089_reflow_execution和CLI帮助已通过；文档本地链接与Git差异核对通过 |

主验收原生并发用例使用真实完整前置链，并在132成功后、133分派前实际等待恢复结果至少65秒，以满足现有MCP整分钟窗口；不改任何冻结事实，不延长原900秒期限。并发模型预算均先持久预留，再分别发布；第三任务先一次失联审计、第二次真实30秒租约到期，迟到成功UPDATE在提交时整笔回滚，旧claim的应用settle/complete均被拒绝，第三次尝试保留unknown占用并复用快照/packet后成功。等待是否到期以PostgreSQL clock_timestamp确认，不用宿主时钟推断。此测试不代表实际等待原run总deadline；总期限规则另由纯规则及每次数据库当前依据检查覆盖。

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice134-unit.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_execution_api.py -q --junitxml=.pytest_cache/slice134-api.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_execution_guards.py tests/integration/test_reinvestigation_execution_migration.py -q --junitxml=.pytest_cache/slice134-native.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_execution_migration.py::test_0089_history_source_and_work_frozen_hashes -q --junitxml=.pytest_cache/slice134-source-final.xml
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice134-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_execution.main --help
git diff --check
```

最终报告为忽略的.pytest_cache/下slice134-unit.xml、slice134-api.xml、slice134-native.xml、slice134-source-final.xml及slice134-regression.xml；slice134-materials.xml和slice134-runtime.xml可单独核对新增各39项，不重复计数。早期原生子任务运行输出3项通过仅作为开发记录，最终主Agent已启用JUnit独立复验，不补造报告。

仅隔离Testcontainers PostgreSQL17.6、假模型与固定HTTP/MCP传输；没有连接生产、升级用户Compose数据库或调用真实供应商。接口样本表示隔离传输中的取证行为，不冒称新生产现场。沿用132在根创建时固定足够模型容量的夹具，未运行中追加预算。无前端变更，未重跑前端构建，报告/缓存不提交。

## 限制与下一步

目前形成专业报告，没有消费报告进行回流综合、独立审查、规划审批或新处置。MCP v1限制下实际采集可能只覆盖原分派窗口的内收部分，或因短窗blocked，完整任意秒支持需要版本化协议。必要输入超原预算、原截止或来源改变后停止，不保证每个运行都能完成所有阶段。held/unknown人工收敛、终端取消、预算追加、多步骤与新前端仍按路线推进。

下一片135定义并消费同根完整专业结果/证据闭包，保留人工约束和未观测区间，在原预算和期限内形成有界开放假设，再连接独立审查。旧图不重开，原批准不能覆盖新动作。

## 面试表达

“回流任务没有借旧父租约或创建新run。我把执行投影与不可变分派分离，用真实任务尝试在原账本预留工具和模型调用，三个专业共享原预算，未知用量继续占用。只读MCP的实际窗口向内收缩并追踪未观测范围，新证据与完整人工约束进入冻结上下文；专业报告只能引用自己的新证据。成功材料跨尝试复用，数据库在真实租约到期后拒绝迟到提交，接管保留原占用。当前完成专业分析，综合、独立审查和后续动作仍分别受控。”
