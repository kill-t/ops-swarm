# 20 - Incident 作战室处置链路聚合展示

## 目标

把已经持久化的调查、证据、计划、审批、执行和验证连接到 Incident 作战室，让值班工程师不离开当前事故页面就能判断处置进展。本切片只增加跨领域读取和展示，不增加浏览器写命令。

## 设计与边界

- 后端使用一个只读 `WorkroomStore` 和一条 PostgreSQL 查询构建当前链路，避免前端多请求产生时间不一致。
- 投影只选择最近调查、当前计划、当前计划的审批、最近执行及其验证，历史事实不删除但不在本摘要展开。
- Evidence Board 仅返回计数和主假设摘要，不复制原始证据与日志。
- 前端为投影维护独立加载、错误和重试状态，并用请求 generation 与 Incident version 拒绝迟到快照。
- 新的非过期状态 SSE 事件触发重新读取；重复事件、历史事件和无关事件不增加请求。
- 页面继续保持只读，所有操作仍必须经过领域服务、乐观锁、人工审批和白名单执行器。

## 实际改动

1. 新增 `opsswarm.workroom` 稳定投影模型和只读仓储。
2. 新增 `GET /api/v1/incidents/{incident_id}/workroom` 及 OpenAPI 响应模型。
3. API 集成测试覆盖新 Incident 空链路和完整演练链路摘要。
4. 前端新增作战室投影类型、API 客户端和 Pinia 并发保护。
5. 新增六阶段响应链组件，展示调查、Evidence Board、计划、审批、执行和验证。
6. 增加桌面三列、平板两列、移动端单列样式，以及聚合加载、错误和手工同步状态。
7. 更新系统架构、API、前端设计和 README 事实来源。

## 测试方法与结果

后端完整回归：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

结果：Ruff 检查通过，186 个文件格式正确；mypy 检查 90 个源文件无错误；201 个后端测试全部通过。API 集成测试使用 Testcontainers 的真实 PostgreSQL，覆盖全空链路和已经进入验证阶段的完整链路。

前端干净安装与回归：

```powershell
cd web
npm ci
npm run typecheck
npm test
npm run build
npm audit
```

结果：从锁文件安装 207 个包；类型检查通过；5 个测试文件共 9 个测试通过；Vite 构建 38 个模块，JavaScript 约 108.63 kB，gzip 后约 40.97 kB；依赖审计为 0 个已知漏洞。首次把 Vitest 与后端全量 Testcontainers 测试并行运行时，Windows 子进程启动超时，没有执行测试断言；改为串行单独运行后全部通过，因此本机最终验收不再让两组重型测试争用资源。

基础设施与真实浏览器：

```powershell
docker compose config --quiet
uv run alembic current
docker compose ps
```

结果：Compose 配置有效，业务库迁移位于 `0010_verification_runs (head)`，固定命名的 PostgreSQL 与订单服务容器均为 healthy。真实浏览器通过 Vite 连接 FastAPI 和业务库，读取到 6 张阶段卡片；控制台无 warning 或 error。390×844 视口下阶段卡片为单列，页面没有横向溢出，测试完成后已恢复默认视口并关闭临时页面。

## 限制与下一步

- 当前只展示最新处置链路，不是完整审计历史页。
- Evidence Board 和计划步骤仍是摘要，没有可展开明细。
- 运行中但不迁移 Incident 的数据变化不会自动产生刷新，当前可以手工同步。
- 前端没有认证与写按钮，不能用于生产操作。
- 下一切片建立评测集，使用同一批故障输入比较单 Agent 与多 Agent 的准确率、证据召回、工具调用和安全违规。

## 面试表达

> 我为作战室设计了一个跨领域只读投影，用单条 PostgreSQL 查询保证 Incident 最新调查到恢复验证来自同一语句快照。投影只返回安全摘要，不复制业务状态。前端用双 generation 和 Incident version 处理快速切换、迟到请求与 SSE 重放，并只在新的有效状态事件到来时刷新完整链路。
