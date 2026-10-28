# 133 - 回流专业任务的确定性编译与持久分派

## 目标

基线main的5572e70。核对AGENTS、PROJECT_STATE、进度索引、132设计/验收、64/65及Git和实际任务代码后，将已保存的回流建议变成稳定、可恢复的待执行专业业务任务。初始仅用户output/未跟踪目录，本片不读取或提交该目录。

## 设计与边界

事实来源为[专项设计](../architecture/133-reinvestigation-dispatch.md)。旧handoff每次按原表最高轮和已完成任务核验，132每次按原表max/count重建冻结输入；直接追加原表会破坏这些已验收历史。因此新增独立分派/任务表，复用RoundAssignment/TaskAssignment，绑定原run、原逻辑下一轮与累计任务容量；不新建run或复制预算，不放宽旧图及父租约。

queued分派包含整轮及完整pending任务，blocked无任务。新服务/窗口/执行策略由系统注入：log/metric最多900秒、change最多21600秒；window_start=max(失败恢复结果时刻,分派时刻-lookback)，window_end为分派时刻，snapshot=None。保留132合法自定义task_key，避免误用旧首轮key=role限制。没有遥测采集、专业调用或执行尝试，不把旧证据变为新现场事实。

完整原账本、原deadline及未知占用保持，按每任务一次完整单次模型和一次工具检查最低容量；尚无预算预留，更不保证后续整个调查一定有额度。在途调用暂不消费，待其结算再固定历史前缀。分派保存basis_event_cursor，历史按前缀重建，当前事故/告警/预算资格另查。

## 实际改动

- 新增reinvestigation_dispatch包，分离严格契约、纯编译/预算、权威来源/事件前缀、短事务消费者、历史查询、API及CLI。主Agent负责设计、应用层、整合、独立验收与提交；子Agent分别只读审查/纯规则测试及原生持久化/测试。
- 0088_reflow_dispatch新增investigation_reflow_dispatches和investigation_reflow_task_queue。独立轮次/任务ID为原run及版本化名称的SHA256前128位，重复/并发不追加任务；短事务父work锁SKIP LOCKED，再锁132job、candidate及Incident。GET共享事故锁保持投影一致。
- 完整重建132成功结果、冻结材料、真实成功call和独立尝试。合法成功call可以属于早期已结束尝试、由当前成功尝试复用，不能把它冒充旧父执行。损坏原材料或Incident字段与状态事件不一致均拒绝，不伪装业务blocked。
- 原生逐字段重建正文、完整预算与新窗口编译，阻止重算摘要后伪造owner/run/round/服务/任务/策略/窗口/原因；只允许同事务整轮、完整任务及相同xmin，拒绝缺失/追加/跨事务补齐、不可变历史删改及有数据降级。queued提交前真实检查原截止及事故/告警，失败整体回滚。
- 新增可信GET /api/v1/investigations/{run_id}/reinvestigation-dispatch，返回不可变record/tasks及pending/current/stale/expired/blocked投影。即使legacy_local也不能公开可信材料；GET无写入。CLI显式--once/--continuous，不加入默认Compose，不调用模型或外部系统。
- 旧父settled/持槽、图/checkpoint、原轮次/任务、预算和128～132历史保持；132的not_dispatched是历史事实，133单独表示实际队列进展。35个历史集成文件仅更新最新head断言。

## 测试方法与结果

验收日期2026-10-28。按classname及测试名去重、取最后结果，共2,510项不同用例有通过记录：单元2,493、API4、原生7及旧链路6。原生后续定向复验不重复计数；早期API前置来源失败不计入通过。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 2,493项通过，80.738秒，新增44项编译/预算规则测试 |
| 旧链路回归 | 6项通过，67.063秒，通用迁移3项及MVP配置/Redis/可信审批3项 |
| API与原子回滚 | 最终完整4项通过，474.215秒；双来源并发单胜、可信读取及legacy拒绝、旧历史保持、后续告警stale和预先告警blocked、132成功调用跨尝试复用、整轮写后崩溃及应用截止回滚、任务材料损坏拒绝。此前定向边界单项171.764秒通过，和最终结果重叠不计数 |
| 原生整轮及来源 | 去重7项通过：首批6项410.652秒，最终来源2项166.575秒、真实告警/来源复验2项197.310秒；同SAVEPOINT成套写入及132跨尝试复用/损坏拒绝单项122.197秒通过，不重复计数 |
| 静态与导航 | Ruff、Mypy570源文件、唯一head0088_reflow_dispatch和CLI帮助已通过；964文件格式、本地文档链接与Git差异检查通过 |

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice133-unit.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_dispatch_api.py -q --junitxml=.pytest_cache/slice133-api-final.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_dispatch_migration.py -q --junitxml=.pytest_cache/dispatch-native.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_dispatch_migration.py::test_0088_stale_source_is_blocked_without_tasks tests/integration/test_reinvestigation_dispatch_migration.py::test_0088_independent_source_rebuild_rejects_corrupted_success_chain -q --junitxml=.pytest_cache/dispatch-native-events.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_dispatch_migration.py::test_0088_independent_source_rebuild_rejects_corrupted_success_chain -q --junitxml=.pytest_cache/dispatch-native-savepoint.xml
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice133-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_dispatch.main --help
git diff --check
```

沿用132在根创建时固定足够模型容量的隔离夹具，不修改既有预算。假模型与固定HTTP传输不等于真实供应商或生产现场。开发审查发现并修正了在途快照历史漂移、132成功call跨尝试归属及Incident字段/事件不一致等边界。SAVEPOINT探针确认row.xmin是子事务而pg_current_xact_id为顶层，最终去掉即时顶层比较，保留延迟整套相同xmin约束；同SAVEPOINT成套写入通过。API早期人工来源在131准入前被拒绝，未计作133通过，最终完整批次已重新准备并通过；不推断当时失败是由负载或某一时效原因造成。

最终报告为忽略的.pytest_cache/下slice133-unit.xml、slice133-api-final.xml、slice133-regression.xml及dispatch-native.xml、dispatch-native-final.xml、dispatch-native-events.xml、dispatch-native-savepoint.xml。按测试身份去重取末次结果，报告和缓存不提交。

应用截止回滚使用注入时钟，不能表述为实际等待原deadline；原生提交stale用事务内事故依据变更验证。仅Testcontainers PostgreSQL17.6，不使用SQLite，未升级用户Compose数据库、调用真实模型或连接生产。无前端变更，未重跑前端构建。

## 限制与下一步

本片只将建议变成pending任务，没有启动专业调查、新证据采集、模型调用、综合审查、图暂停恢复或新处置。原始建议/上下文继续保留全部人工约束与证据引用，后续执行必须消费这些材料，不能只读任务goal。业务任务需要下一片独立真实执行尝试，不借旧父租约；旧批准不覆盖后续动作。

下一片134接独立专业尝试/租约、验证后的新窗口只读取证和原账本实际预留。其后接结果、综合与独立审查、后续规划审批；held新证据、unknown人工收敛/取消、多步骤及前端仍按路线交付。

## 面试表达

“我把回流建议编译成独立的业务任务队列，原运行和累计预算不重置。独立表保留原最高轮交接与冻结上下文历史，逻辑轮次和任务数仍受原上限约束。系统注入服务、验证后的新取证窗口和执行策略，整轮与全部任务同事务保存，数据库重建来源和编译，防止缺失、篡改与跨事务补齐。队列当前待执行，后续专业调用必须建立真实新尝试并在原账本预留。”
