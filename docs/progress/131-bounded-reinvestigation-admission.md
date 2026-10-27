# 131 - 验证失败的有界回流准入与原预算继承

## 目标

基线main的d73ce87。核对AGENTS、PROJECT_STATE、进度索引、130设计/验收、64/65及Git和代码后，为验证失败建立显式、可信、可审计的回流准入。本片只交付not_dispatched请求，原父运行继续持槽；实际调查与预算预留后续交付。初始仅用户output/未跟踪目录，未读取或提交。

## 设计与边界

事实来源为[专项设计](../architecture/131-bounded-reinvestigation-admission.md)。只有130 observed_failure、父settled且marker为空、Incident仍为精确investigating版本、没有后续告警时才能新建。新告警按130最后状态事件之后的incident.alert_attached游标识别，接收时间较早的迟入库告警也会使当前依据失效。

每根运行最多一份准入，reflow_number=max_reflows=1；稳定请求ID直接复用原run UUID，root_run_id、previous_run_id、budget_run_id同为原run。更换主体或幂等键不能增加次数。请求冻结完整CallBudgetSnapshot，保留原策略、started_at、deadline、全部调用和未知模型用量。未知继续占保守预留，实际用量不补零；准入门槛为原账本还容纳一次完整单次输入/输出上限的协调调用及估算费用，不保证整个后续调查一定可执行。

当前原预算预留入口要求running父租约，本片父run仍settled，因此不能直接调用它执行回流。后续必须建立专属阶段身份、原账本预留、上下文与轮次/任务上限；不能通过新建run或复制余额清零预算，也不能沿用旧批准执行新动作。

## 实际改动

- 新增investigation/reinvestigation，按契约、纯策略、可信正文授权、历史查询、事务服务、API及ORM分工。主Agent完成设计、应用层、整合及独立验收，子Agent分别实现预算策略/测试和原生迁移/测试，主Agent审查关键结论并统一提交。
- 0086_reinvestigation_admission新增唯一不可变请求及逐authorization命令审计，原生重建完整130→129→128→127和候选链、当时完整预算前缀、固定关系/次数及可信授权。请求与applied审计同事务；允许同一SAVEPOINT成对写入，拒绝跨事务补齐、孤立请求和重算摘要的伪造关系/预算。
- 捕获的原账本前缀禁止修改、删除或更换运行/序号；未来新调用必须晚于准入，禁止通过UPDATE回填到历史。旧128/129/130、父run/work/attempt/checkpoint、Incident和活动槽位均保持。
- POST /api/v1/investigations/{run_id}/reinvestigation-admission只允许可信engineer/manage_incident，精确绑定原始UTF-8正文hash、规范命令、路径资源、主体、凭据与授权时刻。新建201，完整命令及主体精确重放200，同键换正文或同根换键/主体409；正常业务404/409保存审计，损坏的权威来源拒绝且不伪装业务失败。
- GET同路径区分不可变record和当前pending/current/stale/expired/blocked；GET /commands按游标读取业务审计。legacy_local也要求Bearer。GET无请求的pending只表示未创建，不授予准入资格。旧请求不因后续告警、预算变化或时限经过而改写。
- 写后认证或期限失效整事务回滚，数据库提交时也检查实际原deadline；错误审计绑定判定时刻，避免写审计时恰好越过deadline而改成另一错误原因。35个历史测试文件仅替换最新head断言，已与HEAD逐文件比较。

## 测试方法与结果

