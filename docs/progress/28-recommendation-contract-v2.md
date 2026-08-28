# 28 - 处置建议契约与评测集 v2

## 目标

修复 v1 真实批量评测暴露的建议枚举过粗问题：模型正确识别数据库配置故障时，不应被迫在“回滚发布”和“继续调查”之间选择。同时冻结 v1 历史，创建可追溯的 v2 数据集和提示版本。

## 设计与边界

- 新增 `rollback_configuration` 和 `restore_dependency`，保留 `rollback_release` 与 `continue_investigation`。
- 三种具有变更含义的建议必须声明人工审批；模型仍没有 Shell、SQL、容器控制或数据库写权限。
- v1 数据集、评分规则、场景和哈希保持不变；v2 使用新身份和新哈希。
- 批量命令默认仍为 v1，新实验通过 `--dataset-version 2` 显式选择 v2。
- 提示版本升级为 `single-agent-v2` 与 `multi-agent-parallel-v2`，调查持久化和评测元数据引用同一事实来源。
- 本切片只扩展调查建议语义，不扩展 Action Plan 或执行器。当前仍只有 `rollback_release` 可进入既有白名单处置链路。

## 实际改动

- 扩展 `Recommendation` 并添加统一的 `requires_human_approval` 属性。
- 在 `InvestigationConclusion` 中校验所有动作建议都必须标记人工审批。
- 更新单 Agent 和多 Agent 综合提示中的建议选择规则。
- 新增三条 v2 场景及 `opsswarm-mvp-investigation-v2` 数据集。
- 保留 v1 哈希 `d3ded4e326b1aa0a8e06c38287ce22c6bc454574c6ef19b37e11cbc5803ecf7f`，冻结 v2 哈希为 `60e218d3f4e3dc106c1ac73ae630f77302af36f5001310ab3fb17e2821bf395c`。
- 让离线评测和真实批量评测显式接收数据集版本，并拒绝场景版本混用。
- 补充枚举校验、数据集不可变性、命令选择、离线评测和 PostgreSQL 持久化测试。
- 更新 README、单 Agent、多 Agent 和真实批量评测架构文档。

## 测试方法与结果

执行完整离线回归：

```powershell
uv run pytest -q
uv run ruff check src tests alembic
uv run ruff format --check src tests alembic
uv run mypy
uv run alembic check
docker compose config --quiet
git diff --check
```

完整测试、Ruff、mypy、迁移一致性、Compose 配置和 Git 空白检查均通过。测试期间 Docker Desktop 引擎曾退出，首次 PostgreSQL 集成测试因此无法连接；恢复 Docker Desktop 并启动固定的 `control-db` 后，同一组测试通过，证明失败来自本地基础设施而不是断言或业务代码。

执行 DeepSeek v2 三场景真实评测：

```powershell
uv run python -m opsswarm.evaluation.real_batch_main `
  --experiment-key deepseek-v4-flash-recommendation-v2-20260828 `
  --idempotency-prefix deepseek-v4-flash-recommendation-v2-20260828 `
  --dataset-version 2 `
  --repetition-start 1 `
  --repetitions 1
```

实际结果：

| 指标 | 单 Agent | 多 Agent |
| --- | ---: | ---: |
| 成功样本 | 3/3 | 3/3 |
| 根因 Top-1 | 1.0 | 1.0 |
| 证据召回 | 1.0 | 1.0 |
| 建议准确率 | 1.0 | 1.0 |
| 安全违规率 | 0 | 0 |
| 请求总数 | 6 | 12 |
| Token 总数 | 9652 | 11225 |
| 估算费用（美元） | 0.005785 | 0.006879 |
| 平均耗时 | 6970.799 ms | 5602.912 ms |

合计估算费用约为 0.012664 美元。六条结果分别正确选择版本回滚、配置回滚和依赖恢复。再次执行相同命令复用了原有终态记录，没有新增模型调用、Token 或费用。

## 限制与下一步

- 三个场景各只有一轮真实样本，不能给出统计显著性或稳定速度结论。
- `rollback_configuration` 和 `restore_dependency` 目前只是调查结论，不可执行。
- 下一步建立显式 Action Plan 能力映射，先让 API 清楚表达“建议已识别但尚无受支持执行模板”，再逐项设计参数、风险和本地幂等适配器。

## 面试表达

我通过真实模型评测发现，系统能判断根因，但输出枚举无法表达正确处置。为避免破坏历史可比性，我冻结了 v1，新建数据集和评分规则 v2，并升级提示版本。动作建议统一由 Pydantic 强制人工审批，新增枚举也不会自动获得执行权限。DeepSeek 三场景复测中，单 Agent 和多 Agent 的建议准确率都从 0.667 提升到 1.0，同时完整记录了请求数、Token、成本和幂等重放结果。
