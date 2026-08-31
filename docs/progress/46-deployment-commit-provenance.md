# 第 46 步：部署版本与 GitHub Commit 确定性关联

## 本步目标

让 OpsSwarm 区分“GitHub 最近有代码提交”和“该 Commit 已经部署到订单服务”，通过固定仓库与完整 SHA 建立可验证的软件来源关系。

## 设计决策

- 在既有不可变部署详情中增加嵌套 `code_reference`，不创建重复部署表。
- 历史记录允许缺省并标记 `not_declared`，不伪造 Commit 回填。
- 内容摘要忽略空可选字段，保证升级前的历史摘要完全兼容。
- PostgreSQL CHECK 和 Pydantic 同时约束仓库、完整 SHA 及部署类型。
- 组合器只按规范化仓库与完整 SHA 精确匹配，不使用标题、时间接近或模型判断。
- 未在当前时间窗观察到提交时标记 `commit_not_observed`，不错误宣称提交不存在。
- 不增加 Agent 工具、GitHub 权限、模型调用或数据库删除能力。

## 实现内容

1. 新增 `DeploymentCodeReference`，并把它作为部署事实的可选不可变字段。
2. 保持老部署规范 JSON 与 SHA-256 摘要不变，带引用的新记录参与幂等内容摘要。
3. Alembic `0024` 和 SQLAlchemy 元数据增加部署代码引用 CHECK。
4. 变更调查 Evidence 暴露仓库、Commit SHA 和显式关联状态。
5. GitHub 组合来源增加纯确定性匹配，并保存匹配的 GitHub Evidence ID。
6. 增加模型、API、真实 PostgreSQL、迁移、适配器与歧义失败测试。

## 测试与验收

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

实际结果：

- Ruff 格式检查确认 336 个文件无需调整，静态检查通过。
- mypy 检查 141 个 Python 源文件通过。
- 后端 500 项测试全部通过。
- 前端 8 个测试文件共 20 项测试通过，类型检查与生产构建通过。
- Docker Compose 配置检查通过。
- 业务 PostgreSQL 成功升级到 Alembic `0024_deployment_code_reference`。
- Compose 变更服务成功写入一条携带代码来源的真实演练部署记录；统一变更源读取 5 条证据，其中 1 条部署记录与 GitHub Commit Evidence 精确匹配。
- 现场调查 Worker 以 `simulator_and_github` 模式重新启动，GitHub 令牌已配置且持久任务队列无进行中任务。
- 真实冒烟只读取本地变更服务与 GitHub，没有调用模型、写入 GitHub 或产生模型费用。

## 为什么这样设计

部署记录已经包含服务、版本、发生时间、幂等编号和不可变摘要，另建映射表会产生两份可能漂移的部署事实。把代码引用嵌入部署详情，可以让同一原子事实回答“哪个服务、哪个版本、哪个仓库、哪个 Commit”。关联仍在读取组合层完成，因此原始 GitHub Evidence 和持久部署记录都不会被改写。

## 限制与下一步

- 当前代码引用由本地管理接口写入，尚未自动接收 CI/CD 部署事件。
- 当前不验证容器镜像 digest、构建产物或软件物料清单。
- 下一步根据作战室价值选择展示部署来源，或实现有签名/白名单的部署事件摄取。

## 面试表达

我没有根据 Commit 时间猜测部署，而是要求部署事实携带固定仓库和完整 SHA，再与 GitHub Evidence 精确匹配。历史摘要保持兼容，非法引用在 Pydantic 和 PostgreSQL 两层被拒绝，匹配状态由确定性代码生成而不是模型自报。这补上了事故调查中的软件来源链路，同时没有扩大 Agent 权限。
