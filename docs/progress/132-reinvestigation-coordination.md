# 132 - 独立回流协调阶段与原账本首次调用

## 目标

基线main的2d9c1ce。核对AGENTS、PROJECT_STATE、进度索引、131设计/验收、64/65、Git与实际实现后，消费唯一可信回流准入，交付独立阶段的冻结输入、真实租约尝试、原预算预留和结构化协调调用。初始仅用户output/未跟踪目录；用户文件不纳入本片。

## 设计与边界

事实来源为[专项设计](../architecture/132-reinvestigation-coordination.md)。原run继续settled并持槽，原work、父attempt、checkpoint、Round/Task及128～131历史保持。新阶段复用原run作为稳定job身份，尝试使用独立UUID及lease_token；每阶段最多3次尝试，不创建新run清零预算。

完整输入包含10类材料：原调查与失败调用、审查、全部人工输入、候选、计划、精确条件版本、执行请求/结果、恢复尝试/样本/结果、130结论及131引用。材料保留正文、稳定引用及hash，不将旧样本表述为新现场证据。下一轮严格为原最高实际轮次加一，受原轮数及累计任务数约束；容量耗尽、必要输入超窗或来源失效均零调用阻断。

模型只提出严格CoordinationProposal，没有业务工具、审批或执行权限；输出保存为not_dispatched建议，尚未创建专业任务或运行新调查。131原not_dispatched保持其历史含义，新阶段单独展示实际进度。

## 实际改动

- 新增reinvestigation_coordination包，按契约、完整材料来源、上下文、纯预算规则、任务持久化、账本、模型适配、Worker、只读查询/API及CLI分工。输出契约独立，避免预算/验证模块间循环导入。
- 迁移0087_reflow_coordination新增investigation_reflow_jobs、attempts和results。冻结source/packet及摘要，原生重建来源和完整输入；连续尝试、真实租约、调用/结果精确归属、同事务成功闭环、不可变历史和有数据禁降级由数据库约束保护。
- 新operation=coordinate_reinvestigation和reinvestigation_attempt_id引用真实新尝试；原父attempt仅保留来源引用。所有重试在原账本累计次数、Token、估算费用和总截止；未知用量继续占保守分配。预留后的累计检查不再要求额外一整次容量，自己的精确在途预留不阻断自身；其他在途调用仍拒绝。
- 新阶段reflow_budget_snapshot认识reserved及新operation；131旧函数仍只重建其历史前缀。旧父预算入口及收尾不能操作新归属调用，handoff仅加入精确来源的窄JOIN核对，避免完整来源递归。
- 父work→独立阶段→原candidate→Incident取锁，领取使用SKIP LOCKED。租约过期后的旧结果不能发布，接管把原预留记unknown并保留占用；成功调用在结果落库前崩溃可跨尝试复用，不新增模型调用。
- GET /api/v1/investigations/{run_id}/reinvestigation-coordination及/input为可信只读入口。旧调用材料三接口支持新清单/交互并重建归属；新材料要求Bearer/read，legacy_local统一拒绝可信访问，旧公开演练调用保持兼容。CLI显式--once/--continuous，未加入默认Compose服务。
- 主Agent完成设计、整合、来源/上下文/查询/API与独立验收；子Agent分别承担只读边界核查、运行层及原生迁移，最终结论由主Agent复核后统一提交。35个历史集成测试仅更新head断言。

## 测试方法与结果

