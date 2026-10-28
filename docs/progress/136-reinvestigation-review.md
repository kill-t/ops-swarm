# 136 - 独立回流审查与精确提案绑定

## 目标

基线main的e2b912a。核对AGENTS、项目状态、64/65、135设计/验收、Git及实际综合/审查/预算代码，在原预算内对精确回流综合开展独立审查。本片已验收，随本记录创建独立提交，具体提交以Git核对。初始仅用户output/未跟踪目录，本片不读取、修改或提交该目录。事实来源为[专项设计](../architecture/136-reinvestigation-review.md)。

## 设计与边界

135原始pending_review正文不可改，不能把综合输出当作独立审查；136通过独立任务、真实尝试、角色版本、提示与新调用产生recorded意见。输入包含完整原材料、专业结果/新证据、实际窗口和未观测范围，绑定135原输入/结果摘要。SQL从真实135任务、输入、模型调用、成功结果及尝试重建闭包，Python再严格校验和编译上下文。135模型调用可以属于更早尝试，不能要求caller就是结果的当前成功尝试。

沿用HypothesisReviewProposal有限契约和既有领域引用规则：接受支持必须是原提案的支持引用，反证可来自同轮新证据；重复/重叠引用和无依据方向拒绝。evaluate_for_routing、follow_up和human_review均只是建议，不创建路由、任务、计划或授权，不证明客观根因。人工约束、反证、失败查询和未解问题保留在完整输入，超预算停止，不裁掉必要材料。

## 实际改动

- 新增reinvestigation_review，按契约、上下文、SDK、引用/预算规则、来源、任务/账本、查询、Worker及API/CLI分责。复用LangChain结构化输出及已验收领域审查规则，没有新编排框架或业务工具。
- 0091_reflow_review新增jobs、attempts、results三表，任务外键直接绑定135任务；原run唯一，最多3次真实尝试，默认120秒租约/最多300秒。先SQL过滤135成功项再作至多八条父work锁竞争，SKIP LOCKED避免等待项遮挡；锁序父work→136job→135job→132job→candidate→Incident，网络不在事务中。
- 原账本增加review_reinvestigation、reflow_review_attempt_id和reflow-review:{attempt_id}；其他owner为空，原parent_attempt只作历史来源。通用预留/结算/父收尾排除新owner，旧交接增加窄JOIN而不递归新来源；旧131～135历史SQL函数保持，新当前预算函数涵盖全部历史与当前调用。
- 固定reinvestigation-review-v1策略/提示、reinvestigation-reviewer-v1角色；完整ModelInput、allocation和manifest原子冻结。SQL独立编译提示/schema/输入/清单，与Python重建对照。原次数/Token/费用和总截止跨全部角色、重试、unknown累计；成功调用跨新尝试复用，不承诺外部请求恰好一次。
- 当前事故/告警/持槽父状态、预算和真实截止在准备至提交各阶段重验；失效结果保留unknown审计及真实已知用量，迟到不发布。结果、当前成功尝试和终态同事务/同时间保存，原生真实时钟门禁、严格审查语义/引用、不可变和有数据禁降级保护。
- 新增可信GET reinvestigation-review及/input，新调用三类材料接口也要求Bearer/read。CLI显式--once/--continuous，不进入默认Compose。原父settled/持槽、旧图、135 pending_review正文及所有旧预算不改；35个历史迁移测试仅更新最新head断言。

## 测试方法与结果

