# 55 - 已批准计划的受控执行中心

## 目标

让工程师能够从浏览器发现已批准计划，并显式执行数据库中的确定性白名单步骤。执行请求不能携带动作参数或任意命令，批准与执行必须保持分离。

## 设计与边界

- 增加默认 50、最大 100 的只读待执行投影。
- 只选择 Incident 最新调查中的最高版本批准计划，阻断旧调查步骤重新执行。
- 复用原有计划策略、审批内容哈希、Incident 乐观锁、租约和幂等事务。
- 浏览器幂等键由审批请求 ID 与步骤 ID 稳定派生，不提供自由输入框。
- 页面显著提示执行会修改本地故障演练环境，并要求再次勾选确认。
- 执行成功只进入 `verifying`，本步不创建验证运行、不采样，也不关闭 Incident。

## 实际改动

- 执行持久化层新增严格候选投影、有界查询和 `running` 记录聚合。
- 执行授权增加“必须属于 Incident 最新调查”的后端校验。
- FastAPI 新增 `GET /api/v1/execution-candidates`，返回精确审批、Incident、完整计划和活动执行记录。
- Vue 新增执行类型、API 服务、Pinia 状态、候选列表、执行详情和 `/executions/:planId?` 路由。
- 页面显示批准原因、内容哈希、精确动作参数、恢复判据、固定幂等键、租约和有限执行结果。
- 成功、409 冲突和适配器失败后都会重新读取服务端候选，不保留可覆盖新状态的旧页面数据。

## 测试方法与结果

执行候选投影、授权和 OpenAPI 定向测试：

```powershell
.venv\Scripts\pytest.exe tests/integration/test_operation_execution_store.py tests/api/test_control_plane_openapi.py
```

结果为 `13 passed`。覆盖精确批准投影、成功后移出队列、运行记录展示、limit 边界，以及旧调查批准计划被后端拒绝。

执行控制面完整业务流的定向测试：

```powershell
.venv\Scripts\pytest.exe tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py
```

结果为 `29 passed`。完整链路验证批准后进入候选、候选包含精确步骤、执行成功后队列清空，并保留原幂等执行和恢复验证契约。

提交前把上述两组测试合并执行，最终结果为 `41 passed`。第一次合并复跑时 Docker Desktop 的 Linux Engine 未运行，Testcontainers 在创建 PostgreSQL 前统一产生 40 个环境 setup error，只有不依赖数据库的 OpenAPI 测试进入并通过；这不是业务断言失败。启动本机 Docker Desktop、确认 Engine 就绪后使用相同命令重新执行，41 项全部通过。

执行后端静态检查：

```powershell
.venv\Scripts\ruff.exe check src/opsswarm/approval/persistence src/opsswarm/execution src/opsswarm/control_plane
.venv\Scripts\mypy.exe src/opsswarm/approval/persistence src/opsswarm/execution src/opsswarm/control_plane
```

Ruff 通过，mypy 检查 20 个源码文件无问题。

执行受影响的前端检查：

```powershell
cd web
npm run typecheck
npm test -- --maxWorkers=1 src/services/api.test.ts src/stores/execution-store.test.ts src/components/ExecutionCandidateDetail.test.ts
npm run build
```

结果：类型检查通过，`3` 个测试文件共 `17 passed`，Vite 生产构建成功。覆盖稳定幂等键、精确版本请求、冲突刷新、运行中阻断、防误触确认和有限结果展示。

本切片没有运行 581 项完整后端回归。当前验证集中在执行持久化、控制面全业务流、OpenAPI、前端执行交互和生产构建；完整回归继续保留到跨模块发布验收阶段。

## 限制与下一步

- 页面没有认证、RBAC 或生产权限，只能用于本地故障演练。
- 当前运行中状态依赖手动刷新，没有主动轮询或 SSE。
- 当前计划实际为单步骤；多步骤事务、补偿和部分成功策略尚未设计。
- 下一步增加恢复验证中心：发现已成功但尚未验证的执行，显式创建验证运行，并只读展示 Worker 采样进度和最终恢复判定。

## 面试表达

> 我没有让批准按钮顺便执行，而是补了独立的受控执行中心。浏览器只发送数据库步骤 ID、稳定幂等键和 Incident 版本，后端还会检查该计划属于最新调查，避免旧批准被复用。执行器拿到的是经过完整性和权限验证的结构化步骤，不是 LLM 命令；成功后只进入 verifying，后面还必须由确定性探针证明恢复。
