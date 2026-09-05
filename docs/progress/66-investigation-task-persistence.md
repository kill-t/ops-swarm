# 66 - 调查轮次、专业任务与尝试持久化

## 目标

为完整多 Agent 工程版建立可查询、可重试的专业任务事实层。业务任务保持稳定身份，每次处理尝试独立记录，后续固定图可以据此恢复和展示中间过程，不再把全部进度只保存在 checkpoint 中。

## 设计与边界

- 先更新 [数据库设计](../architecture/04-database-design.md)、[API 设计](../architecture/05-api-design.md) 和 [专项设计](../architecture/66-investigation-task-persistence.md)，再实现迁移、内部写入用例与只读查询。
- 新增轮次、专业任务、专业任务尝试三类事实；既有调查运行与 Worker 尝试继续保留。最多三轮、每轮三个专业任务、每任务三次尝试，分派目标、角色、服务、时间窗和上限不可修改。
- 写入先锁定父 `work_items` 并核对有效租约、运行与事故状态。开始和结算分别使用短事务，事务中没有模型或外部 HTTP 调用；提交前再次检查租约。
- 技术重试保留任务 ID，增加尝试号和任务版本。接管时将旧尝试标记为 `lease_expired`，旧令牌和旧尝试结果不能推进新状态。开始请求返回 `created=false` 时调用方不能重复执行。
- 角色范围限日志、指标和变更，本片只允许事故所属服务；后续跨服务场景再引入明确服务范围。结算保存有限处理摘要或稳定错误码，不把任务成功当作已证明根因。
- 当前 v1 图与 Worker 没有接入此事实层，不会自动产生新任务记录。阶段报告、证据与 SSE 事件的业务事务接入留到下一切片；此片不调用模型、真实工具或改变审批与执行能力。

## 实际改动

- 新增迁移 `0030_investigation_task_facts`，创建 `investigation_rounds`、`agent_tasks`、`agent_task_attempts`。为现有 Worker 尝试增加 `(id, investigation_run_id)` 唯一约束，支持复合外键阻止跨运行关联。
- `investigation/tasks/` 按职责拆分为领域契约、确定性策略、读取记录、ORM 表、写入事务、查询投影、错误和 HTTP 路由，应用工厂只做依赖装配，未扩展既有大路由或调查仓储的职责。
- 支持整轮原子创建、分派内容幂等及冲突检测、带版本的并发开始、失败重试、不可覆盖的尝试结算、租约接管和轮次终态收敛。
- 新增 `GET /api/v1/investigations/{run_id}/collaboration`。子记录通过一条连接查询读取，稳定返回轮次、任务及尝试；旧运行返回空轮次，响应不暴露租约令牌，无写入端点。
- 增加纯规则、真实 PostgreSQL 和 API 测试；迁移测试复用隔离数据库，验证元数据一致及完整升级/回退。本次没有把迁移应用到用户已有的业务数据库。

## 测试方法与结果

Docker 最初未启动，通过 `docker desktop start` 启动后，Testcontainers 使用临时 PostgreSQL 17.6 数据库运行测试，未用 SQLite 替代。自动化测试不依赖模型或遥测服务，不产生模型费用。

在仓库根目录执行：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy

uv run pytest tests/unit/investigation/test_task_contracts.py tests/integration/test_investigation_task_store.py tests/api/test_collaboration_api.py tests/integration/test_migrations.py

uv run pytest tests/integration/test_investigation_store.py tests/integration/test_multi_agent_worker.py tests/integration/test_checkpoint_recovery.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py tests/unit/investigation/test_parallel_graph.py

git diff --check
```

本切片与迁移测试 **35 passed**，覆盖非法分派、结果形状、并发重复创建、内容冲突、版本冲突、重试上限、失效租约、提交边界回滚、跨运行复合外键、并行分支收敛，以及 API 的空历史、稳定顺序、错误格式和令牌隐藏。现有调查、Worker、checkpoint、控制面和固定图回归 **63 passed**，合计 **98 项通过**。Ruff 检查及格式检查通过，mypy 检查 171 个源文件通过。

补充使用 Python 读取 Git 差异与未跟踪文件中的 Markdown，校验 UTF-8、单个一级标题、代码块闭合及仓库内链接：本次十份文档、193 个本地链接通过。`git diff --check` 无错误，已核对当前能力入口，明确区分“任务事实已验收”和“固定图尚未接入”。

未运行前端测试或 Compose 现场演示：本次没有修改前端、模型、遥测适配器和容器配置，正式图接入属于后续切片。

## 限制与下一步

- 这是经过测试的持久化与查询底座，尚未实现动态多 Agent、上下文、记忆、MCP 或新的人工暂停恢复。
- 当前 `running` 是最后提交的任务状态，GET 不更新过期状态。专业尝试的租约失效在合法接管时收敛；父运行终止后的整批子任务收尾将随固定图集成完善，不能把现有记录解释为仍可执行。
- 专业任务暂不持有独立 Worker 队列或独立续租机制；多个专业处理共享父调查租约，只在短业务事务中串行结算，模型调用阶段不占用数据库锁。
- 下一切片将当前固定并行图接入任务事实，明确阶段报告、证据及 SSE 事件的原子提交、父运行结束收尾与 checkpoint 核对；随后逐步加入最小 Harness、调用前预算和异步取证。

## 面试表达

> 我先把多 Agent 协作中的业务任务和技术尝试分开持久化。补查会创建新任务，技术重试则保留任务 ID；父 Worker 接管租约后，旧结果不能覆盖新状态。轮次、任务版本和尝试结算由 PostgreSQL 事务与复合外键约束，只读接口从业务库返回可追踪事实。这一步完成持久化底座，下一步才让固定图实际产生这些中间记录。
