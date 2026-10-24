# 117 - 候选评估结果的可信规划准入

## 目标

第116步保存候选可行性，但没有可审阅的ActionPlan草案。本片让可信工程师显式请求确定性规划，把精确候选结果、开放假设、引用证据与未决条件绑定到独立草案。

## 设计与边界

依据[专项设计](../architecture/117-candidate-plan-admission.md)，基线main的e35a1f0。旧action_plans强制绑定固定RootCause的假设，旧审批/执行也没有候选来源资格检查；本片采用独立candidate_action_plans复用ActionPlan值对象和action-risk-v3，保持旧表、旧API及批准摘要不变，避免候选污染旧“当前计划”查询。

草案固定v1、draft、单步骤、不可变，approval_eligible/execution_eligible均false。模型前置条件与未解问题完整保留，不声称已经满足。原候选仍是非授权事实，不生成旧HypothesisRow，不改变run/work/Incident/checkpoint，不自动进入审批或执行。

可信Bearer工程师必须拥有plan_action和manage_incident，完整原始正文、规范命令、路径run/job和主体绑定实际授权。work→candidate job→Incident锁下重建原候选成功链，校验完整结果hash、等待版本、事故乐观锁、当前来源、原截止与共享账本；无新模型/外部调用，不新增预算预留。次数等于上限允许，超过上限或原Token/费用/分配超额则拒绝。创建后source/Incident版本或截止变化单独投影stale，历史正文和精确重放不改。

## 实际改动

- planning/candidate按契约、纯构建、预算、来源查询、可信授权、用例持久化与HTTP分离职责。草案从重新评估的原候选step构建，独立结果包含开放假设、证据ID、前置条件和未解问题。
- 0073新增计划及命令两张不可变表，同run复合FK、每候选唯一草案、主体幂等、真实授权/正文/命令绑定和deferred同事务applied审计闭环；原生核验实际来源、步骤、风险、版本和原预算，有事实拒绝降级。
- POST/GET /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan及GET /commands，201首次、200完整重放、404/409拒绝审计；GET不写入。写后截止或凭据期限失效整体回滚。
- 原规划/审批/执行表不改，旧ID空间和流程隔离。测试通过真实候选链与真实PostgreSQL，模型使用假端口。
- 主Agent负责架构、来源、API与整体验收；两个职责独立的子任务负责契约/纯规则/单测及DDL/原生测试，并只读复核主任务代码。事故版本/截止投影、累计次数及授权锁后401错误映射由主Agent核实修正。

## 测试方法与结果

本片验收通过，相关Python测试最终去重 **1,827项通过**：全单元1,446、迁移363、新候选草案API 15、旧链路API 3。重复首轮验收不叠加数量，未选场景不宣称本轮通过。

| 验证范围 | 实际结果 |
| --- | --- |
| 全量单元 | 1,446项通过，56.32秒；本片新增构建/契约29项、预算16项 |
| 新原生迁移 | 44项通过，275.44秒；正式metadata、空表往返、原始正文/授权/严格版本、不可变与闭环 |
| 原迁移兼容 | 319项通过、477项未选，401.48秒；旧文件仅同步当前head断言，与新44项不重叠 |
| 最终草案与旧链路API | 17项通过，498.08秒；新草案14项与旧配置回滚、Redis处置、可信审批3项 |
| 授权行锁后凭据到期 | 1项通过，52.93秒；新API合计15项，401不被误报为403，计划/业务审计整体回滚 |
| 静态与事实核对 | Ruff通过，751文件格式一致，mypy 428源文件通过；唯一head为0073 |

覆盖两种真实候选来源、原开放假设/步骤与未决条件保留、共享账本零新增调用、严格权限/完整正文、并发精确重放、hash/等待/事故版本冲突、未完成或unsupported拒绝、补充/取消后历史stale、原截止与凭据期限写后回滚、生成后事故版本/截止变化投影、原生不可变与有事实拒绝降级、旧审批/执行404及旧流程兼容。首轮12项API与后续重复检查不叠加计数。

独立审查补齐事故版本/截止投影、总次数与任务工具次数护栏、未知计数不补零，以及授权行锁后过期的错误映射。原生成功调用校验按真实CandidateModelResult保留kind字段；完整来源散列的浮点表示问题按下述边界处理。最终Git差异、暂存范围、文档链接和git diff --check在提交前统一核对。

```powershell
.venv/Scripts/pytest.exe tests/unit -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_plan_migration.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration -k 'migration and not candidate_plan_migration' -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_plan_api.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_plan_api.py::test_auth_expired_after_authorization_lock_preserves_401_and_rolls_back -o addopts='' -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

完整source包含工具浮点属性，PostgreSQL与Python指数表示可能不同；延用0072边界，完整source散列由应用重建核验，原生独立核验实际JSON来源链、packet与固定阈值步骤，避免合法材料被跨序列化文本误拒。未把未知用量记零，未调用付费模型或生产环境。

## 限制与下一步

本片是实际可查询的不可授权草案，不是可信审批或处置执行。下一片118设计候选草案的可信审批准入与精确版本/证据/当前适用性绑定，再接执行，不继承任何旧批准。人工约束解除、预算追加、补查意图分派和图内interrupt/resume仍是独立后续能力。

未升级用户Compose业务库，没有前端改动；output/未读取、未提交。

## 面试表达

“我复用ActionPlan与风险规则，但把开放假设候选和历史根因计划隔离存储。工程师只能选精确候选，服务重建证据、步骤和原预算，保留全部未决条件。计划与可信审计原子提交，事故版本、人工输入或截止变化后仍能追溯历史，却不能创建新草案或借旧审批执行。”

后续审批会迁移Incident状态与版本。第118步须定义审批/执行阶段的独立来源有效性规则，绑定新批准事实；不能直接套用本片仅用于草案准入的current判断，让阶段自身状态迁移失效，也不能据此跳过真实输入变化的复核。
