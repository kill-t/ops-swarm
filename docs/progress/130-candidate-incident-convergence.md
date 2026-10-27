# 130 - 候选处置结果与Incident受控收敛

## 目标

在main的14101c5上消费129收尾事实，完成候选处置后的事故状态收敛。继续前核对AGENTS、PROJECT_STATE、进度索引、129设计/验收、64/65、Git与实际代码；初始仅用户output/未跟踪目录，未读取或提交。主Agent负责设计、应用层与独立提交时效验证，两个职责独立子Agent分别负责原生迁移和双源API验证，主Agent核对结果并统一提交。

## 设计与边界

事实来源为[专项设计](../architecture/130-candidate-incident-convergence.md)。父run的settled与Incident恢复分别管理：恢复成功必须有同次三份有效业务样本，fresh_until取原128窗口结束和最早observed_at加120秒的较早者。创建及数据库实际提交均严格早于该时刻，不能用收尾时间续期。

新鲜恢复按既有complete_mitigation、confirm_recovery迁移mitigating→verifying→resolved，释放活动槽位；明确业务验证失败按complete_mitigation、fail_verification回investigating，但继续持槽，等待继承原预算的有界回流。未知执行、未执行、数据源失败或效果被替代进入held_uncertain；过期、新告警及事故依据变化分别持久化held_stale、held_new_alert、held_incident_changed，均不改变Incident或释放槽位。判定优先级为依据变化、未知、过期、新告警、健康结论。

原128/129正文和false字段、父run原settled时刻、交接/work/attempt、预算和checkpoint保持。每run只有一份不可变决定，不自动覆盖held决定，不自动关闭事故、刷新样本、重启调查或沿用旧批准执行新动作。

## 实际改动

- 新增incidents/convergence，分别承担契约、纯策略、完整历史读取、事务组织、ORM、只读API和独立CLI。复用129完整来源读取端口，避免复制执行/采样历史校验或产生递归。
- 0085_candidate_convergence新增唯一收敛事实和run.candidate_slot_released_at投影。事实、合法两跳、相邻版本事件与成功marker同事务；数据库重建来源、判定和事件，拒绝孤立事实、跳过verifying、缺事件、伪造前态版本、跨事务补齐或手写marker。held不能通过先改版本再迁移绕过。
- 活动唯一索引继续覆盖queued/running/await_human/await_candidate及marker为空的settled。只有原事务中的recovered可写marker；物理INSERT验证成功分支让出槽位、失败分支仍命中唯一约束。该测试随后回滚，没有声称已交付新调查。
- 创建持有父work、原candidate和Incident锁，检查当前最新告警；历史只核对已保存的告警ID。告警接收后可能等待锁，其实际入库晚于收敛，不能用未来数据重选MAX破坏旧决定。resolved后的迟入库同指纹告警可正常重开，迁移时间取接收时刻与当前更新时间的较晚者，原告警接收时间保持。
- 新增可信Bearer/read GET /api/v1/investigations/{run_id}/incident-convergence；legacy_local仍要求可信身份。GET不推进，未处理返回空，来源损坏返回完整性错误。既有SSE重放已有incident.status_changed事件，不新增进程内事件渠道。
- 独立python -m opsswarm.incidents.convergence.main支持--once和持续扫描，只有数据库访问，不增加模型/网络探测或默认Compose服务。35个历史测试文件仅同步最新head断言，已逐文件与HEAD比较；历史迁移内容不改。

## 测试方法与结果

验收日期2026-10-27。读取JUnit报告确认零失败、零错误、零跳过，按classname/name去重共2,347项不同用例通过；定向补验不重复累计。报告保存于忽略的.pytest_cache/slice130-*.xml，不提交。

| 范围 | 实测结果 |
| --- | --- |
| 全量单元 | 2,321项通过，82.935秒；其中新策略26项覆盖结论分支、时效/优先级、来源身份和冲突 |
| 双来源恢复API | 2项通过，404.080秒；原交接/人工重评候选均完成两跳、并发单胜、marker与历史读取，后续可信关闭或迟入库告警重开仍可读 |
| 其余API分支 | 6项通过，585.050秒；明确失败、来源失败、执行未知、真实时钟样本过期、新告警、鉴权/来源损坏/flush崩溃回滚 |
| 原生迁移 | 11项通过，520.237秒；metadata/迁移往返、完整来源、不可变、伪造/孤立状态事件、held绕过、原子两跳和实际唯一索引范围 |
| 独立提交时效 | 1项通过，222.614秒；保存完整正向事务后实际等到fresh_until，提交精确命中时效拒绝，全部事实/事件/marker回滚；重扫产生held_stale并保留槽位 |
| 旧链路回归 | 6项通过，50.616秒；通用迁移3项及MVP配置、Redis、可信审批幂等/主体审计3项 |
| 定向补验 | 精确前态事件锚点1项106.032秒；成功/失败两分支物理新queued INSERT 2项163.512秒。均与原生11项重叠，不累计 |
| 静态检查 | Ruff通过，919文件格式一致，Mypy539源文件通过；唯一head0085_candidate_convergence，CLI --help通过；文档链接及Git差异检查通过 |

主要复现命令如下。两组API合起来覆盖完整8项，原生补验针对最后增加的锚点和索引断言。

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice130-unit.xml
.venv/Scripts/pytest.exe tests/api/test_candidate_convergence_api.py -k two_sources -q --junitxml=.pytest_cache/slice130-smoke.xml
.venv/Scripts/pytest.exe tests/api/test_candidate_convergence_api.py -k 'not two_sources' -q --junitxml=.pytest_cache/slice130-api.xml
.venv/Scripts/pytest.exe tests/integration/test_candidate_convergence_migration.py -q --junitxml=.pytest_cache/slice130-native.xml
.venv/Scripts/pytest.exe tests/integration/test_candidate_convergence_deadline.py -q --junitxml=.pytest_cache/slice130-deadline.xml
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice130-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.incidents.convergence.main --help
git diff --check
```

只使用隔离Testcontainers PostgreSQL17.6、假模型与固定HTTP传输；没有升级用户Compose数据库、调用真实模型或连接生产。本片无前端修改，没有重新执行前端构建。健康API两项运行后增加的事件数量/前态锚点核对由最终原生与其余API、独立提交时效测试补验；没有把修改前记录描述为整仓最终重跑。

## 限制与下一步

恢复样本仅证明有限模拟业务窗口，不证明生产可用率或恢复因果。resolved继续由既有可信流程关闭；新告警可重开。明确失败仍占活动槽位，held暂无新证据替代或人工解决命令；独立进程没有默认部署进Compose。图内interrupt/resume、多步骤处置和前端展示仍未交付。

下一片131先做验证失败的有界回流准入与原预算继承：绑定130 observed_failure和前序运行，限制次数、原总截止与累计账本，验证可信命令、幂等和并发；再接实际调查分派，不能直接创建无关联的新运行清零预算。

## 面试表达

“我把候选运行结束、业务恢复和事故关闭分成三个事实。系统重新核验完整执行与采样来源，只有样本仍在固定期限、事故版本未变且没有更新告警时才执行合法两跳；数据库在提交时再次检查时效，保证事实、事件和成功槽位释放一起生效。验证失败回调查态但保留活动槽位，方便后续继承原预算；未知和过期保留明确原因。历史引用当时告警和不可变来源，因此后续人工关闭或告警复发不会抹掉当时的判断依据。”
