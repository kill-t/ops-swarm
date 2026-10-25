# 122 - 新核验依据的独立可信审批

## 目标

在main的e90bcf0（第121步）之上，为完整核验版本一次交付工程师申请、审批者批准/拒绝/显式过期及查询追溯。原119批准只覆盖当时依据，不能自动覆盖后来形成的核验；新请求不借用原请求的唯一决定位置。

## 设计与边界

事实来源为[专项设计](../architecture/122-condition-basis-approval.md)。请求精确绑定核验ID、版本、内容摘要以及原决定/计划引用；只有最新current且全部verified的ready_for_review版本可申请和批准。请求截止时间严格等于核验截止时间，不延长原运行、原批准或核验期限。

新请求状态固定pending，决定另存approved/rejected/expired；已有决定的请求投影recorded。新核验替代、原来源失效、预算不再允许或原期限经过，会使历史批准失效，不能因内容相似沿用旧授权。拒绝在原截止前、显式过期在原截止后，只收敛原请求，不要求当前Incident仍处于原版本；两者仍必须验证完整历史。

批准表示审批者接受精确核验依据。原核验与新内容的execution_eligible继续为false；人工verified及批准都不是客观根因证明，也不替代执行时的参数/CAS、权限、租约和幂等门禁。本片不调用模型、外部网络或执行器，不迁移Incident、不追加事故事件、不改变旧请求/决定、父图、原预算或活动槽位。

## 实际改动

- 新增approval/conditions，按严格契约、纯规则、可信准入、来源重建、历史查询、应用事务、ORM及HTTP职责分离。根路径为/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions/versions/{version}/approval-request；POST/GET根路径、POST/GET /decision及GET /commands形成闭环。
- 即使legacy_local也要求可信Bearer。请求仅engineer+request_approval，决定仅approver+decide_approval；批准必须严格布尔acknowledge_verification=true，负面动作必须false。版本路径仅规范单字符1..8，原始UTF-8正文、规范命令、主体及run/job/version三资源精确绑定，客户端不能写身份、期限或执行资格。
- 新增0078_condition_approval和condition_approval_requests、condition_approval_decisions、condition_approval_commands三表。每核验一个请求、每请求一个决定，稳定UUID与主体/完整命令幂等；首次201、精确重放200、合法业务拒绝404/409独立审计。请求/决定与各自applied审计必须同事务，历史不可变，有新事实或审计即禁止降级。
- 统一新授权→父work→候选job→Incident锁序，完整重建原批准及最高核验前缀，历史授权只读，事务内复用来源。原生复用0077历史模式，另查最新版本、当前输入/事故、原期限和累计预算；负面决定跳过当前容量要求。写后再次检查认证、原期限及批准当前性，失败整事务回滚。
- GET/重放不续期、不重写历史，最高核验损坏不能回退；实际JSON摘要和严格布尔拒绝数值0/1掩盖内容损坏。已有目标审批损坏时，即使换幂等键也不能追加普通错误审计；新核验不依赖旧核验的审批，旧审批损坏不冒充新核验来源损坏。审计的阶段、动作、HTTP状态和业务引用共同校验。
- 控制面注册独立Store、错误处理和可信操作；35个历史测试文件仅同步当前迁移head断言。原119审批、120预检、121核验、旧处置链和前端保持原行为。

## 测试方法与结果

验收日期2026-10-25。按JUnit的classname/name去重，共1,919项相关测试通过；采用报告均零失败、零错误、零跳过。最终领域72项与全量单元重叠，最终完整性API补验1项与API边界重叠，均不重复计数。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 1,862项通过，52.34秒，含本片72项纯领域规则 |
| 最终领域补验 | 既有72项通过，5.55秒；补审计结果/HTTP状态/业务引用一致性，不新增计数 |
| 新PostgreSQL原生 | 21项通过，235.28秒；元数据/空迁移往返、双阶段可信准入、严格正文、双来源事实、同事务闭环、不可变/降级、最高核验损坏、原预算和负面收敛 |
| 审批API主链 | 5项通过，357.31秒；双来源、精确请求/决定/重放、原事实隔离、版本替代、并发单胜与角色/资源边界 |
| 审批API边界 | 8项通过，407.85秒；拒绝/显式过期、真实unknown容量耗尽及在途快照、四类写后截止/认证回滚和历史损坏 |
| 最终完整性补验 | 既有1项通过，141.01秒；8类带外损坏拒绝查询、原键/换键写入，不降级为错误审计；恢复后可读 |
| 相关旧链路 | 23项通过，290.67秒；第121步原生18、两来源核验API 2、旧配置/Redis闭环和可信审批API 3 |
| 静态检查 | Ruff通过，815文件格式一致；Mypy471源文件通过；唯一head为0078_condition_approval；550个本地文档链接有效，git diff --cached --check通过 |

API主链与边界使用互斥-k筛选，合计覆盖本文件全部13项；最终完整性补跑覆盖最后提交的换键完整性守卫。报告保存于本地忽略的.pytest_cache/slice122-*.xml，未提交；旧失败报告不作为验收依据。

```powershell
.venv/Scripts/pytest.exe tests/unit -q
.venv/Scripts/pytest.exe tests/integration/test_condition_approval_migration.py -q
.venv/Scripts/pytest.exe tests/api/test_condition_approval_api.py -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_conditions_migration.py tests/api/test_candidate_conditions_api.py::test_two_sources_keep_exact_catalog_three_states_and_old_facts tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

测试仅使用隔离Testcontainers PostgreSQL17.6和假模型。预算耗尽在隔离事务中追加unknown账本审计，保留原成功调用用量与完整来源；在途容量使用显式预算快照注入，不把已交接父调查上的非法新预留描述为合法调用。原生事实还经过应用与HTTP读取校验。Docker引擎曾未启动，启动后重跑；早期失败、重复运行和尚未完成的报告不累计为验收通过数。

按纯领域、数据库守卫和API边界分配独立文件；两个子任务因额度限制中断，由主Agent接手完成、核验及统一提交。没有升级用户Compose数据库、连接生产、调用真实模型或发布；output/未读取或提交。

## 限制与下一步

没有候选实际执行、撤销批准、新前端、定时过期Worker、父运行结算或活动槽位释放。原图v15已经END，本片不是图内interrupt/resume。负面决定只收敛核验审批，Incident仍保留当时的实际状态；已经批准后不再追加第二个过期决定，后续过期以当前适用性表达。

下一片123先补齐本地演练发布版本状态与CAS前置能力：现有release-500只证明开关，旧执行器关闭开关后回显target_version不能证明部署版本。先明确持久状态、参数漂移、重启和幂等语义，再将当前精确核验批准接入候选专用执行任务、租约/结算及恢复验证；旧MVP语义保留，父运行收尾另行交付。

## 面试表达

“我把核验后的审批单独建模，批准精确覆盖某个不可变核验版本；新版本出现后旧批准保留历史但失去当前适用性。申请与决定各自和可信审计原子提交，重复请求只能重放原事实，不能续期。拒绝和过期允许在预算耗尽后收敛，但历史损坏仍然拒绝。批准本身不执行操作，执行层还要验证当前参数、CAS和租约。”
