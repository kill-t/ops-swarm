# 22 - 评测结果持久化与只读查询

## 目标

把三故障公平对照从一次性 JSON 输出升级为 PostgreSQL 中可追溯的评测事实，并给后续评测面板提供稳定、只读、可分页的数据源。

## 设计与边界

- 使用 `evaluation_cases`、`evaluation_runs`、`evaluation_results` 三张表，分别保存数据集快照、运行聚合和逐场景明细。
- 离线命令是当前唯一写入口；控制面只提供列表和详情 `GET`，不允许未鉴权 HTTP 请求启动评测或改写报告。
- 一份报告在单事务中完整写入，数据集版本冲突、幂等键冲突和并发重复由应用校验及 PostgreSQL 唯一约束共同处理。
- 数据集快照和报告分别计算 SHA-256；读取时重新构造 Pydantic 模型并验证版本、真值、顺序和内容哈希。
- `sequence` 是全局递增分页游标，不使用会因新数据插入而漂移的 offset。
- 当前只保存已经完成的冻结契约报告，不提前设计真实模型批量任务的运行中与失败状态。

## 实际改动

1. 新增 Alembic `0011_evaluation_storage`，创建评测三表、外键、唯一约束、JSONB 形状约束和安全降级检查。
2. 新增评测 ORM、持久化输入、只读记录、领域错误和 `EvaluationStore`。
3. 实现数据集版本防静默改写、完整报告哈希、逐场景与聚合指标重算、同键重放、冲突拒绝和并发收敛。
4. 扩展离线命令：默认只输出 JSON；只有显式 `--persist` 且提供幂等键时才连接业务库。
5. 新增评测运行列表和详情只读 API，以及稳定 `evaluation_run_not_found` 错误。
6. 新增仓储、并发、篡改检测、命令参数、OpenAPI 和真实 PostgreSQL API 测试。
7. 更新系统架构、数据库、API、评测协议、README 和进度索引。

## 测试方法与结果

定向评测、仓储与 API 测试：

```powershell
uv run pytest tests/unit/evaluation/test_main.py `
  tests/integration/test_evaluation_store.py `
  tests/api/test_control_plane_openapi.py `
  tests/api/test_control_plane_api.py -q `
  -k "evaluation or persist or idempotency or integrity or openapi"
```

结果：19 个目标测试通过。覆盖首次写入、相同报告重放、同键不同报告冲突、同版本不同数据冲突、两个并发提交只创建一轮、稳定游标分页、不存在详情、快照篡改拒绝、冻结真值评分及聚合指标伪造拒绝、命令显式开关、只读 OpenAPI 和真实 PostgreSQL API 响应。

完整后端回归：

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
git diff --check
```

结果：222 个后端测试全部通过；Ruff 检查与 207 个文件格式检查通过；mypy 检查 101 个源文件无错误；Git 空白检查通过。

本地固定数据库验证：

```powershell
docker compose ps
uv run alembic upgrade head
uv run alembic current
uv run alembic check
uv run python -m opsswarm.evaluation.main `
  --persist `
  --idempotency-key fixture-contract-20260827-001
```

结果：固定 PostgreSQL 与订单服务均为 healthy；业务库从 `0010_verification_runs` 升级到 `0011_evaluation_storage (head)`；Alembic 未发现模型漂移；命令首次返回 `created=true`，保存 1 个数据集、1 个运行和 3 个逐场景结果。

前端回归：

```powershell
cd web
npm run typecheck
npm test -- --run
npm run build
npm audit
```

结果：类型检查通过；5 个测试文件共 9 个测试通过；Vite 构建 38 个模块，JavaScript 约 108.63 kB，gzip 后约 40.97 kB；依赖审计为 0 个已知漏洞。第一次与多个 PostgreSQL 测试容器并行运行 Vitest 时，Windows 上的 fork worker 启动超时；资源释放后独立重跑全部通过，没有为环境抖动修改业务代码。

## 限制与下一步

- 当前评测只记录成功完成的整轮报告，真实模型失败、超时、取消和部分完成尚未建模。
- 报告尚未记录模型名、提示版本、Token、成本和被护栏拒绝的危险尝试。
- 三个冻结主路径样本不能代表真实泛化能力，耗时也没有统计置信度。
- API 没有 JWT，只能用于 localhost 和 Compose 私有网络。
- 下一切片可实现只读评测面板，展示历史聚合和逐场景对照；之后再扩展真实模型重复评测和失败分类。

## 面试表达

> 我没有把 Agent 评测停留在命令行结果，而是把数据集快照、运行聚合和逐场景明细原子写入 PostgreSQL。数据集与报告都有内容哈希，同一幂等键只绑定一份确定报告，并发重试靠唯一约束收敛。为了控制成本和数据可信度，当前只有离线命令能写，HTTP 只读；读取详情还会重新验证快照、真实标签和报告哈希。
