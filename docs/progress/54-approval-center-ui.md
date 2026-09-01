# 54 - 本地审批中心与待办队列

## 目标

把已经存在的审批领域能力提供给本地值班工程师：能够发现所有待审批计划、复核完整结构化动作和恢复标准，并通过精确 Incident 版本批准或拒绝。批准不能直接操作故障环境。

## 设计与边界

- 增加默认 50、最大 100 的只读待办队列，按最早过期排序。
- 队列复用现有计划加载和内容哈希校验，不建立新的审批表或缓存事实。
- 返回 Incident 当前版本和完整计划，使页面能够展示与决定实际绑定的内容。
- 页面只提交 `approve` 或 `reject`、`expected_incident_version`、`actor_id` 和原因。
- 决定继续由原有 PostgreSQL 事务完成；409 后刷新队列，不在前端猜测或强制覆盖。
- `actor_id` 没有认证能力，页面明确标记仅限本地演练。

## 实际改动

- 审批持久化层新增严格待办投影及有界查询。
- FastAPI 新增 `GET /api/v1/approval-requests`，返回待办总数、精确审批绑定、Incident 快照和完整计划。
- Vue 新增审批类型、API 服务、Pinia 状态、待办列表、计划详情和 `/approvals/:requestId?` 路由。
- 计划详情展示风险、内容摘要、精确动作参数和恢复判据；填写原因并显式确认后才能决定。
- 批准成功提示“尚未执行任何环境操作”，拒绝提示 Incident 返回调查。
- 增加队列、批准/拒绝请求、乐观锁冲突刷新和页面防误触测试。

## 测试方法与结果

执行审批后端的定向回归：

```powershell
.venv\Scripts\ruff.exe check src/opsswarm/approval src/opsswarm/control_plane tests/integration/test_approval_request_store.py tests/api/test_control_plane_api.py
.venv\Scripts\mypy.exe src/opsswarm/approval src/opsswarm/control_plane
.venv\Scripts\pytest.exe tests/integration/test_approval_request_store.py tests/api/test_control_plane_api.py
```

结果：Ruff 和 mypy 通过，审批持久化与控制面共 `40 passed`。覆盖待办精确绑定、决定后移出队列、无界 limit 拒绝，以及完整 API 闭环中的队列内容与批准后清空。

执行受影响的前端检查：

```powershell
cd web
npm run typecheck
npm test -- --maxWorkers=1 src/services/api.test.ts src/stores/approval-store.test.ts src/components/ApprovalRequestDetail.test.ts
npm run build
```

结果：TypeScript 类型检查通过，`3` 个测试文件共 `13 passed`，Vite 生产构建成功。测试覆盖 GET 队列、精确决定请求体、批准不等于执行的提示、必填复核确认，以及 409 后刷新队列。

本切片没有运行 581 项完整后端回归。原因是改动边界集中在审批读取投影、原有决定 API 的前端调用和新页面，已执行对应持久化/API 测试、静态检查与前端构建；完整回归保留到跨模块发布验收阶段。

## 限制与下一步

- 页面还没有认证、RBAC 和生产权限模型，只能用于本地故障演练。
- 审批中心不能执行动作；下一步应增加“已批准处置”视图，再通过现有幂等白名单执行 API 显式触发单个步骤。
- 执行入口必须展示幂等键、当前 Incident 版本、步骤状态和执行后验证边界，不能复用审批按钮偷偷执行。
- 之后再补恢复验证进度和关闭动作，形成完整浏览器处置闭环。

## 面试表达

> 我给审批领域补了一个有界待办投影和独立审批中心。队列返回精确计划版本、内容哈希、Incident 乐观锁和完整白名单参数；批准或拒绝仍由后端事务校验，冲突就刷新。最关键的是批准不会调用执行器，页面也明确提示这一点，所以人工授权、确定性执行和恢复验证是三段独立安全边界。
