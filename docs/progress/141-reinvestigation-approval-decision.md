# 141 - 回流审批请求的可信决定与过期收敛

2026-11-05，基线50d6c4b。本轮实现收尾并独立提交，单元与迁移验收通过；完整API链尚未验收，保留欠项，不标记整片全部通过。

## 改动与边界

可信审批者绑定140请求与139完整草案，记录approve/reject/expire。0096新增决定和命令审计两表；决定、Incident相邻版本迁移、事件及applied审计同事务闭合。批准进入mitigating，拒绝和显式到期回investigating；原预算、开放假设、全部未决条件与窗口保留，不重开旧图、不提供执行资格。详细规则见[专项设计](../architecture/141-reinvestigation-approval-decision.md)。

修正拒绝阶段检查和临界合法草案的正文空间；原生守卫同时拒绝非法目标跳转及合法目标缺少决定的孤立迁移。审批者权限沿旧119语义，不额外增加主体互斥。

## 实际验证

- 通过：新契约、状态、时限、内容保全、可信准入及关联旧决定/身份单元；真实PG元数据、0095空库往返、严格原始命令和凭据过期提交回滚；Ruff、格式及Mypy。
- API部分断言通过：真实拒绝分支、未到期expire拒绝、非法目标与孤立迁移回滚。整条测试失败，不能计为API验收通过：一次批准在告警注入前提前拒绝，原因尚未定位；随后诊断在131历史授权守卫中止。已有WSL时钟倒退实测，但不能认定所有失败均由时钟引起。
- 按本轮精简要求，暂缓剩余长链、自然等待完整TTL、全量/前端/真实模型验证与共享环境修复。保留失败诊断，不将未发生的故障注入当作守卫成功。expire正常到期边界由纯规则覆盖，真实负面事务以reject代表。

可复现入口（API须在可用、时钟可靠的隔离PG环境执行，本轮不再重跑）：

```powershell
.venv/Scripts/pytest.exe tests/unit/approval/test_reinvestigation_decision.py tests/unit/approval/test_candidate_decision.py tests/unit/identity -q
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_decision_migration.py -q
.venv/Scripts/pytest.exe tests/api/test_reinvestigation_decision_api.py -q
```

报告在忽略目录`.pytest_cache/slice141-*`，最新诊断为`slice141-api-linux-diagnostic.log`。仅使用本项目临时测试容器，未启动整套Compose；Linux测试依赖按uv.lock固定。未修改业务TTL、租约、运行中预算或共享内核参数。本轮收尾时Docker不可用，未重新启动。`output/`、`tmp/`未读取、修改或提交。

## 后续与面试表达

完整API欠项在推进实际执行前关闭；后续目标为142新批准的受限执行前检查，不自动恢复长链复测或共享调参。

“人工决定与执行资格分开：完整新草案进入不可变决定，事故迁移、事件和审计同事务。批准使用自己的阶段校验，负面决定允许收敛，未决条件仍需后续核验。”当前对外表达须附带API整链未验收的限制。
