# 118 - 候选草案可信审批请求准入

## 目标

在第117步独立候选草案上提供可信审批请求，让工程师能把精确方案提交待审。基线main提交0957a44；原旧审批依赖固定根因计划，不能直接接收开放假设候选。先核对AGENTS、项目状态、进度与实际代码再划定本片。

## 设计与边界

事实来源见[专项设计](../architecture/118-candidate-approval-admission.md)。本片交付请求和阶段有效性，不提前批准或执行。复用Incident状态机的REQUEST_APPROVAL及事件/SSE，草案JSON与原角色/图保持不可变。新请求绑定plan ID、完整摘要、等待版本、迁移前后事故版本、唯一事件游标及可信授权；人工确认只代表阅读原未决条件，不代表条件已成立。

审批阶段有独立current/stale/expired规则：本次investigating→awaiting_approval不自我失效；后续人工版本、取消、事故版本、截止变化均阻止沿用。期限不超过原run截止；不调用模型或工具，不追加预算。原可信人工补充/取消入口只为同运行精确绑定的新候选待审批阶段增加访问资格，旧审批或后续mitigating不获此资格；补充后请求仍为历史stale，可继续补充。

## 实际改动

- approval/candidate按契约、纯策略、可信授权、历史查询、事务仓储、API和ORM分离职责；重建原候选和草案，不复制第二套当前系统状态。
- 新POST/GET /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request与GET /commands。engineer须具request_approval/manage_incident；严格true确认，正文上限64KiB，摘要/版本/主体/原始与规范正文全绑定。
- 迁移0074_candidate_approval增加不可变candidate_approval_requests/commands及原生约束；请求、事故迁移事件与applied审计须同事务闭环，绑定事件不可篡改，有事实拒绝降级。
- work→job→Incident锁下首次201、精确重放200、并发单胜者与404/409独立审计；写后期限或认证失效回滚请求、事件、事故及业务审计。
- 旧审批/执行保持旧表与权限；不向旧表填充伪造根因/假设，不消费旧批准。

## 测试方法与结果

本片最终去重1,931项相关Python测试通过；中途环境断开、旧进程与重复复核不叠加计数。

| 验收范围 | 最终实际结果 |
| --- | --- |
| 全量单元 | 1,490项通过，41.74秒；本片新增严格契约/期限/阶段44项 |
| 新原生迁移 | 49项通过，329.87秒；正式metadata、空往返、可信正文、真实来源/预算/期限、事件及同事务闭环、真实OLD状态与相邻版本 |
| 原迁移兼容 | 363项通过，726.77秒；含第117步44项，33个旧测试文件仅同步当前head断言，历史迁移与downgrade目标未改 |
| 候选审批请求API | 15项通过，723.62秒；双来源、并发、版本/主体/正文、历史失效及完整回滚 |
| 原人工等待API | 11项通过，247.77秒；旧可信补充、取消、并发版本、认证过期与历史读取 |
| 旧闭环API | 3项通过，42.32秒；配置回滚、Redis处置、可信审批幂等与主体审计 |
| 静态与事实核对 | Ruff通过，763文件格式一致，mypy 436源文件通过；唯一head为0074_candidate_approval，Git差异与diff --check通过 |

最终JUnit在本地忽略缓存.pytest_cache/slice118-*.xml保留并逐项核验零错误、零失败、零跳过，不随切片提交。工具会话中断后Docker服务不可用的建库失败，已恢复服务并全部重新验收；不代表真实模型或生产验收。未使用SQLite、付费模型或用户Compose数据库。

```powershell
.venv/Scripts/pytest.exe tests/unit -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_approval_migration.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration -k 'migration and not candidate_approval_migration' -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_approval_api.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_human_wait_api.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -o addopts='' -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

开发子任务按契约/纯策略与ORM、迁移/原生守卫、API集成验收分配文件所有权；主Agent整合应用事务并独立核对来源、幂等、阶段及完整差异。独立审查修正不存在候选任务的404映射，以及待审批阶段的人工输入入口，避免安全拒绝时漏业务审计。未声明切换模型或具体Token节省。

## 限制与下一步

本片请求status固定pending，GET投影expired/stale不写入，不自动释放事故等待状态。第119步实现可信批准/拒绝与过期收敛，明确独立决定事实、精确批准摘要、失效输入及自己的后续事故迁移资格；再进入受控执行。批准不代表根因客观证明，也不能跳过原未解执行条件。第119步必须把决定独立持久化，不更新本片pending请求或草案的两个false资格字段；批准产生新事故状态/版本后定义决定阶段适用性，不直接套用本片仅用于待审批阶段的current。人工阅读确认不能替代未决执行条件的确定性核验。预算追加、补查意图分派及图内恢复继续独立推进。

新请求仅有可信API及审计查询，旧审批中心/作战室聚合仍按旧表展示，尚未接入候选请求。

仅使用隔离PostgreSQL与假模型；未升级用户Compose业务库，无前端、CLI或发布变更。output/未读取、未提交。

## 面试表达

“候选草案提交审批后，事故会改变状态和版本。我把不可变计划、审批请求、迁移事件和原输入分别绑定，审批阶段按自己的精确版本校验，避免合法迁移立即让请求失效。人工补充或取消仍可发生，但旧请求随之失效；待审批不等于批准，更不等于执行授权。”
