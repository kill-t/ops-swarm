# 21 - 三故障版本化调查评测集

## 目标

把版本回归、数据库连接池配置错误和 Redis 不可用三个 MVP 故障放进同一版本化数据集，让单 Agent 与多 Agent 使用同一输入和评分规则生成可审查对照报告。

## 设计与边界

- 数据集绑定 ID、版本、评分规则版本和三个冻结场景，并计算完整内容 SHA-256。
- 单元测试固定 `v1` 内容哈希，修改场景必须显式升级版本。
- 两种模式按场景交替先后顺序，减轻固定冷启动顺序偏差。
- 报告保存逐场景输出、宏平均和多 Agent 减单 Agent 差值。
- 冻结假模型真实经过 LangChain 工具循环和 LangGraph 并行图，但不代表真实模型能力。
- 当前运行失败会使整轮失败，不跳过失败样本；持久化、失败分类和评测页面留给后续切片。

## 实际改动

1. 新增数据库连接池配置错误和 Redis 不可用冻结调查场景。
2. 新增数据集模型，校验场景身份、版本一致性和内容哈希。
3. 新增逐模式、逐场景、聚合指标和差值报告模型。
4. 新增可注入异步基准运行器、交替执行顺序、单调耗时和稳定小数策略。
5. 新增冻结 LangChain 单 Agent、LangGraph 多 Agent 契约运行入口。
6. 新增离线 JSON 报告命令和聚合质量差异测试。
7. 更新 README、架构事实来源和开发进度索引。

## 测试方法与结果

评测定向测试与离线命令：

```powershell
uv run pytest tests/unit/evaluation `
  tests/unit/investigation/test_agent.py `
  tests/unit/investigation/test_parallel_graph.py -q
uv run python -m opsswarm.evaluation.main
```

结果：13 个定向测试通过。离线命令真实经过三次 LangChain 单 Agent 工具循环和三次 LangGraph 并行调查，输出 3 个逐场景结果；两种模式的宏平均 Top-1、证据召回和建议准确率均为 1，安全违规率为 0，平均只读工具调用数均为 3。冻结模型直接使用场景期望答案，因此只把这些数值作为契约验收，不作为真实模型效果。

实现过程中，固定哈希测试发现 `frozenset` 直接 JSON 化会受 Python 进程哈希随机化影响。修复后规范化器保留列表顺序、稳定排序集合和映射；在两个独立 Python 进程中得到相同内容哈希，并把该值冻结在单元测试中。

完整后端回归：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

结果：Ruff 检查通过，197 个文件格式正确；mypy 检查 96 个源文件无错误；206 个后端测试全部通过。

前端回归：

```powershell
cd web
npm run typecheck
npm test
npm run build
npm audit
```

结果：类型检查通过；5 个测试文件共 9 个测试通过；Vite 构建 38 个模块，JavaScript 约 108.63 kB，gzip 后约 40.97 kB；依赖审计为 0 个已知漏洞。

基础设施与文档检查：

```powershell
docker compose config --quiet
uv run alembic current
docker compose ps
git diff --check
```

结果：Compose 配置有效，业务库仍位于 `0010_verification_runs (head)`，固定 PostgreSQL 与订单服务容器均为 healthy；Markdown 相对链接、代码围栏和 Git 空白检查通过。本切片没有数据库迁移，也没有创建临时容器或删除数据卷。

## 限制与下一步

- 数据量只有三个主路径场景，不能代表真实模型泛化能力。
- 当前安全违规率只针对通过结构校验的结果，没有记录被护栏拒绝的危险尝试。
- 没有模型调用数、Token、成本、失败分类和统计置信区间。
- 评测事实尚未进入 PostgreSQL，也没有评测 API 与面板。
- 下一切片实现评测用例、运行和结果的最小持久化与只读查询。

## 面试表达

> 我把三个故障场景绑定为带版本、评分规则和内容哈希的数据集，单 Agent 与多 Agent 必须用同一把尺子。运行顺序按场景交替，报告保留逐样本和宏平均差值。冻结模型的满分只证明 LangChain、LangGraph、证据护栏和评分管线可复现，不代表真实模型质量；真实结论还需要失败率、Token、成本和重复实验。
