# 135 - 独立回流综合与专业来源闭包

## 目标

基线main的f8bb319。核对AGENTS、项目状态、64/65、最新134设计/进度、Git及真实专业/账本代码，消费134成功结果，在原预算内生成等待独立审查的开放假设。初始仅用户output/未跟踪目录，本片不读取、改动或提交。设计来源为[专项设计](../architecture/135-reinvestigation-synthesis.md)。本片已验收，随本记录创建独立提交；具体提交以Git核对。

## 设计与边界

原133队列是不可变分派历史，134投影才表示执行结果，不能看pending字段就判定尚未执行，也不能跳过失败专业。135按task_key排序消费完整同根成功集合，SQL从原业务表核验分派、独立尝试、取证/模型调用、实际窗口、快照、packet和结果，Python再校验契约、来源与输入。必要人工约束、反证、失败查询、未解问题和未观测区间进入模型；源数据是材料，不能覆盖系统规则。历史证据只作背景，不纳入当前支持/反证引用集合。

独立任务使用原run身份和原账本，父运行仍settled并持槽、原work/attempt与旧图保持；不新建run、轮次、额度或授权。所有专业成功才可准备；未完成等待，明确失败/阻断则零模型调用地保存blocked。新证据全空、必要输入超容量、预算不足、原截止或来源改变均停止。OpenHypothesisProposal保留开放描述、支持/反证和未解问题，结果固定pending_review，不证明客观根因或授予执行资格。

## 实际改动

- 新增reinvestigation_synthesis，契约、输入/SDK、纯预算/引用规则、来源、任务/账本、历史查询、Worker和API/CLI按职责分离。沿用已有独立阶段模式，未增加调度框架或业务工具。
- 0090_reflow_synthesis新增jobs、attempts、results三表。任务按原run唯一，最多3次真实尝试，默认120秒租约/最多300秒；SQL先聚合过滤等待集合，缺失work视为未完成；避免最早八条等待记录遮挡后续就绪任务。父work串行保护，SKIP LOCKED与有界扫描防止互相阻塞。网络在事务外执行。
- 原账本新增synthesize_reinvestigation和reflow_synthesis_attempt_id，稳定键synthesis:{attempt_id}；原parent_attempt仅为历史来源，旧归属字段为空。通用预留/结算/父收尾拒绝新owner，旧交接只增加窄归属JOIN，历史函数保持，新当前预算函数涵盖全部调用且工具按稳定任务计数。
- 完整输入与allocation/manifest一同冻结；SQL使用固定提示及schema独立重建，与Python编译结果对照。原次数、Token、费用和总时长跨全部角色/尝试累计，未知不补零。已确认成功调用可跨尝试复用；unknown重试可能产生新调用，不承诺恰好一次。
- 原生守卫保护当前依据、真实租约/提交时间、严格提案与新证据闭包、不可变材料/终态及有数据禁降级。结果、当前成功尝试和任务终态同事务、同时间保存，支持SAVEPOINT。当前失效阻止新发布，历史读取继续重建已保存材料。
- 新增可信GET reinvestigation-synthesis及/input和显式--once/--continuous Worker；调用材料接口要求Bearer/read，HTTP不执行模型，不加入默认Compose。35个历史集成文件仅更新最新head断言。

## 测试方法与结果

验收日期2026-10-28。主验收去重共2,604项不同用例有通过记录：全量单元2,592、API5、迁移1及旧链路6。定向21项和扫描过滤补验属于重叠用例，不重复计数；扫描过滤补验2项通过，371.468秒。

