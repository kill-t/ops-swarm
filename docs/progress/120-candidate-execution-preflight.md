# 120 - 候选受控执行准入与当前参数前置核验

## 目标

在第119步可信批准（main基线f0e8edd）之后回答“为什么当前仍不能执行”。先核对AGENTS、交接、Git和旧执行路径，确认原候选条件是自由文本，旧发布适配器只有故障开关，不足以证明当前版本参数。以独立事实保存批准引用、实际只读观测和阻断，防止把人工阅读确认升级为条件满足。

## 设计与边界

事实来源为[专项设计](../architecture/120-candidate-execution-preflight.md)。可信executor命令绑定精确决定及内容摘要、mitigating事故版本、原输入、累计预算和截止时间；调用前后均核验，不追加预算、不改父图。拟操作与检查各有独立稳定身份；完整JSON身份散列避免主体和键直接拼接的歧义。每个新键保存新观测，同键重放及GET只读历史，不刷新时间或续期。

配置与Redis按原参数比较当前值，已达目标单独报告；发布接口仅报告release_500开关，因此始终明确版本不可验证。所有原自然语言条件及未解问题完整保留、全部未验证阻断；当前策略只产生blocked和execution_eligible=false。这是可运行的确定性检查用例，不是空的执行入口，也没有授予可被未来直接消费的执行令牌。实际执行须另建条件核验、CAS和租约/结果边界。

## 实际改动

- execution/candidate分别承担严格契约、纯白名单与参数比较、异步只读观测、可信准入、历史重建、事务和HTTP转换。POST/GET /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights，GET /commands，均要求Bearer；POST仅executor+execute_action。
- 0076_candidate_preflight新增candidate_execution_preflights与candidate_preflight_commands，独立保存不可变报告及原始可信命令审计；报告与applied结果必须同事务，精确重放200、新建201、业务404/409独立审计。有事实不允许降级覆盖；不能删除事实后降级数据库。
- 观测只访问现有ExecutionSettings允许的本地地址、三个固定GET，禁代理和重定向；严格解析完整响应、64KiB流上限及总超时，失败明确保存受限错误，不回退冻结证据。观测有效期最多30秒且不超过原截止，保存原步骤、实际参数和条件，不调用模型或修改接口。确定性执行前GET不属于调查Agent工具循环，不清空/增加调查预算；独立报告可追溯这次读取。
- 事务按新授权→父work→candidate job→Incident锁顺序，旧授权历史只读。原生重建批准历史、原预算及精确阻断；应用重建稳定UUID、完整source散列和规范正文。来源损坏归一409，已有同命令事实不能降级成新error审计；写后期限/认证失效整事务回滚。
- 原draft/pending/approved事实、false字段、旧执行表、Incident状态及事件、父run/work/handed_off尝试和checkpoint均保持。只更新35个历史测试文件的当前head断言，旧迁移未改。

## 测试方法与结果

验收日期2026-10-25。本轮按JUnit用例标识去重共2,177项相关Python测试通过；单元分批共1,661项、原生30、候选API16、旧闭环API3及历史迁移467项，采用报告均零失败、零错误、零跳过。历史467项与当前收集结果逐项一致。以下仅记录本轮实际执行；中间失败、被停止的旧进程和重复运行不累计为通过数。JUnit保存在忽略的.pytest_cache/slice120-*.xml，未提交。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元基线 | 1,652项通过，67.58秒，含本片97项契约/纯策略/HTTP检查 |
| 查询异常边界补验 | 9项通过，11.67秒；来源损坏、404保留与真实坏行Pydantic校验 |
| 新PostgreSQL原生 | 30项通过，113.33秒；命令权限/正文/严格类型、迁移元数据/空往返、来源与内容篡改、预算占用、闭环、不可变、降级和旧授权锁；原生合法写入可由应用重建 |
| 候选检查API | 15项通过，823.56秒；双来源、角色/正文、版本/摘要、过期、幂等/分页、写后认证/期限回滚及同键并发单次观测 |
| 已存事实损坏API | 1项通过，79.43秒；分别篡改检查内容与原候选摘要，GET/commands/replay均409，原事实不降级、无新观测，恢复后可正常读取和重放；合法读取失败仍持久化阻断，参数全空且不回退冻结值 |
| 旧闭环API | 3项通过，46.57秒；数据库配置、Redis恢复验证和原可信审批幂等审计 |
| 历史迁移回归 | 467项通过，1,838.54秒；包含旧候选/规划/请求/决定与其他迁移兼容，35个旧文件仅更新head断言 |
| 静态检查 | Ruff通过，789文件格式一致；Mypy453源文件通过，唯一head为0076_candidate_preflight；文档链接、完整Git差异及diff --check通过 |

```powershell
.venv/Scripts/pytest.exe tests/unit -o addopts='' -q
.venv/Scripts/pytest.exe tests/unit/execution/test_candidate_preflight_queries.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_preflight_migration.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_preflight_api.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration -k 'migration and not candidate_preflight_migration' -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -o addopts='' -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

开发子任务按领域与观测、原生守卫、API验证分配写入文件；主Agent整合可信事务与路由，独立核实报告和最终差异。独立审查发现来源异常可能返回500及原生夹具的身份/时间规范偏差，已修复并补查询归一、真实篡改API及原生写入后应用读取。预算攻击只在隔离测试中追加reserved/unknown审计，原成功调用用量保持，不把破坏历史伪装成超额场景。

## 限制与下一步

本片没有实际候选执行、生产参数证据、新前端或父运行收尾。假模型与ASGI/假HTTP传输、临时PostgreSQL只证明本地契约和事务行为，未升级用户Compose库、未发布或连接生产系统。release当前没有版本状态或版本CAS协议，不能用开关或镜像身份宣称完成版本回滚；配置/Redis参数匹配也不能证明自由文本条件成立。GET历史报告不等于当前可用，30秒期限不延长原批准。output/未读取、未提交。

下一片121先建立版本化执行条件与可信核验事实：逐项绑定原条件及未解问题、严格说明哪些已核实及其来源，不能以119阅读确认替代；明确核验失效和新批准关系后，继续设计候选专用白名单执行与CAS。发布版本状态、执行租约与结果、恢复验证、父运行结算/终端取消、活动槽位释放、前端与预算追加仍需后续实际交付。

## 面试表达

“批准之后我没有直接把候选ID送进旧执行器，而是用独立检查事实绑定批准和当前事故版本。系统实际读取本地演练参数，并把版本无法验证、参数漂移和未核实条件保留下来；读过条件不等于条件成立。检查与可信审计原子保存，历史重放不重复取数，来源被篡改时整体拒绝。真正执行还必须重新核验并通过CAS，预检快照本身不是执行授权。”
