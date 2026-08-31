# 第 45 步：GitHub 只读变更调查数据源

## 本步目标

为现场调查增加真实 GitHub Commit 与已合并 Pull Request 证据，同时保留本地变更模拟器的可重复故障事实，不让模型获得仓库写权限或动态 HTTP 能力。

## 设计决策

- 默认 `simulator` 不访问 GitHub，只有显式选择 `simulator_and_github` 才启用补充来源。
- 固定一个 `owner/repo`、两个 GET 路径、时间窗和每类 20 条上限。
- 私有仓库令牌只需要 `Contents: read` 和 `Pull requests: read`，仅从环境读取。
- 分页不完整、限流、权限、重定向、响应大小、外部契约或证据 ID 冲突都失败关闭。
- GitHub 只补充上下文；本地模拟器仍提供确定性发布、配置和依赖变更事实。
- 外部文本明确视为不可信数据，单/多 Agent 提示版本升级到 v3，历史结果不改写。
- 不新增表、不修改 Alembic，不引入新的 Agent 工具或模型调用。

## 实现内容

1. 新增 GitHub 设置、Commit/PR 外部契约、只读 REST 适配器和组合变更来源。
2. 对 URL、分页、排序、时间、重复项、响应大小和常见 HTTP 失败建立稳定错误语义。
3. 把 Commit 和已合并 PR 规范化为有限 `EvidenceObservation`，清理标题控制字符。
4. 为现场 Worker 增加显式变更来源选择，并把设置传入 Compose 现场工作池。
5. 新增无模型 GitHub 冒烟入口，默认现场冒烟也改为按 `.env` 组装来源。
6. 在单 Agent、专业节点和综合节点加入外部文本不可信策略，并升级提示版本。
7. 增加固定请求、最小头、公开模式、失败状态、分页完整性、URL 白名单和组合冲突测试。

## 测试与验收

本切片的自动化测试不读取 `.env` 中的真实令牌，不访问 GitHub，也不调用模型。GitHub HTTP 行为由 `httpx2.MockTransport` 固定重放；真实私有仓库冒烟留到用户配置最小权限令牌后执行。

完整验收命令：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
docker compose config --quiet
cd web
npm run typecheck
npm test -- --run
npm run build
```

实际结果：333 个文件通过格式检查，Ruff 通过，mypy 严格检查 141 个源码文件通过，494 个后端测试全部通过。前端类型检查通过，8 个测试文件中的 20 个测试通过，Vite 生产构建成功；Markdown 相对链接、代码围栏、Compose 解析和 `git diff --check` 通过。不调用模型的统一现场冒烟成功调用三类工具，健康现场下日志与变更如实为空，Prometheus 返回一条规范化指标证据。

重新构建 `ops-swarm-runtime:local` 后，冻结与现场调查 Worker 均成功重新创建并进入常驻轮询；订单服务、Redis 故障代理、变更服务及依赖容器保持健康。运行态确认变更来源仍为默认 `simulator`，业务库没有 `pending` 或 `running` 调查任务，因此验收没有访问 GitHub、调用真实模型或产生模型费用。

## 为什么这样设计

把 GitHub 作为模拟器的可选补充，能让求职项目展示真实 SaaS 工具集成，又不会牺牲故障演练的确定性。Agent 不需要知道 GitHub API，更不需要拥有通用网络或写权限；它只消费经过本地契约验证的 Evidence。失败不降级为“空结果”，保证根因判断不会建立在缺失却未披露的数据上。

## 限制与下一步

- 私有仓库真实冒烟尚待 `.env` 中的细粒度只读令牌。
- 当前没有读取 diff、Checks、Deployments 或 Actions，也没有 webhook 增量同步。
- 下一步先完成私有仓库无模型冒烟和现场 Worker 组合验证，再决定是否需要持久化同步游标。

## 面试表达

我把 GitHub 集成限制为固定仓库、固定 GET 请求和最小权限令牌，并对分页、限流、重定向、外部契约和提示注入分别建立边界。GitHub 只补充本地确定性变更事实，失败时任务可审计地重试，不会静默回退或伪造空证据。提示改变后版本从 v2 升到 v3，保证真实评测仍可复现。