验收日期2026-10-28。按classname及测试名取最后结果去重，共2,624项不同用例通过：全量单元2,613、API4、迁移1及旧链路6。API三项主验收通过，人工来源负向断言修正后单独复验1项通过，451.331秒；该项替代同名早期失败，不重复计数。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 2,613项通过，76.659秒；新增21项覆盖精确输入/结果/摘要绑定、缺失与伪造来源、三种有限建议、原支持/同轮证据范围、unknown占用、仅本阶段owner在途预留、原期限以及真实LangChain SDK固定HTTP已知/未知usage |
| 迁移一致性 | 1项通过，32.857秒；直接绑定135任务的外键与ORM一致，command.check及空库升降级通过 |
| API恢复与告警 | 真实30秒租约UPDATE后COMMIT越期回滚、失租拒绝、接管保留unknown，第二次尝试成功模型后结果落库崩溃、第三次尝试复用调用；新告警保留实际usage为unknown但不发布意见，两项通过 |
| API双来源与原生 | 双来源完整三专业均通过：候选直接来源evaluate_for_routing与人工来源follow_up意见分别记录；人工来源同时验证135成功调用跨旧owner复用。覆盖并发准备单胜、135成功调用旧owner复用、完整输入/预算SQL-Python对照、可信三材料查询、同轮未被原提案支持引用的证据拒绝、不可变和有数据禁降级 |
| 旧链路回归 | 6项通过，57.774秒；通用迁移3项、MVP两类处置及可信审批幂等/主体审计 |
| 静态与导航 | Ruff、1,023文件格式、Mypy615源文件、唯一head0091_reflow_review、CLI帮助通过；文档本地链接及35个历史head-only差异已核对，完整暂存差异与差异检查在提交前完成 |

API主报告中前三项分别205.118、112.830、259.384秒通过，人工来源最后复验451.331秒通过；较早报告中同名人工负向断言错误不列为通过。该错误没有暴露业务代码接受非法方向，而是测试对follow_up的False再次赋False，修正为真正翻转；最终同轮非原支持引用、越界反证、不一致方向、额外字段、支持/反证重叠均由原生守卫拒绝。人工补验还在特权模拟中同时改写提案、结果摘要、完整packet及相关摘要后验证原始来源重建拒绝，不仅依赖摘要自洽。

仅隔离Testcontainers PostgreSQL17.6-alpine、假模型和固定HTTP，未连接生产、真实供应商或升级用户Compose数据库。完整三专业前置链沿用真实至少65秒等待满足MCP整分钟窗口，不改冻结事实或原900秒期限。租约越期以PostgreSQL clock_timestamp确认，不用宿主时钟推断；测试包含完整旧前置链，不作为生产性能结论。必要输入容量从根创建时固定，未运行中追加预算。

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice136-unit.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_review_api.py::test_expired_commit_takeover_and_success_reuse tests/api/test_reinvestigation_review_api.py::test_new_alert_keeps_usage_but_rejects_review tests/api/test_reinvestigation_review_api.py::test_two_sources_exact_review_and_native_protection -q --junitxml=.pytest_cache/slice136-api.xml
.venv/Scripts/pytest.exe "tests/api/test_reinvestigation_review_api.py::test_two_sources_exact_review_and_native_protection[human_clean_with_change-human_route]" -q --junitxml=.pytest_cache/slice136-human-final.xml
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_review_migration.py -q --junitxml=.pytest_cache/slice136-migration.xml
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice136-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_review.main --help
git diff --check
```

报告保存在忽略的.pytest_cache/下：slice136-unit.xml、slice136-api.xml、slice136-human-final.xml、slice136-migration.xml及slice136-regression.xml；按同一测试最后结果统计，早期负向断言错误不作为通过记录。无前端变更，未重跑前端构建；缓存、报告及用户output/不提交。

## 限制与下一步

仅记录独立意见，原135状态仍是历史pending_review；实际136状态读新接口。不自动消费补查/人工建议，不重开旧图或沿用旧批准。原预算可能不足、总期限可能到期，必要上下文不能省略以强行完成；没有追加预算。MCP内收窗口、held/unknown人工处理、终端取消、规划和多步骤/前端保持后续范围。

第137步对精确135/136结果与同源证据进行确定性路由，保留人工约束、原轮次/任务/累计预算和停止条件，再连接新的规划审批；不能把审查建议直接变成执行资格。

## 面试表达

“综合与审查是独立阶段。审查绑定精确提案、完整专业材料和两个原始摘要，只能接受该提案已引用的支持，其他新证据可作为反证。任务先在原账本预留，再调用独立提示和严格结构输出；未知占用不清零，成功调用跨新尝试复用。数据库在真实租约和截止约束下原子保存recorded意见，旧综合正文仍保持。建议没有自动路由、审批或执行资格，后续由确定性代码消费。”
