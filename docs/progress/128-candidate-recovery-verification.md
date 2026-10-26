# 128 - 候选发布后的客观恢复验证

## 目标

在main的9c612ba上，为127已结算执行增加独立客观业务验证。继续前核对AGENTS、PROJECT_STATE、进度索引、127设计/验收、64/65相关章节、Git及实际代码；初始只有output/用户未跟踪目录，本片不读取或提交它。

## 设计与边界

事实来源为[专项设计](../architecture/128-candidate-recovery-verification.md)。执行成功只证明原操作历史，不能用target_version、effect_current或进程/health证明业务恢复。独立Worker只读固定订单业务探针；每份样本绑定唯一执行任务、完整结果摘要、operation及独立尝试。恢复结论表示有限窗口内的隔离演练观察，不是生产证据或因果证明。

窗口从原执行completed_at起算120秒，并取原运行总期限较早者；最多3个30秒租约尝试，每次最多3份、间隔至少2秒，最多9次只读请求。重试不重置窗口，不跨尝试拼接成功。沿用确定性验证器边界：不创建模型/专业工具循环调用，不改原账本，只读探针受自身冻结采样上限与原总期限限制。父run/work/handed_off尝试、Incident/事件、审批/计划/执行历史、图和活动槽位保持。

## 实际改动

- 新增verification/candidate，按严格契约、纯判定、固定探针、原执行来源重建、历史查询、租约仓储、Worker、HTTP和CLI拆分职责。Worker自动发现127终态；没有新增HTTP写命令或沿用历史授权写出。
- 0083_candidate_recovery保存job/attempt/sample/result四表。一份执行唯一验证任务，窗口/摘要/operation不可改；逐次采样独立事务提交，当前租约与完整原历史在每次请求前及保存/结算重验。连续尝试/样本、严格来源和时间、确定性原因码、原子终态及有数据拒绝降级由原生约束保护。
- applied_current的同次3份健康订单才recovered；三份中含5xx为not_recovered，来源/窗口/尝试耗尽为source_failed；unknown零探测形成execution_unknown，未执行形成not_executed；superseded以明确原因表示原操作恢复未确认，不冒充健康失败证据。
- GET原execution-job路径下/recovery-verification返回任务/尝试/样本/结果；未发现为空，不触发网络探测。legacy_local也要求可信Bearer；规范版本和read权限沿用现有身份入口。
- 探针仅本地白名单单GET，默认总超时3秒、最多10秒，禁重试/跳转/代理/压缩，正文16KiB封顶；2xx必须显式完整订单身份，拒绝重复JSON字段、非有限数和默认补齐；5xx保存失败状态，网络错误只保存稳定脱敏代码。
- 新写未来时间上界容忍1秒宿主/数据库偏差，租约/窗口实际截止不放宽，deferred提交二次核验。严格微秒比较曾拒绝真实LiveClock样本；另起隔离PG测8次差值绝对最大1.900毫秒，但没有失败瞬间的精确差值，不能声称已证明具体漂移幅度。修正只影响伪造未来时刻检查，不续期。
- 独立审查补齐原生与应用的原因码、UTC/UUID规范表示及过早失租终态一致性；原事实损坏整笔拒绝，不能降级为普通来源失败。历史测试只同步唯一head，不修改已发布迁移。

## 测试方法与结果

验收日期2026-10-26。按JUnit classname/name去重，最终有通过记录的验收用例共2,289项；失败批次保留，不混入通过数量。全量单元之后增加的窗口7项单独执行，未重复宣称重跑全量。

