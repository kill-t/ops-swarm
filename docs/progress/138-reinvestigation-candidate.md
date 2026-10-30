# 138 - 回流候选评估意图的受限消费

## 目标与改动

基线ff74710，仅消费137的candidate_evaluation。新增独立任务、最多三次租约尝试及不可变结果，精确绑定137路由、135/136完整新依据，保留人工约束、反证、未解问题与请求/实际窗口。复用候选模型、输出契约和参数模板，通过小型纯评估协议兼容旧来源。设计见[专项设计](../architecture/138-reinvestigation-candidate.md)。

0093增加三表、原账本专属owner及原生来源/输入/结果/提交守卫；所有调用与unknown继续累计原预算，成功调用恢复复用前仍核验来源。补齐共享调用分类、可信材料读取和父收尾隔离。显式Worker与只读API已接入，不加入默认Compose；父运行、旧图及旧批准不变，不创建计划或动作。

## 验证

2026-10-30已验收，随本记录独立提交。受影响候选/预算/审查路由单元与身份权限回归、隔离PG元数据及空库0092往返、SQL/Python评估对照、代表性API链均通过。API覆盖并发单胜、迟到COMMIT拒绝、unknown保留、发布中断后成功调用复用、来源变化、可信读取与历史保留，旧计划/审批/操作及父运行不变。Ruff、格式、Mypy、唯一head、CLI帮助与差异检查通过。

首次回归补齐新操作分类，首轮API补齐上下文响应union；期限断言改为应用和PG均到期后检查应用错误类型，未放宽守卫。最终同名API复验通过，早期失败报告保留。复现入口：

```powershell
.venv/Scripts/pytest.exe tests/unit/investigation/test_reinvestigation_candidate.py tests/unit/investigation/test_call_budget_policy.py tests/unit/identity -q
.venv/Scripts/pytest.exe tests/integration/test_reinvestigation_candidate_migration.py tests/api/test_reinvestigation_candidate_api.py -q
```

详细报告留在忽略的`.pytest_cache/slice138-*`。按风险定向验证，未跑全量、前端或真实模型；`output/`未读取、修改或提交。

## 下一步与面试表达

下一片核对新候选规划准入，必须绑定本次结果和新依据，旧批准不覆盖；follow_up分派、预算追加与人工收敛仍待后续。

“把候选评估接在精确的新证据链后，通过原账本追加受限调用；租约与提交守卫隔离迟到结果，恢复复用已成功调用。模板可行只进入后续规划审查，不等于根因证明或执行授权。”