验收日期2026-10-27。按测试名去重共2,400项不同用例取得通过记录；2,386项可由本片JUnit报告逐项核对，另14项原生用例依据实际工具运行输出核对。未把中间失败或重叠定向测试重复计为通过，原生完整批次也没有被描述为一次全部通过。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元及最终策略补验 | 全量2,371项通过，69.501秒；随后原每任务工具上限补强，51项策略单元加1项PostgreSQL预算函数对照共52项通过，17.069秒。50项单元重叠，最终不同单元共2,372项，新策略51项 |
| 双来源及API对抗 | 6个实例逐项通过：human-final1项287.354秒；api-final批次3项通过、1项夹具失败，293.664秒；boundary-diagnostic批次crash1项通过、unknown旧断言失败，126.845秒；修正版unknown-final1项76.575秒。最终每个测试均有通过记录 |
| 原生完整批次与补验 | 最新15项批次前14项通过，末项在130前置采样时效校验失败，尚未进入131；独立180秒原截止提交门禁通过。随后SAVEPOINT/跨事务补齐专项及最终双来源prefix2项通过，修正稳定ID、前缀删改/换run/回填时间与序号均有目标验证，不重复累计 |
| 主Agent独立截止验收 | 1项通过，195.240秒；从根创建时固定原180秒策略，真实完成调查/执行/采样/130来源，写入131请求与成功审计后等到真实deadline，提交拒绝并确认两表及父事实整体回滚。该项计入原生15项 |
| 旧链路回归 | 6项通过，61.673秒；通用迁移3项及MVP配置、Redis、可信审批幂等/主体审计3项 |
| 静态与导航 | Ruff通过，932文件格式一致，Mypy547源文件通过；唯一head0086_reinvestigation_admission，文档链接及Git差异检查通过 |

最终JUnit为忽略的.pytest_cache/slice131-unit-final.xml、slice131-budget-limits.xml、slice131-regression.xml、slice131-api-human-final.xml、slice131-api-final.xml、slice131-api-boundary-diagnostic.xml、slice131-api-unknown-final.xml及slice131-deadline.xml。API各测试取最后成功结果，中间诊断报告的failure保留。原生子任务此前未启用JUnit，只记录真实命令与输出，不补造XML；最终prefix2项exit0的工具墙钟约226秒，不冒称pytest内部精确耗时。

开发阶段Docker未运行的设置失败未计入验收。早期响应错误信封断言、SQLAlchemy把字符串冒号99识别为绑定参数、篡改值与原值相同等测试问题均修正后复验。追加旧已完成任务的伪造在途调用最初误期待blocked，完整异常链确认原交接拒绝该归属是正确行为；最终改为409负向断言，清理隔离夹具后原record/current恢复，没有放宽生产来源。部分准备过程未完成或130样本超期的失败保留，独立重新准备通过，不推断未经验证的负载原因。

未知用量用隔离夹具模拟供应商未报告，原allocation保持；GET截止诊断与写后过期回滚使用应用时钟注入，GET检查后恢复真实时钟再重放，不把它描述为已经实际等待原deadline的重放。真实数据库截止由上述180秒独立提交测试证明。

主要复现命令：

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice131-unit-final.xml
.venv/Scripts/pytest.exe tests/unit/investigation/test_reinvestigation_admission_policy.py tests/integration/test_reinvestigation_budget_limits.py -q --junitxml=.pytest_cache/slice131-budget-limits.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_admission_api.py -q
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_admission_migration.py -q
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_admission_migration.py::test_0086_actual_deadline_is_checked_again_at_commit -q --junitxml=.pytest_cache/slice131-deadline.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_admission_migration.py::test_0086_request_requires_same_transaction_applied_audit -q
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_admission_migration.py::test_0086_native_budget_matches_python_and_success_is_immutable -q
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice131-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

仅独立Testcontainers PostgreSQL17.6、假模型与固定HTTP传输；未升级用户Compose数据库、调用真实模型或连接生产。本片无前端变更，未重跑前端构建；测试报告与缓存未提交。

## 限制与下一步

请求始终not_dispatched，没有新增run、排队任务、Worker、模型调用、预算预留、图恢复或前端入口；预算绑定和当时容量门槛不代表实际调用已经继承运行预算。准入固定一次，当前没有撤销、重开或预算追加；新告警或依据变化后不自动替代原请求。真实生产可用率、恢复因果与生产部署均不在本片验收范围。

下一片132定义独立回流阶段的冻结输入、领取身份与首次原账本预留，先明确原轮次/任务容量、上下文和版本，再连接实际模型调查；保留旧图END和checkpoint，不用新图直接恢复旧快照。

## 面试表达

“我先把验证失败后的回流准入做成完整业务边界：可信工程师必须引用精确失败决定，数据库重新校验来源、事故版本和后续告警，每根最多一次。请求绑定原始预算账本和总截止，未知用量继续占用，准入与成功审计原子提交，提交超期整笔回滚。历史按当时账本前缀重建，当前适用性单独展示。实际回流另建受租约约束的阶段，在原账本重新预留，避免用新run绕过累计预算。”
