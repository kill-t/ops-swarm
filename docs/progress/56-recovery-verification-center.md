# 56 - 恢复验证中心与采样进度展示

## 目标

让工程师在受控执行成功后，从浏览器显式创建恢复验证并观察 Worker 的持久采样进度，同时继续保证执行成功不等于系统恢复。

## 设计与边界

- 增加默认 50、最大 100 的只读待验证投影。
- 候选和验证创建都只接受 Incident 最新调查中当前批准计划的成功操作。
- 浏览器创建请求只携带 `expected_incident_version`，不能提供验证规则或探针输入。
- 页面每 3 秒读取运行与调度状态，不调用手动样本 POST。
- 终态候选离开队列后仍保留最后读取结果，明确展示 Incident 进入 `resolved` 或返回 `investigating`。
- 本步不增加数据库迁移、不改变 Worker 采样算法，也不开放 Incident 关闭。

## 实际改动

- 验证持久化层新增严格候选模型、有界关联查询和服务层读取方法。
- 执行记录 ORM 转换提取为共享函数，避免验证投影复制执行生命周期重建逻辑。
- 验证创建新增“最新调查 + 当前批准计划”校验，阻止旧成功操作被新周期复用。
- FastAPI 新增 `GET /api/v1/verification-candidates`，并扩展 OpenAPI 契约。
- Vue 新增验证类型、API 客户端、Pinia 状态、候选列表、验证详情和 `/verifications/:executionId?` 路由。
- 页面展示冻结规则、固定参数、执行器、样本进度、调度到期时间、Worker 状态、有限错误和终态。
- 导航增加“恢复验证”，并补充窄屏布局。

## 测试方法与结果

执行后端静态检查：

```powershell
uv run ruff check src/opsswarm/verification src/opsswarm/execution/persistence src/opsswarm/control_plane/schemas.py src/opsswarm/control_plane/routes.py
uv run mypy src/opsswarm/verification src/opsswarm/execution/persistence src/opsswarm/control_plane/schemas.py src/opsswarm/control_plane/routes.py
```

Ruff 通过，mypy 检查 19 个源码文件无问题。

执行验证候选、完整控制面流程和 OpenAPI 定向测试：

```powershell
uv run pytest -q tests/integration/test_verification_store.py tests/api/test_control_plane_api.py::test_completed_investigation_exposes_normalized_evidence_board tests/api/test_control_plane_openapi.py
```

首次运行时 Docker Desktop 的 Linux Engine 已停止，11 个需要 Testcontainers PostgreSQL 的用例在 fixture 初始化阶段产生环境错误，不依赖数据库的 OpenAPI 用例通过；这不是业务断言失败。启动 Docker Desktop 后以相同命令重跑，`12 passed`。覆盖候选创建前后投影、终态移出队列、limit 边界、旧调查操作拒绝和完整 API 链路。

执行受影响的前端检查：

```powershell
cd web
npm run typecheck
npm test -- --run src/services/api.test.ts src/stores/verification-store.test.ts src/components/VerificationCandidateDetail.test.ts
npm run build
```

类型检查通过，`3` 个测试文件共 `19 passed`，Vite 生产构建成功。覆盖精确创建请求、已有运行幂等读取、终态结果保留、只读 API、确认防误触，以及页面不存在观测值和阈值输入。

`git diff --check` 通过，仅提示现有 Windows 工作区的 LF/CRLF 自动转换，没有空白错误。本切片不运行与恢复验证无关的完整后端回归，完整回归保留到跨模块发布验收。

## 限制与下一步

- 页面没有认证或 RBAC，只能用于本地故障演练。
- 3 秒轮询用于 MVP 可观测性，后续可以把样本进度纳入可重放事件流。
- 历史终态由 Incident 作战室保存；验证列表不是历史报表。
- 下一步增加 `resolved` Incident 的人工关闭入口，完成页面上的事故终态操作。

## 面试表达

> 我没有把执行接口返回 200 当作恢复，而是增加独立恢复验证中心。候选只来自最新调查的当前批准计划，创建时后端再次校验，浏览器不能提交阈值、PromQL 或观测值。常驻 Worker 用持久任务自动采样，页面只读观察进度，并在候选消失后保留最终通过或失败结果。
