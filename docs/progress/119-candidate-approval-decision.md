# 119 - 候选审批请求的可信决定与过期收敛

## 目标

在第118步（main提交b3bafa0）不可变pending请求上建立真实可信批准、拒绝与过期收敛；不把旧审批表或历史根因批准借给开放假设候选。先核对AGENTS、交接、实际Git和相关代码。

## 设计与边界

以[专项设计](../architecture/119-candidate-approval-decision.md)为事实来源。新决定独立持久化，不更新请求、draft计划或false资格字段。批准复核完整原来源、精确事故阶段、原累计预算与期限；拒绝允许输入已失效但未过期，过期通过显式可信expire命令收敛，即使没有剩余模型额度也可撤销等待，不自动改为批准或重置预算。

批准后原请求按pending阶段自然stale；新决定按mitigating新版本独立投影，避免自身合法迁移立即失效。人工阅读确认不是未决条件已满足，也不是根因客观证明。批准后的人工补充/取消仍按第118步拒绝，不提供隐式撤销批准接口。

## 实际改动

- approval/decision按严格契约、纯策略、可信原始正文准入、历史查询、事务仓储、API和ORM分离。
- POST/GET 原approval-request下/decision，GET /decision/commands；始终Bearer approver+decide_approval，原始正文/规范命令/资源/真实主体精确绑定。动作approve/reject/expire及严格bool确认，客户端不得覆盖方案、期限或执行权限。
- 0075_candidate_decision新增candidate_approval_decisions和candidate_decision_commands；每请求一个稳定不可变决定，保存完整原证据/条件引用、内容散列、前后事故版本及唯一事件。决定/新Incident事件/applied审计原子提交，原生验证真实OLD状态与相邻版本，有事实拒绝降级。
- approve迁移mitigating，reject/expire收敛investigating；work→job→Incident锁下精确同主体幂等与并发单胜，404/409业务拒绝独立审计。写后期限/预算/凭据失效回滚全事务，GET不写入。
- 原始source含任意浮点的散列继续由应用完整重建，原生核实际JSON链；负面决定的历史重建不要求剩余额度，不能因超时/额度用完锁死过期收敛。

## 测试方法与结果

验收跨2026-10-24～25进行，最终按JUnit实际用例标识去重共2,042项相关Python测试通过。本片新增纯契约/状态/阶段65项；原生55项与当前收集结果逐项相符，所有采用报告均零错误、零失败、零跳过。

| 范围 | 实际验收结果 |
| --- | --- |
| 全量单元 | 1,555项通过，69.53秒 |
| 历史迁移兼容 | 412项通过，1,072.44秒；34个旧测试文件仅同步head断言，旧迁移及降级目标未改 |
| 原生决定与命令 | 主50项通过，1,086.33秒；新增UUID格式5项通过，34.04秒，去重共55项 |
| 原生锁顺序补验 | 既有1项重跑通过，69.19秒；持有旧授权锁时新决定仍可完成，不重复计数 |
| 原生预算与完整性补验 | 既有4项重跑通过，108.04秒；累计unknown审计超额可负面收敛，原成功用量损坏拒绝，写后历史可由应用重建，不重复计数 |
| 候选决定API | 17项通过，1,042.97秒；双来源、精确幂等/并发、权限、补充取消后的负面收敛、历史失效与写后回滚 |
| 最终两来源API补验 | 既有2项重跑通过，112.48秒；最终SQL批准不自我失效，旧表和执行隔离，不重复计数 |
| 旧闭环API | 3项通过，34.70秒；配置回滚、Redis处置、旧可信审批幂等及主体审计 |
| 静态检查 | Ruff通过，775文件格式一致；Mypy 444源文件通过，唯一head 0075_candidate_decision；完整Git差异、文档链接与diff --check通过 |

JUnit保留在本地忽略的.pytest_cache/slice119-*.xml，未提交。中途测试夹具修正和重复复核不累计为新增通过数，不把跨日结果表述为整仓所有集成/发布测试。

```powershell
.venv/Scripts/pytest.exe tests/unit -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_decision_migration.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration -k 'migration and not candidate_decision_migration' -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_decision_api.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -o addopts='' -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

开发子任务按契约/纯策略/ORM、原生迁移与守卫、API边界验收分配独立文件；主Agent实现应用事务并核对真实结果与完整差异。单次来源重建后解码同一不可变正文，避免一次决定里重复递归遍历。原生对审批阶段和预算条件分别处理，不为过期收敛偷偷续期。独立审查移除历史授权的反向行锁，并用旧授权持锁复核；UUID文本转换与Pydantic逐例比对。25日复核将累计超额改为追加unknown审计，原成功调用保持完整；同时补齐原生成功用量完整性检查及原生/应用历史读取一致性，损坏来源不因负面决定而放行。

## 限制与下一步

本片没有候选执行或新前端展示；旧审批中心和作战室仍按旧表，不把新决定描述为前端闭环。过期为显式可信命令，无定时Worker或GET副作用；同请求只有一个终态决定，不能在批准后再写第二个expired决定来隐式撤销，已批准过期按availability失效。父run/work/handed_off尝试及图保持，拒绝/过期仅收敛Incident，未释放父活动槽位；终端取消、父运行后续结算与新调查准入须另片交付，不能宣称整个工作流结束。

下一步120：候选受控执行准入，精确绑定有效新批准并确定性核验运行参数与原未决前置条件，不能用旧ActionStep/ApprovalRequest执行，不修改本片false执行字段。预算追加、图内恢复及更多专业现场能力仍独立推进。

仅使用隔离Testcontainers PostgreSQL与假模型；未升级用户Compose业务库、未调用真实模型或生产系统；output/未读取、未提交。

## 面试表达

“我没有把pending请求直接改成approved，而是单独保存决定及绑定的新事故事件。批准会重新核对原输入、版本和预算；拒绝/过期只撤销等待，不能因额度耗尽无法收敛。决定的阶段检查承认自己的合法状态迁移，但输入、事故或期限变化后不能沿用，也不能把人工判断当作条件已经被验证。”
