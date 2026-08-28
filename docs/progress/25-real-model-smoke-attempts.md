# 25 - 真实模型单场景冒烟与尝试生命周期

## 目标

为第一次真实模型 API 测试建立可靠入口：调用前保存尝试，调用后保存成功结果或稳定失败分类，同时限制请求、工具、输出 Token、总时长和估算费用。当前不要求用户立刻提供 Key，也不让自动化测试产生费用。

## 设计与边界

- 一条尝试只对应一个冻结场景和一种 Agent 模式，单 Agent、多 Agent 分别审计。
- `running` 必须先于外部调用提交；`succeeded`、`failed` 是不可覆盖终态。
- 相同幂等键重放不再次调用模型；遗留 `running` 不自动重试可能已经计费的请求。
- 模型只读取冻结日志、指标和变更，不能调用 Shell、SQL 或任何处置执行器。
- `.env` 只在真实命令运行时读取，自动化测试使用假配置和假结果。
- 控制面仅提供尝试查询，不提供触发付费调用的 HTTP 接口。

## 实际改动

- 新增 `evaluation_attempts` ORM 映射及 Alembic `0013_evaluation_attempts` 迁移。
- 新增预算、尝试状态、六类失败和持久只读 Pydantic 模型。
- `EvaluationStore` 新增调用前登记、成功/失败条件更新、幂等重放和游标查询。
- 新增模型请求与 Token 回调、费用估算、失败分类和密钥脱敏。
- 新增单场景真实模型运行器及显式 CLI；奇偶重复交替两种模式的先后顺序。
- OpenAI 兼容模型工厂支持请求超时、单请求输出 Token 和回调。
- `.env.example` 增加模型与价格占位配置，真实 `.env` 继续由 Git 忽略。
- 新增两条只读 API，并同步现有前端遥测类型中的请求数和预算失败类别。

## 测试方法与结果

已执行且通过：

```powershell
uv run ruff check src tests alembic
uv run mypy
uv run pytest tests/unit/evaluation tests/unit/investigation/test_settings.py tests/unit/investigation/test_agent.py tests/unit/investigation/test_tools.py tests/api/test_control_plane_openapi.py -q
```

结果：Ruff 与 105 个源码文件 mypy 严格检查通过。测试覆盖预算静态校验、模型请求前阻断、Token 与成本累计、配置哈希不含 Key、错误脱敏和 OpenAPI 只读边界。

Docker Desktop Linux Engine 启动后，使用 Testcontainers 的 PostgreSQL 17 定向测试验证了调用前落库、幂等重放、成功/失败收敛、终态不可覆盖、冒烟运行器重放、只读 API，以及迁移降到 base 后重新升级。随后执行 `uv run pytest -q`，收集到的 248 个后端测试全部通过；`uv run alembic check` 没有发现 ORM 与迁移漂移。

前端执行：

```powershell
cd web
npm run typecheck
npm test -- --run
npm run build
npm audit --audit-level=high
```

结果：类型检查通过，7 个测试文件中的 14 个测试全部通过；生产构建转换 45 个模块，CSS 15.76 kB、JavaScript 122.07 kB；npm 审计发现 0 个漏洞。

本地持久 PostgreSQL 容器 `ops-swarm-control-db-1` 已恢复为 healthy，并从 `0012_evaluation_experiments` 升级到 `0013_evaluation_attempts (head)`。`docker compose config --quiet` 和 `git diff --check` 通过。整个验证过程没有创建 `.env`、读取 API Key 或发起真实模型请求。

## 限制与下一步

- 还没有执行真实模型调用，因此当前没有真实质量、Token 或费用结论。
- 兼容接口只能在响应后得到准确输入 Token，费用上限是响应后累计护栏，不是供应商账单的硬性预授权上限。
- 进程崩溃留下的 `running` 尝试暂不自动恢复，以免重复发起可能已经计费的请求。
- 单场景成功只代表冒烟通过；后续必须在三个场景、多次重复上运行，才能比较均值和方差。
- 下一步是在用户本地 `.env` 配置完成后运行一次低预算真实模型冒烟，并依据实际供应商兼容性修正适配层。

## 面试表达

我没有把 API Key 直接接到完整评测循环，而是先为每个“场景 × Agent 模式”建立持久尝试。系统调用前落库，调用后只能原子进入成功或失败，并用幂等槽位防止重放产生二次费用。LangChain 回调累计请求和 Token，单 Agent 工具层限制调用次数，外层还控制超时、输出 Token 与估算成本。Key 只从被 Git 忽略的 `.env` 读取，数据库只保留不含 Key 的配置哈希。这样真实模型质量、成本和失败率都可审计，而不是只展示成功样本。
