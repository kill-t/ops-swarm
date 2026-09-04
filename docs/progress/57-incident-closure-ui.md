# 57 - 已恢复 Incident 的人工关闭入口

## 目标

在 Incident 作战室为 `resolved` 事故提供明确的人工关闭入口，完成浏览器从恢复确认到 `closed` 终态的生命周期操作，同时保留乐观锁、人工审计和复发保护。

## 设计与边界

- 复用现有 `POST /api/v1/incidents/{id}/actions/close`，不新增状态机、数据表或迁移。
- 关闭表单只收集本地审计操作者和原因；Incident 版本由 Pinia 当前快照提供。
- 页面不发送目标状态、强制开关或环境操作参数。
- 只有 `resolved` 显示可操作表单，`closed` 只显示终态，其他状态不渲染入口。
- 关闭冲突后重新读取最新 Incident，但不自动重试；复发后的 `investigating` 必须保留。
- 当前没有身份认证，`actor_id` 不作为可信授权。

## 实际改动

- 前端 Incident 类型新增关闭审计输入和完整 API 请求模型。
- API 客户端新增编码 Incident ID 的关闭请求。
- Incident Pinia store 新增关闭加载、错误、成功状态和冲突后最新快照恢复。
- 新增 `IncidentClosurePanel`，展示恢复与关闭边界、二次确认和终态复发语义。
- 作战室详情接入关闭面板；成功响应同步更新详情和侧栏，并继续接受 SSE 幂等事件。
- 增加组件、API 客户端和状态仓库测试以及响应式样式。

## 测试方法与结果

执行现有后端关闭契约和 OpenAPI 定向测试：

```powershell
uv run pytest -q tests/api/test_control_plane_api.py::test_engineer_closes_resolved_incident_and_recurrence_creates_new_incident tests/api/test_control_plane_api.py::test_close_rejects_unresolved_incident_and_stale_version tests/api/test_control_plane_openapi.py
```

结果为 `3 passed`。覆盖成功关闭、提前关闭、过期版本、关闭后同指纹告警新建 Incident 和接口契约。

执行受影响的前端检查：

```powershell
cd web
npm run typecheck
npm test -- --run src/services/api.test.ts src/stores/incident-store.test.ts src/components/IncidentClosurePanel.test.ts
npm run build
```

类型检查通过，`3` 个测试文件共 `24 passed`，Vite 生产构建成功。覆盖请求字段、ID 编码、二次确认、非 resolved 隐藏、closed 只读终态、当前版本提交和冲突后复发状态刷新。

`git diff --check` 通过，仅提示 Windows 工作区的 LF/CRLF 自动转换，没有空白错误。本切片没有运行与 Incident 关闭无关的完整后端回归。

## 限制与下一步

- 操作者 ID 仍是本地审计声明，不具备认证强度。
- 关闭原因尚未结构化为影响范围、修复项和负责人。
- 作战室已走通审批、执行、验证和关闭，但事故报告仍只是分散在业务表和时间线中的事实。
- 下一步建议生成持久、可追溯的事故复盘报告，并只允许从 `closed` Incident 的已提交事实构建。

## 面试表达

> 我没有把验证通过自动等同于关闭，而是在作战室增加独立人工动作。组件只收集审计信息，store 用当前 Incident 版本调用后端固定 close 状态机。若提交前故障复发，409 后页面读取最新 investigating 状态并停止关闭，不会自动替用户覆盖并发业务事实。
