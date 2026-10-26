# 127 - 候选发布可信任务与独立Worker

## 目标

在main的d79ddcb上，把124精确准备请求、125回执和126远端窗口接成完整控制面执行链路：可信显式入队、持久任务/尝试、独立Worker实际派发、仅GET恢复及不可变结果。开始前核对AGENTS、项目状态/进度、124～126相关设计、Git和实际代码；初始只有用户output/未跟踪目录，未读取、未提交。

## 设计与边界

事实来源为[专项设计](../architecture/127-candidate-release-worker.md)。API只创建任务，Worker执行固定原CAS；新执行命令不改原计划、核验、审批和124 prepared内容，原false字段保留其历史语义。只允许本地order-service rc1→stable，既有父run/work/handed_off尝试、Incident/事件、原预算、图及活动槽位均保持。

委派绑定当前executor凭据版本而非永久复用authorization。写资格与对账资格分别校验：写出需要当前权限、精确批准/最高核验、原输入/预算/期限及固定CAS；已派发后审批或凭据失效仍允许有效新租约GET记录历史副作用，旧租约迟到不能结算。结果只是模拟执行事实，尚未客观证明恢复健康。

## 实际改动

- 新增execution/release_jobs，按契约、授权/凭据指纹、纯策略、来源重建、查询、API、入队事务、租约事务、有界客户端、Worker和CLI分工。POST .../release-execution-request/execution-job仅可信executor+execute_action，完整正文绑定run/job/规范version三资源；GET任务包含最多3个尝试及结果，GET /commands查询业务审计。首次201、同主体同键同完整命令200；一个124请求唯一任务和原operation，创建UUID由请求稳定派生。
- 0082_candidate_release_job保存job/attempt/command/result四表。固定窗口not_before=job.created_at、expires_at=min(原准备期限,原凭据期限)，不晚于原运行期限，最多30秒；60秒尝试租约覆盖窗口。入队和成功审计、终态/尝试/结果分别同事务闭环；原文、窗口、身份、结果及命令不可改写，有数据禁止降级。35个历史测试文件仅同步head，未改已发布迁移。
- 首次POST前单独提交永久dispatched标记及首次尝试/时刻；POST注册后用新会话/事务再核完整来源、当前凭据与租约，才允许唯一一次PUT。重领dispatched任务连注册POST也不重发，只GET原operation。最多3次尝试，第3次失租后扫描保存无观察unknown/blocked终态，避免永久running。
- 远端HTTP固定本地白名单、单次请求、禁重试/跳转/代理/压缩、总超时及64KiB；拒绝重复JSON字段、默认身份补齐、数字布尔、错operation/CAS/hash/window/状态/回执和不严格时间。每份DispatchObservation保存查询路径/采集时间并在写入和历史读取重验。unknown或冲突不自动换键重发。
- 结果保存精确请求/operation/window摘要、完整有界材料与reason：applied_current/applied_superseded、明确远端cancelled/expired的not_applied、无法确定的unknown、未派发失效的blocked。正常结算及首次派发延迟约束再核数据库实际租约/窗口时钟；保存点兼容。已应用不推进父运行，不冒充恢复验证。
- 凭据指纹对环境配置摘要再次散列，持久数据没有令牌或原secret摘要。API使用进程配置，Worker每次写前调用provider加载当前环境；不宣称跨进程即时撤销。README提供独立CLI --once和持续模式，不提前添加默认Compose服务。
- 独立审查补齐合法同键异体/换键冲突的业务错误审计：健康原事实核验后保存409，原键和换键都不能把历史损坏降级成普通拒绝。

## 测试方法与结果

验收日期2026-10-26。采用JUnit均零失败、零错误、零跳过；按classname/name去重共2,259项相关测试通过，重复专项不累计。

