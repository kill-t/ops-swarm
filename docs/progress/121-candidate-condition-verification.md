# 121 - 版本化执行条件与可信核验事实

## 目标

在第120步main提交928be5f之上，将只能保留为自由文本的前置条件和未解问题，转为可逐项追溯、连续修订的可信工程师核验记录。先核对AGENTS、交接、Git、原批准和预检实现，不以原“已阅读”或参数观测推定条件成立。

## 设计与边界

事实来源为[专项设计](../architecture/121-candidate-condition-verification.md)。目录由服务端按原文、类别和序位生成稳定摘要，客户端必须完整覆盖，不接受改原文或省略条目。verified是带来源的工程师判断；unresolved继续阻断；not_applicable不是默认通过，要求重新审视方案。代码校验身份、结构、引用和期限，不证明人工解释真伪或客观根因。

每份内容固定approval_required=true、execution_eligible=false：全部verified为ready_for_review，有unresolved为blocked，其余含not_applicable为plan_revision_required。新版本是新的执行依据，未来新审批必须绑定精确版本ID/hash；第119步同request只允许一个决定，不能对它再次approve或沿用旧批准覆盖核验。原批准、120预检及父图均不改。

## 实际改动

- 新增execution/conditions，按契约、纯目录/CAS策略、可信准入、来源解析、历史重建、事务和HTTP分离。POST/GET /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions，GET /versions/{version}及/commands；始终Bearer，写入严格engineer+verify_action，executor不能凭同权限代核验。
- 来源区分原决定证据ID、明确标识的人工观察、同批准下真实预检ID/hash。非unresolved必须有来源，每项最多3个、每批最多3个不同预检；不调用模型或网络、不把人工陈述写入工具证据，不回退冻结参数。人工时间严格ISO8601、归一UTC、最多6位精度，拒绝数字时间戳和UTC越界。规范命令限24KiB、原始正文64KiB。
- 0077_candidate_conditions新增candidate_condition_verifications和candidate_condition_commands。每决定最多8版，expected_condition_version和前驱hash精确CAS；批次和applied审计同事务，历史不可变且有事实拒绝降级。相同完整命令重放200、新建201、业务404/409单独审计；同一旧版本竞争只单胜，不重采或续期。
- 按新授权→父work→job→Incident串行化；原批准完整重建一次，事务内复用预检详情，旧授权不反向加锁。当前版本绑定原批准、mitigating事故版本、原输入、无在途调用的原累计预算及原期限。核验有效期取原批准、本次300秒、人工观察300秒及引用预检截止的最小值；写后期限/凭据失败全回滚。
- GET及重放完整重建最高版本前缀，分别表达current/superseded/stale/expired；过期不清零版本，损坏最高版不回退。严格布尔校验与原始JSON实际摘要共同拒绝数值0/1掩盖损坏。所有核验、来源和审计可追溯，Incident事件、旧执行表、父run/work/handed_off尝试和预算保持。35个历史测试文件只同步当前head断言，旧迁移未改。

## 测试方法与结果

验收日期2026-10-25。按JUnit用例标识去重，共1,852项相关测试通过，采用报告均零失败、零错误、零跳过。最终领域129项与全量单元中的95项重叠，新增34项，单元合计1,790；其他补跑不重复计数。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元基线 | 1,756项通过，72.91秒，含本片早期95项领域测试 |
| 最终领域与边界 | 129项通过，5.84秒；补UTC溢出及严格布尔32项，合并后单元1,790项 |
| 新PostgreSQL原生 | 18项通过，124.75秒；元数据/空迁移往返、完整目录、可信命令、三状态连续版本、真实预检与证据、预算占用、前驱/同事务闭环、不可变和降级保护 |
| 最终原生命令补验 | 既有1项通过，21.52秒；严格时间/UTC越界、Literal、大小、来源和大整数版本审计；不重复计数 |
| 候选核验API | 9项通过，440.13秒；双来源、完整目录/状态/前驱、角色、幂等/并发、8版、来源期限、写后截止/认证回滚和历史查询 |
| 最终API完整性补验 | 既有2项通过，114.28秒；预算门禁投影及最高版三种带外损坏拒绝state/历史/审计/重放，恢复后可读；不重复计数 |
| 最终工程师权限补验 | 既有1项通过，55.71秒；最终主代码的严格角色与完整正文边界；不重复计数 |
| 相关旧链路回归 | 35项通过，245.46秒：第120步原生30项、两来源预检API 2项、旧配置/Redis闭环与可信审批API 3项 |
| 静态与差异检查 | Ruff通过，802文件格式一致；Mypy462源文件通过，唯一head 0077_candidate_conditions；最终文档链接和Git差异检查见本片提交 |

```powershell
.venv/Scripts/pytest.exe tests/unit -o addopts='' -q
.venv/Scripts/pytest.exe tests/unit/execution/test_candidate_conditions.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_conditions_migration.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_conditions_api.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_preflight_migration.py tests/api/test_candidate_preflight_api.py::test_two_sources_preserve_unverified_conditions_parent_and_old_execution_facts tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -o addopts='' -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

报告在本地忽略的.pytest_cache/slice121-*.xml，未提交。未将重复或早期失败运行累计为新通过数，也未重跑全部历史迁移/发布。API预算消费补验使用显式替代预算门禁，不能描述为真实额度语义验收；原生预算专项则在隔离事务中追加reserved/unknown审计，保持原成功用量和来源完整。

子任务按纯领域、原生守卫和API边界分配独立文件，主Agent实现事务整合并独立核实报告和关键差异。审查补齐时间格式与UTC溢出、严格布尔、原始JSON摘要和最高版本完整重建；原生成功记录经应用Store读取验证，避免夹具只在SQL侧自洽。仅使用Testcontainers PostgreSQL17.6、假模型和本地模拟传输，未升级用户Compose库、调用真实模型或连接生产系统；output/未读取、未提交。

## 限制与下一步

本片没有独立核验审批请求/决定、候选执行、新前端、父运行结算或活动槽位释放。原119决定current仅表示旧阶段当前性，不表示其覆盖新核验依据；120报告继续保持原阻断与false字段。核验到期可在原批准仍有效时追加新版本，但不能重置原期限或预算。人工判断不能冒充工具采集或客观根因证明；not_applicable须进入计划修订审视，不能绕过前置条件。

下一片122：为当前ready_for_review核验版本建立独立可信审批，精确绑定批次ID/hash、原计划/决定、事故阶段、原输入/预算/期限；新版本替代、来源失效或到期后不得沿用旧核验授权，plan_revision_required不能直接批准。随后再建设候选专用执行、发布版本状态/CAS、租约与结果、恢复验证；父运行收尾、前端及预算追加/图恢复仍独立交付。

## 面试表达

“我把原自由文本条件生成固定目录，工程师必须提交完整、有来源的核验快照。版本以精确前驱CAS追加，纠错保留历史；损坏最高版本时不能退回旧记录。已核实、未解决和不适用有不同语义，尤其不适用不会自动通过。代码保证来源、权限和时效，但人工判断仍须由新审批明确覆盖，不能把以前的已阅读批准升级成执行授权。”