| 范围 | 实际结果 |
| --- | --- |
| 全量单元 | 2,592项通过，64.398秒；新增21项覆盖完整材料/未观测区间、原摘要/专业结果伪造、缺失集合、越界引用、原策略/截止、unknown占用、仅自身在途预留、超分配用量和真实LangChain SDK固定HTTP的已知/未知usage |
| 独立综合与原生闭包 | 主验收5项API及1项迁移全部通过，合计1,078.882秒；双来源均完成log/metric/change三专业综合，SQL/Python完整packet及累计预算一致，旧父/专业/历史调用前缀保持；可信材料读取、伪造摘要及结构/引用拒绝、不可变及有数据禁降级、metadata和空升降级均通过 |
| 恢复与失败路径 | 上述API包含真实30秒租约越期提交回滚、接管保留unknown、模型成功后结果保存崩溃并跨新尝试复用（共三次尝试）；新告警保留实际usage为unknown而不发布；明确专业失败blocked且零模型调用 |
| 扫描过滤补验 | 2项通过，371.468秒；等待、完整成功和失败集合的SQL筛选均通过，未把有界扫描本身当作就绪资格 |
| 旧链路回归 | 6项通过，51.130秒；通用迁移3项、MVP配置/Redis两类处置及可信审批幂等/主体审计 |
| 静态与导航 | Ruff、1,005文件格式、Mypy601源文件、唯一head0090_reflow_synthesis及CLI帮助通过；文档本地链接和差异已核对，暂存完整差异与差异检查在提交前完成 |

使用隔离Testcontainers PostgreSQL17.6-alpine、假模型与固定HTTP；未连接生产、真实供应商或升级用户Compose数据库。完整三专业夹具在132成功后、133分派前实际等待至少65秒以满足旧MCP整分钟协议，不改冻结事实或原900秒期限。租约到期测试以PostgreSQL clock_timestamp判断，UPDATE先成功、COMMIT越期整笔回滚；不以宿主时钟推断数据库截止。当前预算追加未实现，正向容量在根创建时固定，不运行中扩容。测试耗时包含完整前置链，不作为生产吞吐或更大负载性能验收。

```powershell
.venv/Scripts/pytest.exe tests/unit -q --junitxml=.pytest_cache/slice135-unit.xml
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_synthesis_api.py::test_success_call_reuse_after_result_crash tests/api/test_reinvestigation_synthesis_api.py::test_new_alert_retains_usage_without_hypothesis tests/api/test_reinvestigation_synthesis_api.py::test_failed_professional_blocks_without_model tests/api/test_reinvestigation_synthesis_api.py::test_complete_source_synthesis_and_native_integrity tests/integration/test_reinvestigation_synthesis_migration.py -q --junitxml=.pytest_cache/slice135-db-final.xml
.venv/Scripts/pytest.exe "tests/api/test_reinvestigation_synthesis_api.py::test_complete_source_synthesis_and_native_integrity[candidate_with_change-terminal_handoff]" tests/api/test_reinvestigation_synthesis_api.py::test_failed_professional_blocks_without_model -q --junitxml=.pytest_cache/slice135-filter-final.xml
.venv/Scripts/pytest.exe tests/integration/test_migrations.py tests/api/test_control_plane_api.py::test_database_configuration_plan_runs_through_controlled_verification tests/api/test_control_plane_api.py::test_redis_dependency_plan_runs_through_controlled_verification tests/api/test_operator_identity_api.py::test_authenticated_approval_preserves_idempotency_and_subject_audit -q --junitxml=.pytest_cache/slice135-regression.xml
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
.venv/Scripts/python.exe -m opsswarm.investigation.reinvestigation_synthesis.main --help
git diff --check
```

最终报告在忽略的.pytest_cache/下：slice135-unit.xml、slice135-db-final.xml、slice135-filter-final.xml及slice135-regression.xml；早期开发失败记录不列为通过。无前端改动，未重跑前端构建；报告、缓存和用户output/均不提交。

## 限制与下一步

独立审查、审查后路由和新规划/审批/处置尚未接通。MCP v1内收窗口限制保持，必要上下文可能超过原预算并被阻断；不自动追加额度，不为完成阶段延长原期限。第136步消费精确综合提案及同源完整材料开展独立审查，不把pending_review当作已审查。

## 面试表达

“我让回流综合消费完整专业结果闭包，先核验真实任务尝试、新证据、完整输入与成功调用，再在原账本预留。人工约束和未观测区间不会在摘要中丢失，模型只能引用本次新证据。专业失败时明确阻断，未知调用保留占用；成功调用可跨尝试复用，数据库用真实提交时钟隔离迟到结果。当前输出是待独立审查的开放假设，没有沿用旧审批或生成执行资格。”