| 范围 | 实测结果 |
| --- | --- |
| 全量单元 | 2,212项，75.57秒；新增客户端80与凭据/窗口/结果纯策略20 |
| 原生PostgreSQL | 13项，97.04秒；元数据/空迁移往返、可信原正文/准入、孤立成功拒绝、不可变/带数据禁降级，以及真实124来源的跨租约提交整体回滚 |
| 新控制面API | 14项，657.83秒；双来源、并发幂等、身份/三资源/原期限、双Worker、真实126正常CAS/响应丢失/ABA、POST后撤凭据/替代核验/预算耗尽、已应用崩溃恢复/迟到拒绝、三次耗尽、历史损坏及flush后回滚 |
| 其余崩溃窗口 | 3项，172.89秒；marker前失租零网络blocked、marker后POST前崩溃仅GET unknown、POST后PUT前崩溃仅GET过期not_applied；均校验父链及旧租约拒绝 |
| 旧链路回归 | 17项，217.92秒；通用迁移3、126远端9、124双来源2、旧配置/Redis/可信审批API3 |
| 主Agent独立真实接入 | 2项，102.99秒，与API重复；真实CAS及实际成功后丢失响应，均只读原操作收敛 |
| 错误审计补验 | 3项，206.41秒，与API重复；双来源合法409落审计，损坏原键/换键不新增错误事实 |
| 静态 | Ruff通过、873文件格式一致、Mypy510源文件通过，746个修改文档本地链接及差异检查通过；唯一head为0082_candidate_release_job，CLI --help通过 |

```powershell
.venv/Scripts/pytest.exe tests/unit -q
.venv/Scripts/pytest.exe tests/integration/test_release_execution_migration.py -q
.venv/Scripts/pytest.exe tests/api/test_release_execution_jobs_api.py -k 'not remaining_crash_boundaries' -q
.venv/Scripts/pytest.exe tests/api/test_release_execution_jobs_api.py -k remaining_crash_boundaries -q
.venv/Scripts/pytest.exe tests/api/test_release_execution_jobs_api.py -k 'two_sources or corrupt_job' -q
.venv/Scripts/pytest.exe tests/api/test_release_execution_jobs_api.py::test_actual_126_asgi_cas_and_lost_response_only_query_original_operation -q
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_lab_release_dispatch_api.py tests/api/test_candidate_release_request_api.py::test_two_sources_bind_actual_lab_get_without_dispatch_or_parent_mutation tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.execution.release_jobs.main --help
git diff --check
```

真实126测试使用当前UTC基线并在完成旧调查/审批夹具后再开始30秒准备窗口，保留原运行期限；旧冻结时钟相对数据库过旧导致的早期失败未计验收。并行独立补验曾有一次进入unknown，未保留该次reason_code，不能确定具体触发因素；增强失败诊断后，顺序复跑两个真实接入均通过，未放宽期限或请求超时。该现象作为负载下时限波动的限制保留，不能据通过数量承诺执行可用率或把unknown视为成功。

报告保存在忽略的.pytest_cache/slice127-*.xml，未提交。开发子任务负责严格客户端、API边界测试、迁移及原生测试；主Agent实现服务/租约/Worker、整合并独立审查关键规则，补齐首次派发原生current/延迟租约校验和错误审计，再核报告及真实接入统一提交。只用独立Testcontainers PostgreSQL17.6、假模型、受控时钟与实际ASGI应用，未升级用户Compose库、调用真实模型或连接生产。

## 限制与下一步

unknown终态没有自动轮询或人工重开命令；本片没有控制面任务取消API，不能把停止写出或126管理取消端口称为已完成业务取消。历史材料完整性失败仍整笔拒绝；执行事实不能绕过当前审批成为后续权限。失租后的新尝试只读原操作，父流程和活动槽位保持。

下一片128为候选执行后的客观恢复验证：精确绑定持久结果与操作、独立采样窗口/任务/尝试，明确未恢复、来源失败及结果未知分支；不得把target_version或effect_current当作健康证明。父运行成功/失败收尾、验证失败回流、unknown人工收敛与取消、前端及图恢复继续按独立边界推进。

## 面试表达

“我把显式执行命令变成持久任务，Worker每次写前重验真实授权和原预算。首次网络写之前提交不可回退的派发标记，所以崩溃后即使原操作不存在，也只读查询而不重新写出。远端窗口和CAS负责拒绝迟到或漂移，控制面租约负责拒绝旧尝试结算。原审批过期后仍能保存实际副作用的只读材料，但执行结果不会直接宣告事故恢复。”