| 范围 | 实测结果 |
| --- | --- |
| 全量单元 | 2,263项通过，67.895秒，含新增契约/判定/探针51项 |
| 原期限窗口 | 7项通过，4.386秒；较早原期限、已过期限和缺失原预算拒绝 |
| 原生迁移 | 4项通过，106.490秒；metadata/空迁移往返、孤立来源拒绝、完整历史、正确摘要的原因/UTC/UUID异体拒绝、不可变和有数据禁降级 |
| 主Agent SQL对抗/提交截止 | 1项通过，127.102秒；12类非法样本、连续序号/来源/时效/身份保护，实际pg_sleep跨30秒租约后整事务回滚，原样本保留而终态不落库 |
| 恢复API与Worker整批 | 7项通过、1项准备失败，682.260秒；来源失败、执行unknown零探测、并发/逐样本、真实失租重领/旧token拒绝、三次耗尽、120秒延迟启动耗尽、可信查询及历史损坏拒绝；失败说明见下文 |
| 主Agent最终实际ASGI反例 | 2项通过，127.328秒；最新原生规则下真实126 CAS后连续健康，以及第2样本引入池故障后200/500/500仍判未恢复。与整批1项重复，补足另1项 |
| 先行独立池故障复验 | 1项通过，80.414秒，与最终反例重复，不累加 |
| 旧链路回归 | 6项通过，52.183秒；通用迁移、旧配置/Redis恢复闭环与可信审批 |
| 静态 | Ruff通过、891文件格式一致、Mypy521源文件通过；唯一head为0083_candidate_recovery、CLI --help通过，文档链接与Git差异检查通过 |

整批唯一失败在prepare里的第127步实际执行结果为unknown，尚未进入第128步采样或池故障注入；该次具体reason未保留，不能确定触发因素。未放宽30秒执行窗口或3秒请求超时，主Agent同配置独立复验该反例1项、以及最新规则下真实健康/池故障2项均通过；断言已增强为执行结果及远端调用列表。原127时限敏感性作为限制保留，不把unknown冒充已执行或恢复，也不承诺负载下执行可用率。报告保存在忽略的.pytest_cache/slice128-*.xml，未提交。

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice128-unit.xml
.venv/Scripts/pytest.exe tests/unit/verification/test_candidate_recovery_window.py -q --junitxml=.pytest_cache/slice128-window.xml
.venv/Scripts/pytest.exe tests/integration/test_candidate_recovery_migration.py -q --junitxml=.pytest_cache/slice128-native.xml
.venv/Scripts/pytest.exe tests/integration/test_candidate_recovery_guards.py -q --junitxml=.pytest_cache/slice128-guards.xml
.venv/Scripts/pytest.exe tests/api/test_candidate_recovery_api.py -q --junitxml=.pytest_cache/slice128-api.xml
.venv/Scripts/pytest.exe tests/api/test_candidate_recovery_api.py -k actual_cas -q --junitxml=.pytest_cache/slice128-final-business.xml
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice128-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.verification.candidate.main --help
git diff --check
```

只使用独立Testcontainers PostgreSQL17.6、假模型、真实ASGI演练应用；未升级用户Compose数据库、调用真实模型或连接生产。开始时Docker Desktop未运行，启动现有本地引擎后运行隔离测试，未部署新增默认服务。子任务分别实现契约/探针、迁移及API测试；主Agent实现仓储/Worker/查询整合，独立核查关键行为和提交。

## 限制与下一步

验证任务完成后不自动重开，source_failed和unknown不冒充恢复；不提供人工解决或取消入口。默认Compose未自动启动新Worker。最多三份同次订单样本不能证明长期可用率、所有实例健康或故障因果关系。

下一片129推进候选恢复结论与父运行的持久收尾，明确恢复、未恢复和未知结论的后续状态与审计，保留不可变交接/执行/采样历史；失败回流、unknown人工收敛、取消、活动槽位、前端及图恢复仍分片交付。

## 面试表达

“发布回退和业务恢复是两份独立事实。我把健康验证放到独立持久任务中，每个样本保存来源、时刻和所属尝试，只允许同一有效租约内的三份业务样本形成恢复结论。进程崩溃后重领不会延长原窗口或拼接旧样本；来源失败、执行未知都明确停止，数据库也在提交时拒绝迟到结算。版本已回退而订单仍500的反例，能检验系统是否把操作成功误当成恢复。”
