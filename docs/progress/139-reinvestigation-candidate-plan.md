# 139 - 回流新候选的可信规划准入

## 目标与改动

基线6af0a90。可信工程师显式将138成功候选编译为独立单步骤draft，精确绑定138完整结果、137路由和事故版本，保留135开放假设、未解问题、前置条件与观测窗口。设计见[专项设计](../architecture/139-reinvestigation-candidate-plan.md)。

复用纯草案构造及action-risk-v3，不映射旧根因或假设ID。0094新增计划和命令两表；成功、重放及业务拒绝逐请求绑定可信身份、原始正文和规范命令。计划与applied审计同事务，原生守卫重建真实调用者/发布尝试、重算正文，并在提交时复核依据、预算、版本与服务端凭据期限。旧图、父运行和旧批准不变，无新模型调用或预算预留。

## 验证

2026-10-30已验收，随本记录独立提交。新旧规划、预算与身份相关单元，隔离PG元数据与空库0093往返、原始命令严格校验、可信身份及提交凭据过期回滚均通过。单条假模型回流链验证权限、完整来源/事故版本冲突、写后告警整笔回滚、并发单胜与幂等审计、SQL/Python正文对照、不可变及自洽摘要篡改拒绝、旧授权隔离、原账本不变、历史stale与精确重放。

Ruff、格式、Mypy、唯一head、文档链接及差异检查通过；预算包装补齐底层完整性错误向本模块错误的转换。复现入口：

```powershell
.venv/Scripts/pytest.exe tests/unit/planning/test_reinvestigation_candidate_plan.py tests/unit/planning/test_candidate_plan.py tests/unit/planning/test_candidate_plan_budget.py tests/unit/identity -q
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_candidate_plan_migration.py tests/api/test_reinvestigation_candidate_plan_api.py -q
```

详细报告留在忽略的`.pytest_cache/slice139-*`。按风险验证，未跑全量、前端或真实模型，未升级用户Compose；`output/`未读取、修改或提交。

## 下一步与面试表达

下一片核对新草案的可信审批请求准入，继续绑定本次完整新依据；本片approval_eligible与execution_eligible保持false，未创建审批或动作。

“把模型候选与可信处置规划分开：工程师选择精确结果，确定性代码重建安全草案；数据库再核验来源、命令、版本和原预算。历史重放保留事实，新审批必须另行建立有效性绑定。”
