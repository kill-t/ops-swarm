# 现场动态调查至终端交接

2026-11-05。将现场取证接入动态调查，完成任务分派、补查、审查与可恢复交接。独立的 graph-v16 固定精确窗口与取证来源，复用任务快照、共享预算、上下文及首轮经验冻结。0097 在 PostgreSQL 复核任务、证据和成功调用的绑定。见[设计](../architecture/live-dynamic-investigation.md)。

定向单元、启动 API、真实 PostgreSQL 迁移及原生约束、两轮动态调查和中断恢复用例通过。场景从日志调查补查指标，完成独立审查并释放 Worker。恢复复用已提交的模型调用和证据，累计预算与首轮经验选择保持可追溯。Ruff、mypy 和差异检查通过。验证使用脚本模型与模拟 MCP 客户端，Compose 现场演示留在发布验收。报告位于 `.pytest_cache/live-dynamic-*`。

```powershell
.venv/Scripts/pytest.exe tests/integration/test_live_dynamic_migration.py tests/integration/test_live_dynamic_workflow.py -q
.venv/Scripts/pytest.exe tests/api/test_control_plane_api.py -k 'test_start_v3_investigation_returns_dynamic_contract or test_start_v3_rejects_incomplete_routes_before_writes' -q
```

下一项从现场终端交接接入人工补充、重评和单步骤处置。本片的面试表达是，专业任务只能使用持久计划授权的窗口，checkpoint 恢复先核对业务工件，因此可以在同一预算下续接补查和审查。
