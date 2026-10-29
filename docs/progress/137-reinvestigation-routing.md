# 137 - 回流审查的确定性路由

## 目标

基线main的0766f68。本片已验收，随本记录创建独立提交，实际提交以Git核对。核对AGENTS、PROJECT_STATE、进度索引、136设计/验收、64/65、Git与实际代码，消费独立成功审查，形成可解释的后续意图。事实来源为[专项设计](../architecture/137-reinvestigation-routing.md)。初始只有用户output/未跟踪目录，本片不读取、修改或提交该目录。

## 设计与边界

135pending_review和136recorded都是不可变历史；新增独立not_dispatched路由，不修改旧正文、原父settled/持槽、旧图或审批。复用review_routing纯规则而不照抄模型suggested_direction：充分支持且无综合/审查反证、无审查缺口/竞争解释才可进入候选评估；不足支持有有限补查且原容量足够才可follow_up，否则await_human。原轮次按133实际轮次，任务数为原任务数加本次完整专业集合；不重置原计数。

完整人工constraint一律作为未解决限制阻断自动路径。保留原综合未解问题和每专业effective_window，未观测区间不能充当阴性证据。后续最低容量按原单次输入/输出上限及固定价格保守估算：候选一次模型，补查每专业一次模型/工具并加综合和审查两次模型。未知用量保留分配；该诊断不预留额度，真正调用仍需新阶段在原账本原子预留。

## 实际改动

- 新增reinvestigation_routing：严格契约、纯规则、完整来源读取、短事务消费、历史查询、API/CLI及单一事实表，按职责拆分；没有新模型、调用owner或租约。
- 0092_reflow_routing新增investigation_reflow_routes，一根至多一条，直接外键到136结果。绑定135/136精确结果、136来源/packet、原账本前缀、事故事件游标和固定规则版本；SQL独立重建真实调用者与发布尝试，允许已成功调用来自早前失败尝试。
- SQL独立编译基础路由、原容量、约束、预算及完整正文，Python复用既有领域规则并对照；旧131～136函数保持不变。插入守卫拒绝自洽摘要下的伪造正文，更新/删除拒绝，有数据禁止降级；延迟提交守卫阻止自动意图在事务期间因新告警、总期限或账本变化而提交。
- 显式消费者至多扫描8份成功、无路由、无在途调用的来源，原work锁先于136job、135job、132job、candidate及Incident；SKIP LOCKED与唯一根身份实现并发单胜和重复空操作。网络不在事务内，失败事务可重新消费。
- 可信Bearer/read GET reinvestigation-routing，no-store，历史来源/账本前缀重建与当前stale/expired/blocked诊断分离。CLI --once/--continuous，不新增默认Compose；HTTP只读。历史迁移用例只更新最新head断言。

## 测试方法与结果

验收日期2026-10-29。按classname与测试名取最后结果去重，共2,647项不同用例通过：单元2,632、API3、本片原生/迁移6及旧链路6。全量单元先通过2,628项，后补4项容量/在途边界均通过；合并去重统计，没有把重跑重复计数。

| 范围 | 实际结果 |
| --- | --- |
| 单元 | 原全量2,628项65.300秒通过；新增最终19项覆盖三种有限意图、真实轮次/任务停止、人工约束不可由模型消除、缺口/竞争解释、整套补查最低模型/工具数、输入/输出/费用容量、unknown保留、在途账本拒绝、期限和伪造来源 |
| 原生/迁移 | 6项通过：command.check与空库降级0091/升级head，另5项SQL基础路由与纯领域三方向/原轮次及总任务容量对照；最早单元+迁移16项41.824秒通过，最终补验包含在下述31项中 |
| 双来源API | 候选直接来源candidate_evaluation 160.517秒、人工来源follow_up 267.095秒通过；人工来源复用136早前尝试的成功调用。覆盖并发消费单胜、重复空操作、原父/135/136/完整账本不改、可信401/403与no-store、完整SQL/Python正文对照、伪造摘要/调用/状态/任务数/账本拒绝、不可变和有数据禁降级、新告警后历史不变 |
| 提交门禁API | 最后串行复验127.880秒通过，含启动报告总时134.650秒：路由插入及新事件追加均成功，异常明确发生在COMMIT；整笔回滚后正常消费者记录await_human及reflow_source_changed，历史查询正常 |
| 补验及旧链路 | 31项55.234秒通过，含最终19项领域测试、本片6项原生/迁移、通用迁移3项及MVP两类处置/可信审批3项；与全量重叠项去重 |
| 静态/差异 | Ruff、1,036文件格式、Mypy624源文件、唯一head0092_reflow_routing、CLI帮助、变更文档本地链接及git diff --check通过；35个历史迁移文件已逐一与HEAD对照，只有0091→0092最新head文字变化 |

API主报告有两项通过、一项早期失败，不能称主批次全绿。同名提交门禁用例曾在134前置取证链被现有调用时间/序号守卫拒绝，尚未进入137；额外并行复验也出现前置失败。测得主机比隔离PG快约0.6秒，接近已有1秒容差，不能据此断言唯一根因。停止并行完整链后最终串行用例通过，替代同名早期失败；没有放宽守卫、改冻结时间或追加原预算。断言另有显式wrote_event标记，避免把INSERT失败误当作COMMIT拒绝。

数据库首次检查因Docker Desktop未启动报错，启动后正式迁移往返通过。所有数据库检查使用隔离Testcontainers PostgreSQL17.6-alpine、假模型和固定取证，没有真实供应商、用户Compose升级或生产连接。本片API使用单个change专业，不需额外整分钟MCP等待；136三专业历史验收仍保留。本片没有前端变更，未重跑前端构建。

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice137-unit.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_routing_api.py -q -x --junitxml=.pytest_cache/slice137-api.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_routing_api.py::test_commit_rechecks_new_event_and_stale_source_records_human -q --junitxml=.pytest_cache/slice137-commit-final.xml
.venv/Scripts/pytest.exe tests/unit/investigation/test_reinvestigation_routing.py tests/integration/test_reinvestigation_routing_migration.py tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice137-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_routing.main --help
git diff --check
```

最终统计按slice137-core.xml、slice137-unit.xml、slice137-regression.xml、slice137-api.xml、slice137-commit-final.xml顺序合并同名最后结果；各报告位于忽略的.pytest_cache/，不提交缓存和用户output/。

## 限制与下一步

意图尚未分派：不产生新候选、计划、审批、动作或补查任务，不沿用旧批准，也不恢复旧图。原期限到期或剩余额度不足时只能保留阻断；没有预算追加。综合未解问题是后续必须保留的材料，已有契约要求至少一项，不以“问题非空”机械关闭所有候选评估意图；进入候选评估也不等于根因已被客观证明。任意秒MCP、held/unknown人工处理、终端取消、多步骤和前端仍后续分片。

第138步受限消费回流candidate_evaluation意图：新候选来源必须精确绑定137及135/136完整新依据，保留人工约束/窗口与原累计预算，独立尝试和幂等恢复；新计划及审批另行绑定，不直接复用旧候选或旧批准。follow_up后续分派另片实现。

## 面试表达

“审查意见不能直接授权执行。确定性路由从精确提案和独立意见重新判断证据异议，按真实轮次、任务总数、人工限制及原累计预算保存未分派意图。数据库独立重建来源并编译完整正文，提交时再次检查依据和期限。历史事实不可改，后续告警只改变当前适用性；后续阶段仍需重新预留预算和取得新的计划审批。”
