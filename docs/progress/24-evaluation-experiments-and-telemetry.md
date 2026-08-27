# 24 - 评测实验身份、重复运行与遥测

## 目标

把“同一数据集上的两次分数”升级为真正可比较、可审计的实验：固定模型和提示配置，明确重复序号，并分别保存单 Agent、多 Agent 的 Token、成本与失败分类。当前仍不调用真实付费模型，只先建立可靠的数据与查询边界。

## 设计与边界

- 新增 `evaluation_experiments`，固定数据集、运行器、评测类型、模型身份、参数哈希和两套提示版本。
- `fixture_contract` 不允许声明真实模型身份；`real_model` 必须完整提供模型提供方、名称和参数 SHA-256。
- `evaluation_runs` 增加实验外键、1～1000 的重复序号及 `(experiment_id, repetition_index)` 唯一约束。
- 单 Agent、多 Agent 分别保存输入、输出、总 Token，美元估算成本，以及模型请求、工具、超时、结构化输出和安全拒绝五类计数。
- 总 Token 必须能由输入和输出重算；冻结假模型显式保存零遥测，不使用含糊的 `null`。
- 幂等重放比较报告、实验、重复序号、运行器和遥测，不能用同一键偷偷改写成本或配置。
- 当前仍只保存最终完成报告；失败计数表示已经恢复或纳入计分的失败尝试，不等于完整失败运行生命周期。
- API 保持只读，密钥、模型 `base_url` 和完整参数快照不写数据库。

## 实际改动

- 新增评测实验、模型身份、失败分类和模式级遥测的不可变 Pydantic 模型。
- 新增 Alembic `0012_evaluation_experiments` 迁移和 SQLAlchemy 映射。
- 为旧评测运行创建 `fixture-backfill-{sequence}` 实验，保留原报告并回填重复序号 1 和零遥测。
- 扩展 `EvaluationStore` 的并发实验创建、重复槽位冲突、元数据幂等比较和读取完整性校验。
- 离线持久化命令新增必填 `--experiment-key` 和可选 `--repetition-index`。
- 只读评测 API 返回实验、重复序号和两种模式遥测。
- 评测面板新增实验上下文及资源失败对照，列表显示实验键和重复次数。
- 增加模型身份、Token 重算、重复运行、冲突、非零遥测往返、篡改检测和 API 契约测试。

## 测试方法与结果

后端执行：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run alembic check
uv run pytest -q
```

结果：Ruff、214 个 Python 文件格式检查、102 个源码文件 mypy 严格检查和 Alembic 元数据检查通过；收集到的 234 个后端测试全部通过。迁移测试在隔离 PostgreSQL 17 上验证了空库升级、降到 base 和重新升级，评测仓储测试覆盖真实 PostgreSQL 并发与约束语义。

前端执行：

```powershell
cd web
npm run typecheck
npm test -- --run
npm run build
npm audit --audit-level=high
```

结果：类型检查通过，7 个测试文件中的 14 个测试全部通过；生产构建转换 45 个模块，CSS 15.76 kB、JavaScript 122.05 kB；npm 审计发现 0 个漏洞。

本地持久环境验证：

- `ops-swarm-control-db-1` 保持 healthy，业务库从 `0011_evaluation_storage` 升级到 `0012_evaluation_experiments (head)`；
- 原有评测报告保留，回填为 `fixture-backfill-1`、第 1 次、零 Token 和零成本；
- 真实浏览器能够读取新增 API 字段，桌面与 390 × 844 窄屏布局正常；
- 页面刷新后仍恢复相同运行，实验上下文和两个遥测卡片均存在，控制台无错误。

提交前还执行 Markdown 相对链接、代码围栏、Git 空白和常见密钥格式检查。

## 限制与下一步

- 还没有真实模型运行器，当前数据库支持真实模型身份，但项目没有产生真实模型质量或成本结论。
- 完整失败、取消和运行中 attempt 尚未持久化；完成报告内的失败计数不能替代生命周期。
- 美元成本是未来运行器按价格规则计算的估算值，目前未保存价格规则版本。
- 真实多次重复尚未产生，因此页面不画趋势、方差或置信区间。
- 下一切片应实现“单场景、单次、显式预算”的真实模型评测运行器，并优先落库失败 attempt，避免失败样本从完成报告中消失。

## 面试表达

我发现只有数据集版本和最终分数还不足以比较 Agent，于是增加了实验实体，把模型、参数哈希、运行器和两套提示版本冻结下来。每轮运行用实验加重复序号唯一定位，并分别记录单 Agent、多 Agent 的 Token、成本和五类失败。幂等重放会比较这些元数据，数据库和 Pydantic 双层校验模型身份及 Token 总数。迁移还为旧报告无损回填实验身份，因此新能力没有破坏已有审计历史。
