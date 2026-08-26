# 19 - Incident 作战室与实时事件展示

## 目标

把已经完成的 Incident 查询和可重放 SSE 变成值班工程师可直接观察的第一个前端页面。这个切片只解决列表、详情、时间线、路由和实时连接，不把审批或执行能力提前放进浏览器。

## 设计与边界

- 前端是独立 `web/` 工程，使用 Vue 3、TypeScript、Vite、Pinia 和 Vue Router。
- FastAPI 与 PostgreSQL 仍是唯一事实来源，前端状态可以随时从 API 重建。
- Vite 把同源 `/api` 转发到本机控制面，不修改当前 localhost 后端的 CORS 边界。
- 原生 EventSource 负责 `Last-Event-ID` 和重连；Pinia 使用游标去重、版本号阻止历史快照倒退。
- 首版保持只读，没有调查、审批、执行、验证或关闭命令，也没有前端容器。

## 实际改动

1. 新增可锁定依赖、类型检查、测试和生产构建的 Vue 工程。
2. 新增 REST 客户端、具名 SSE 客户端和 Incident 运行时事件校验。
3. 新增 Pinia store，处理列表/详情加载、快速切换竞态、迟到列表响应、流连接状态、游标去重和版本保护。
4. 新增 Incident 列表、关键事实、连接状态和时间线组件，以及可直达的可选 ID 路由。
5. 新增响应式深色作战室样式、加载/空状态/错误状态和基础可访问性语义。
6. 在 `.gitignore` 与 `.dockerignore` 排除 Node 依赖、构建和覆盖率产物，避免污染 Git 与 Docker 构建上下文。
7. 新增前端架构事实来源并同步 README、SSE 和生命周期文档。

## 测试方法与结果

前端静态检查、单元测试和生产构建：

```powershell
cd web
npm ci
npm run typecheck
npm test
npm run build
```

结果：从 `package-lock.json` 干净安装 207 个包后，类型检查通过；4 个测试文件共 7 个测试通过；Vite 成功构建 36 个模块，生产 JavaScript 约 101.34 kB，gzip 后约 39.17 kB。

真实浏览器连接本地控制面与 PostgreSQL：

- 无 ID 路由自动选择 `INC-000001` 并把 Incident ID 写入 URL。
- 页面初始读取 2 条已持久事件；提交同指纹新告警后，无刷新实时出现第 3 条 `incident.alert_attached`。
- 连接中断时显示“连接中断，自动重连中”；开发服务恢复并重新加载直达路由后，从数据库重建为 3 条事件，没有重复展示。
- 桌面布局和 390×844 窄屏布局均可用，没有横向内容溢出。

后端与基础设施回归：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
docker compose config --quiet
uv run alembic current

cd web
npm audit
```

结果：Ruff 检查通过，179 个文件格式正确；mypy 检查 87 个源文件无错误；后端 201 个测试全部通过。Compose 配置有效，固定项目容器中的 PostgreSQL 与订单服务均为 healthy；业务库位于 `0010_verification_runs (head)`；npm 报告 0 个已知漏洞。

## 限制与下一步

- 当前一次只取最近 100 个 Incident，没有接入后端分页游标。
- SSE 开发链路依赖 Vite 代理；生产静态资源容器和反向代理还未设计。
- Vite 开发代理在后端连接被强制重置时会记录 `ECONNRESET`，但不保证立即把该重置传播给浏览器；正式反向代理必须增加独立的自动重连端到端测试。
- `@vue/test-utils` 的间接依赖当前仍包含已弃用的 `glob@10.5.0`；`npm audit` 为 0，暂不强制覆盖上游依赖版本，后续随测试工具升级移除告警。
- 前端没有认证与操作按钮，不能声称已具备生产作战室权限控制。
- 下一切片增加只读作战室聚合查询，串起 Incident、调查运行、Evidence Board 和处置链路，继续避免把多个前端请求拼接成不一致快照。

## 面试表达

> 我先做了只读事故作战室，使用 REST 获取快照、SSE 接收 PostgreSQL 已提交事件。因为 SSE 是至少一次投递，我按全局 cursor 去重；因为重连会补完整历史，我再按 Incident version 防止旧事件覆盖新快照。Vue Router 保存当前事故，Pinia 的 generation 解决快速切换请求竞态。开发代理保持同源，没有为了前端方便放宽后端安全边界。
