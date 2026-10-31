# 140 - 回流新草案的可信审批请求准入

## 目标与改动

基线2f114cb。可信工程师精确选择139草案并确认已阅读未决条件，生成独立pending审批请求；不沿用旧批准，不生成审批决定或动作。设计见[专项设计](../architecture/140-reinvestigation-approval-request.md)。

0095新增请求和命令审计两表，请求、Incident的相邻版本迁移、事件及applied审计同事务绑定。新审批阶段单独判断有效性，保留139历史而不套用其current；自己的迁移不使请求失效，真正新告警/版本变化仍阻断。原生守卫重建来源和事件前缀、拒绝孤立迁移及篡改，提交时复核原预算、总截止、请求和本次凭据期限；无新调用或预算预留。

## 验证

2026-10-31已验收，随本记录独立提交。新契约/期限/阶段及相关旧审批和身份单元通过。隔离PG元数据与空库0094往返、严格原始命令、可信身份和提交凭据过期回滚通过；单条假模型回流链覆盖并发幂等、版本/摘要冲突、孤立迁移拒绝、写后告警回滚、阶段不自我失效、事件/SSE游标、原账本和旧授权隔离、原生历史篡改拒绝、stale/expired只读及精确重放。

Ruff、格式、Mypy、唯一head、文档链接及差异检查通过。首次PG前置缺少运行中的Docker，启动后完成正式验收；凭据守卫已正确拒绝过期提交，测试错误文案对齐后定向复验通过，未放宽业务守卫。复现入口：

```powershell
.venv/Scripts/pytest.exe tests/unit/approval/test_reinvestigation_approval.py tests/unit/approval/test_candidate_approval.py tests/unit/identity -q
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_approval_migration.py tests/api/test_reinvestigation_approval_api.py -q
```

详细报告留在忽略的`.pytest_cache/slice140-*`。未跑全量、前端或真实模型，未升级用户Compose；`output/`未读取、修改或提交。

## 下一步与面试表达

下一片核对本请求的可信批准、拒绝与过期收敛，仍须绑定完整新依据；批准不能跳过未决执行条件。

“审批请求是新的持久阶段：先校验精确草案和原预算，再原子迁移事故与落下事件、请求、审计。旧阶段变stale不等于新请求失效，只有本请求自身的精确新版本、原期限和证据变化决定后续资格。”
