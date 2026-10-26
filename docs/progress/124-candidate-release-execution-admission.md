# 124 - 候选发布执行的可信准入与持久请求

## 目标

在main的283a913（第123步）上，将有效核验批准、原步骤与当前演练版本/CAS修订绑定成不可变发布执行请求。先核对AGENTS、交接、Git和122/123实际接口，不把旧执行器回显目标版本当作新执行结果。

## 设计与边界

事实来源为[专项设计](../architecture/124-candidate-release-execution-admission.md)。核查发现123没有远端按命令键只读查询和受限派发/租约恢复协议；响应丢失后再次PUT可能成为新写入。原123记录中的“首个候选执行链路”目标因此在本片收敛为可信准入、只读采集和持久请求，状态prepared、dispatch_eligible=false、requires_dispatch_recheck=true；没有创建无消费者的空壳Worker或声称已执行。

准备只支持原计划order-service单步骤2026.01.01-rc1→stable，完整重建精确122批准、核验版本和原计划/输入，要求mitigating事故版本、原预算/期限有效。只读GET123的完整模拟状态，固定来源、服务和修订；不把旧开关、静态日志标签或冻结材料混入观测。未来操作ID按精确批准唯一，CAS参数由服务端构建，客户端不能修改参数、修订、身份、期限或派发资格。

这是准备事实，不是执行令牌或永久授权。当前投影仅表示本地审批来源和30秒观测时效，GET不证明远端此刻仍匹配；后续派发仍需新鲜核验。原核验/审批的false字段不改，原父run/work/handed_off尝试、图、Incident/事件、预算和活动槽位保持。

## 实际改动

- 新增execution/release，按严格契约、纯白名单/时限规则、可信准入、观测适配、历史查询、事务、ORM及HTTP分离。根路径为/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions/versions/{version}/release-execution-request；POST准备、GET请求和GET /commands追溯。包括legacy_local均须Bearer，写入仅executor+execute_action，run/job/规范version三资源及原始正文精确绑定。
- 0080_candidate_release_request新增candidate_release_execution_requests及candidate_release_execution_commands。每精确批准最多一份请求，独立稳定request/operation UUID及未来CAS键不借用旧执行表；请求及applied审计原子提交，唯一性、历史不可变、同事务闭环和带数据禁止降级由原生守卫保护。首次201、精确重放200、业务404/409独立审计；原键或换键都不能掩盖既有历史损坏。
- 沿新授权→父work→candidate job→Incident锁序检查完整当前来源，在锁内做有界只读GET，返回后再次检查凭据、原期限/预算与采集窗口；写后越过观测/原期限或凭据到期整体回滚。expires_at=min(原核验截止,observed_at+30秒)，不续期、不创建新调用额度。它是显式操作者准备接口，不增加模型或Agent工具调用。
- 观测器仅访问既有本地地址白名单的/__admin/simulated-release，异步总超时、64KiB上限、禁止压缩/重定向/环境代理。必须先验证原始完整字段，再构造DTO，拒绝缺少source/service或simulation=1被默认值/类型转换掩盖。日期严格带时区ISO8601并统一UTC，拒绝未来状态、错误修订、epoch和旧观测；网络失败不回退。
- GET/重放完整重建审批与最高核验前缀，返回pending/current/superseded/stale/expired；不重新联网或续期。内容及实际JSON摘要共同检查，数字0不能伪装dispatch_eligible=false。35个历史测试文件只同步当前head断言，已发布迁移未改。

## 测试方法与结果

验收日期2026-10-26。按JUnit的classname/name去重，共2,059项相关测试通过，采用报告均零失败、零错误、零跳过；领域/观测专项与全量单元重复，独立接入补验与API重复，均不新增计数。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 2,032项通过，113.59秒，包含新增领域84及观测器43项 |
| 领域专项 | 84项通过，3.60秒，与全量单元重复；精确引用/只读步骤白名单、完整来源/严格时间/布尔、30秒边界与错误审计结构 |
| 观测器专项 | 43项通过，4.00秒，与全量单元重复；固定GET、完整原始身份、数值布尔拒绝、有界响应/超时、重定向/压缩/传输失败及配置护栏 |
| PostgreSQL原生 | 12项通过，178.80秒；元数据/空迁移往返、可信准入/原正文、双来源成功事实、预算、观测/CAS、savepoint同事务闭环、不可变/过期重放及最高版本损坏拒绝 |
| 发布请求API | 9项通过，437.40秒；双来源真实123 ASGI只读接入、操作/CAS与父事实隔离、权限/三资源、并发一次采集、预算/观测错误/期限/替代、写后回滚与原键/换键损坏拒绝 |
| 主Agent独立接入补验 | 1项通过，58.75秒，与API重复；再次核实真实123端口只GET一次、重放不采集、lab状态与原父事实不变 |
| 相关旧链路 | 6项通过，58.19秒；通用迁移元数据/完整往返/历史工件保护3项，旧配置/Redis闭环及可信审批API3项 |
| 静态 | Ruff通过、839文件格式一致、Mypy486源文件通过；唯一head 0080_candidate_release_request；575个修改文档的本地链接有效，Git差异检查通过 |

```powershell
.venv/Scripts/pytest.exe tests/unit -q
.venv/Scripts/pytest.exe tests/unit/execution/test_release_request.py tests/unit/execution/test_release_observer.py -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_release_request_migration.py -q
.venv/Scripts/pytest.exe tests/api/test_candidate_release_request_api.py -q
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

报告保存于本地忽略的.pytest_cache/slice124-*.xml，未提交。最初Docker引擎管道缺失，启动后重跑；早期失败不累计。子任务分别负责纯领域、原生守卫与API/观测边界，主Agent实现事务整合、修正默认来源填充漏洞、独立核对关键路径、报告和差异后统一提交。预算耗尽使用隔离事务追加unknown审计，原成功调用用量和完整来源保留。

仅使用隔离Testcontainers PostgreSQL17.6、假模型和本地ASGI传输；真实123接入表示实际演练应用代码与数据库接口，不是新Compose发布或生产遥测。未升级用户Compose库、调用真实模型、连接生产或执行发布写入；output/未读取、未提交。

## 限制与下一步

请求准备成功不等于执行资格或已排队派发，未交付Worker、外部CAS写入、未知结果人工收敛、恢复验证或父运行结算。到期/替代的准备事实保留历史，不自动续期或重新观测。新角色、预算和期限仍须在未来派发时重新核验；不能从历史authorization ID推断Worker届时仍有权限。

下一片125先交付远端按稳定命令键的只读回执查询和确定性对账契约，未知记录查询不能产生新写入；同时明确后续派发截止、租约与迟到结果边界，再接独立执行Worker。一次写出后unknown停止可作为另外明确设计的保守方案，但不能冒充自动恢复。恢复验证、活动槽位释放、前端和图内恢复继续后置。

## 面试表达

“我把执行准备和真正的副作用分开。准备接口只能读取当前演练版本，把精确批准、原步骤和修订CAS冻结成唯一请求；重放不会再次采集或续期。新观测必须显式包含来源，不能靠DTO默认值补出身份。当前远端还缺只读回执和派发恢复协议，所以请求明确不可派发，避免响应丢失后用一次可能产生新副作用的PUT去查历史。”
