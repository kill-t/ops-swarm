# 129 - 候选恢复结论与父运行持久收尾

## 目标

在main的5a439da上消费128恢复结论，为父调查运行提供精确、可审计且不破坏历史读取的持久收尾。继续前核对AGENTS、PROJECT_STATE、进度索引、128设计/验收、64/65相关规则、Git以及实际状态约束。初始仅用户output/未跟踪目录，未读取或提交。

## 设计与边界

事实来源为[专项设计](../architecture/129-candidate-parent-settlement.md)。父run.status=settled只表示候选阶段收尾，独立记录保存原始五类恢复outcome和三类disposition。三份健康样本的历史证明不能等同长期健康或根因因果；来源失败和执行未知不能伪造成旧succeeded/failed结果。

收尾事实、可重放run.candidate_settled事件和父终态同事务保存，不产生网络/模型调用，不另建无业务必要的任务/尝试或图。原work完成时刻、handed_off父尝试、111交接、审批/计划、127/128正文和parent_advanced=false、模型/工具账本、图均保持；父run仅改变status/completed_at/updated_at，result/metrics/last_error保持NULL。Incident仍mitigating，活动索引保留settled，不提前释放槽位或关闭事故。

## 实际改动

- 新增investigation/settlement，按契约、纯后续意图策略、SQL资格选择、窄父状态校验、收尾仓储、只读API和独立CLI分工。稳定身份直接复用run UUID，应用与原生校验一致，不引入UUID扩展。
- 0084_candidate_settlement保存唯一run/recovery任务、原执行/恢复摘要、operation、handoff、旧父状态/时刻、事故版本、原outcome/reason、disposition、事件游标及完整摘要。独立事实和事件不可改；原生约束拒绝孤立事实/父终态/事件、异源或重算摘要的假结论、跨事务补齐和有数据降级。
- 资格在SQL选取前过滤并在父锁后复核：最高核验、精确人工版本、原mitigating事故版本、无在途调用/候选评估/执行/恢复。最多扫描八份瞬时竞争记录，但永久失格记录不占扫描前沿，避免后续合格记录饥饿。完整来源损坏不转成未知结论。
- 历史111交接通过窄校验识别有精确收尾事实的新父状态；129 GET再完整重建128/127和原候选链，避免129→128→116→111循环。历史可读不代表当前资格恢复。人工等待仍保留历史waiting投影，旧幂等输入可重放，新补充/取消不能重开settled运行。
- 新增可信只读GET /api/v1/investigations/{run_id}/candidate-settlement，未收尾返回空，不触发推进；legacy_local也要求Bearer/read。普通run、证据板及进度事件契约认识settled，前端仅同步已有状态联合类型，不增加页面或控制入口。
- 独立进程python -m opsswarm.investigation.settlement.main支持--once和持续扫描；单事务回滚后可重扫，已提交后不重复收尾，不自动启动新Compose服务。

## 测试方法与结果

验收日期2026-10-27。按JUnit classname/name去重，当前实现共2,321项验收用例取得通过记录。失败记录保留，不把环境或早期夹具错误计成通过；下列汇总以最终全量单元、最终API/扫描、原生/旧链路及修正夹具的专项报告为准。

| 范围 | 实测结果 |
| --- | --- |
| 全量单元 | 2,295项通过，41.606秒；新增25项覆盖三类意图、错误原因拒绝和执行/恢复冲突 |
| 最终API与扫描 | 9项通过，679.686秒；双来源并发单胜/幂等、完整历史GET、人工重放/新命令拒绝、五类结果、鉴权/摘要损坏/flush回滚，以及八条失格不阻塞第九条 |
| 原生与旧链路批次 | 17项中16通过、1项早期夹具失败，364.376秒；包含新迁移11项与旧链路6项。metadata/空迁移往返、孤立状态/事件/事实、原子闭环、摘要/结论/ID/版本及材料拒绝、不可变/禁降级、真实索引槽位保留和MVP配置/Redis/可信审批回归 |
| 修正夹具专项 | 1项通过，55.708秒；新增另一份queued候选，保持原来源完整，以精确错误断言验证原生在途候选守卫，补齐上述失败用例，不重复累计 |
| 静态与构建 | Ruff通过、905文件格式一致、Mypy530源文件通过；唯一head为0084_candidate_settlement，CLI --help通过，vue-tsc与Vite构建通过；文档链接和Git差异检查通过 |

早期在途候选夹具直接把原已成功候选改为queued，使上游草案历史校验先拒绝，未命中目标收尾守卫；完整批次在该夹具修正前已加载测试代码，因此保留1项错误断言记录。改为独立新增queued调度对抗行后，目标原生资格守卫的精确错误专项通过，不放宽生产规则或断言。开发中此前的双源smoke2项及旧原生8项是最终修改前记录，不用于最终通过总数。报告位于忽略的.pytest_cache/slice129-*.xml，未提交。

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice129-unit.xml
.venv/Scripts/pytest.exe tests/api/test_candidate_settlement_api.py tests/integration/test_candidate_settlement_scan.py -q --junitxml=.pytest_cache/slice129-api-final.xml
.venv/Scripts/pytest.exe tests/integration/test_candidate_settlement_migration.py tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice129-native-final.xml
.venv/Scripts/pytest.exe tests/integration/test_candidate_settlement_migration.py -k in_flight_candidate -q --junitxml=.pytest_cache/slice129-inflight.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.investigation.settlement.main --help
npm --prefix web run build
git diff --check
```

只使用独立Testcontainers PostgreSQL17.6、假模型与固定HTTP传输；未升级用户Compose数据库、调用真实模型或连接生产。跨日继续时Docker未运行，启动后重新执行未进入测试主体的六项设置错误，不把环境错误记成通过。

扫描回归中的八条失格材料是隔离测试中注入的最小选取器对抗夹具，不伪造完整批准/证据来源，也不作为合法业务收尾；真实第九条完整来源必须被选中并正常收尾。在途候选守卫测试使用另一条queued夹具保留原候选历史；早期直接修改原候选使上游历史先拒绝的夹具已替换，使用精确错误断言验证目标守卫。

## 限制与下一步

本片保存后续意图，尚未实际启动重新调查或人工解决。收尾不刷新128样本，不承诺收尾时仍持续健康。原运行收尾后仍占事故活动槽位，Incident维持mitigating。新进程/查询不代表已完成生产部署或图内interrupt/resume。

下一片130推进候选处置结果与Incident的受控收敛，明确恢复证据时效、非恢复/未知分支及活动槽位边界；自动失败回流、unknown人工解决/取消、多步骤、前端与图恢复继续分片交付。

## 面试表达

“我把父运行结束与事故恢复分开：独立收尾事实精确引用执行和采样结果，用三类后续意图区分验证恢复、需要重新调查和需要人工解决。收尾事实、进度事件及父终态在一个事务提交，旧交接和样本不改。历史验证通过窄状态校验兼容新终态，完整查询仍重建原来源；这样既能审计恢复，也不会把旧批准重新当成当前权限。活动槽位保留到明确的事故收敛阶段。”