验收日期2026-10-28。按classname与测试名去重、取末次结果，共2,481项不同用例有通过记录：单元2,449、API6、新原生5、旧131原生15和旧链路6。原生完整批次中有1项失败后修复并定向通过，未描述为一次整批全绿。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 2,449项通过，78.007秒，含新增77项上下文/预算/Worker/真实SDK假传输验证 |
| 旧链路回归 | 6项通过，63.277秒；通用迁移3项、MVP配置/Redis及可信审批幂等/主体审计3项 |
| 原131原生回归 | 前次完整15项通过，661.568秒；报告日期2026-10-27，属于本片工作区的前置回归，后续新增快照不修改旧函数 |
| 双来源API与恢复 | 主批次5项通过，570.197秒，包含并发准备/在途单领取、原调用未知用量、原父事实隔离、三材料接口认证、崩溃后复用、旧24,000输入阻断及模型期间新告警；最后无输入blocked阶段来源损坏检查在最终2项批次中通过 |
| 新原生迁移及对抗 | 完整批次前4项通过、最后严格提案1项失败，327.108秒；修正JSON布尔task_key被文本提取隐式转换的问题后，严格提案与API来源损坏2项通过，93.969秒。5项新原生用例均有通过记录，包括SQL/Python完整source/packet/在途budget对照、并发领取、真实租约到期提交拒绝及unknown接管、不可变和有数据禁降级 |
| 静态与导航 | Ruff、951文件格式、Mypy561源文件、唯一head0087_reflow_coordination及CLI帮助、文档链接和Git差异检查通过 |

复现命令：

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice132-unit.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_coordination_api.py -q --junitxml=.pytest_cache/slice132-api.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_coordination_migration.py -q --junitxml=.pytest_cache/slice132-native-final.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_coordination_migration.py::test_0087_native_proposal_requires_strict_shape_and_exact_round tests/api/test_reinvestigation_coordination_api.py::test_blocked_without_input_still_validates_original_admission -q --junitxml=.pytest_cache/slice132-final-boundaries.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_admission_migration.py -q --junitxml=.pytest_cache/slice132-native-preregression.xml
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice132-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_coordination.main --help
git diff --check
```

正向夹具在根运行创建时固定足够模型容量（上下文262,144、单次输入200,000、累计输入3,000,000、估算费用20美元），不是运行中追加预算。旧默认24,000单次上限的负向测试必须零调用blocked。假模型/固定HTTP传输仅证明隔离测试行为，不代表调用真实供应商或现场验证。

开发中发现并修正了原生守卫与ORM跨表刷新顺序、旧准入快照拒绝新operation/在途调用、严格提案空白目标/JSON字段类型及legacy认证断言等问题；真实租约测试最终按PostgreSQL的clock_timestamp等待到期，再验证提交拒绝，不以宿主机时钟等待推断数据库已超期；中间失败不计入验收。Docker曾未启动导致设置失败，启动后重新执行。前次子任务曾将两个新生成JUnit报告写入output/，已仅将这两个明确新文件移到.pytest_cache/，没有读取或变更原用户文件；报告及缓存均不提交。

最终JUnit为忽略目录.pytest_cache/下的slice132-unit.xml、slice132-api.xml、slice132-native-final.xml、slice132-final-boundaries.xml、slice132-native-preregression.xml及slice132-regression.xml。按测试身份去重取最后结果；完整原生批次的中间failure保留，不伪造或改写报告。

仅隔离Testcontainers PostgreSQL17.6、假模型与固定HTTP传输；未升级用户Compose数据库、连接生产或执行真实模型。本片无前端变更，未重跑前端构建。

## 限制与下一步

当前完成协调建议，不等于已执行重新调查。完整上下文超原窗口时停止；成功准入不保证后续整个调查有足够容量。未开放预算追加、held新证据、unknown人工收敛、终端取消、多步骤处置、前端或LangGraph暂停恢复。

下一片133先将当前有效协调建议确定性编译为受原容量约束的下一轮专业任务，明确稳定来源、取证窗口、独立执行归属和原预算约束，再接专业执行；不能直接重开旧END图或用新图恢复旧checkpoint。回流后新动作须重新规划及审批，不复用原批准。

## 面试表达

“验证失败回流没有重新建run或复制预算。我用独立阶段和真实租约尝试，在原账本预留完整输入后调用结构化协调模型，严格限制原剩余轮次与任务数。数据库重新计算来源、输入和调用归属，成功调用可跨尝试复用，失联调用保留未知占用；新告警、超期及迟到响应不能发布。当前产物只是待分派建议，原父运行、执行与验证历史保持，后续任务和处置仍需各自准入。”
