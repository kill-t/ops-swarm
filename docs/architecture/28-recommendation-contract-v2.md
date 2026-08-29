# 处置建议契约与评测集 v2

## 1. 问题与目标

评测集 v1 的首次三场景真实模型运行暴露了一个契约问题：数据库连接池配置错误已被正确识别，但模型只能在 `rollback_release` 和 `continue_investigation` 中选择。把“已知道应回滚配置”标成“继续调查”会惩罚合理答案，把它标成“回滚发布”又会混淆操作对象。

本切片扩展结构化建议语义，并创建不可变的评测集 v2。目标是让“根因判断正确”和“处置类型正确”分别可测，同时不改写任何 v1 历史。

## 2. 建议语义与安全边界

`Recommendation` 现在包含四种稳定枚举：

| 枚举值 | 业务含义 | 必须人工审批 |
| --- | --- | --- |
| `rollback_release` | 回滚导致回归的应用版本 | 是 |
| `rollback_configuration` | 回滚或修正已确认的错误配置 | 是 |
| `restore_dependency` | 恢复不可用的依赖服务 | 是 |
| `continue_investigation` | 证据不足，继续只读调查 | 否 |

Pydantic 在结构化结论边界强制前三种动作建议携带 `requires_human_approval=true`。单 Agent 与多 Agent 综合提示都写明了映射规则，但提示只是辅助模型理解；真正的安全约束仍由本地校验、审批和白名单执行器提供。

新增枚举不等于新增执行能力。后续已为 `rollback_configuration` 建立只生成草案的独立参数契约和风险策略，但审批与执行仍未开放；`restore_dependency` 仍没有计划模板。只有完整建立审批边界、本地幂等适配器和恢复验证后，动作才可执行。

## 3. 数据集版本策略

v1 被完整冻结，v2 使用新的数据集键、版本号、评分规则版本和内容哈希：

| 数据集 | 评分规则 | 发布回归 | 数据库配置 | Redis 不可用 |
| --- | --- | --- | --- | --- |
| `opsswarm-mvp-investigation-v1` | `investigation-scoring-v1` | `rollback_release` | `continue_investigation` | `continue_investigation` |
| `opsswarm-mvp-investigation-v2` | `investigation-scoring-v2` | `rollback_release` | `rollback_configuration` | `restore_dependency` |

v1 内容 SHA-256 保持为 `d3ded4e326b1aa0a8e06c38287ce22c6bc454574c6ef19b37e11cbc5803ecf7f`；v2 内容 SHA-256 为 `60e218d3f4e3dc106c1ac73ae630f77302af36f5001310ab3fb17e2821bf395c`。运行报告继续保存数据集快照和哈希，因此将来代码升级后仍可解释历史分数。

批量命令的默认版本保持为 v1，防止已有脚本在升级后静默改变实验含义。使用 v2 必须显式声明：

```powershell
uv run python -m opsswarm.evaluation.real_batch_main `
  --experiment-key deepseek-v4-flash-recommendation-v2-exp01 `
  --idempotency-prefix deepseek-v4-flash-recommendation-v2-exp01 `
  --dataset-version 2 `
  --repetition-start 1 `
  --repetitions 1
```

场景 ID 也按版本区分。命令会在发起模型请求前拒绝版本与场景不匹配的计划。

## 4. 提示版本与历史兼容

提示版本升级为：

- 单 Agent：`single-agent-v2`；
- 多 Agent：`multi-agent-parallel-v2`。

提示版本常量由调查运行和评测元数据共同引用，避免两处字符串漂移。旧 v1 运行和尝试不迁移、不回填；新提示版本会产生不同的实验配置指纹。重复使用旧实验键但改变数据集或提示版本时，持久化层会拒绝配置冲突，而不是把不可比较的样本合并。

本切片不修改数据库结构。建议值保存在已有字符串或 JSONB 载荷中，不需要 Alembic 迁移；业务约束由应用层枚举和结构化模型管理。

## 5. DeepSeek v2 实测结果

使用当前 `.env` 中的 DeepSeek OpenAI 兼容配置，对 v2 三个场景各运行一轮单 Agent 和多 Agent，共六条尝试，全部成功：

| 指标 | 单 Agent | 多 Agent | 多减单 |
| --- | ---: | ---: | ---: |
| 成功数 | 3/3 | 3/3 | 0 |
| 根因 Top-1 | 1.0 | 1.0 | 0 |
| 证据召回 | 1.0 | 1.0 | 0 |
| 建议准确率 | 1.0 | 1.0 | 0 |
| 安全违规率 | 0 | 0 | 0 |
| 模型请求总数 | 6 | 12 | 每场景平均 +2 |
| Token 总数 | 9652 | 11225 | 每场景平均 +524.333334 |
| 估算总费用（美元） | 0.005785 | 0.006879 | 每场景平均 +0.000365 |
| 平均耗时 | 6970.799 ms | 5602.912 ms | -1367.887 ms |

两种模式都分别输出了 `rollback_release`、`rollback_configuration` 和 `restore_dependency`。建议准确率从 v1 的 `0.666667` 提升到 v2 的 `1.0`，证明这次修改解决了已知标签表达问题；但只有一轮小样本，不能据此推断总体准确率或认定多 Agent 更快。

使用同一实验键、幂等前缀和轮次重放时复用了六条终态尝试，没有产生新模型调用或费用。

## 6. 下一步

后续 [确定性处置计划能力映射](29-action-plan-capability-mapping.md) 已建立“根因 + 建议”到模板的确定性映射。不受支持的建议保持只读结论，不能自动降级成其他动作，也不能绕过审批和白名单执行器。

## 7. 面试表达

真实模型评测发现根因正确但建议分数偏低后，我没有直接改写历史真值，而是判断输出契约缺少配置回滚和依赖恢复语义。我冻结 v1，创建带独立哈希和评分规则的 v2，并同步升级提示版本。所有具有变更含义的建议都由 Pydantic 强制人工审批，而且新增语义不会自动获得执行权限。真实三场景复测把两种模式的建议准确率从 0.667 提升到 1.0，同时保留 Token、费用和耗时，证明改动解决的是可追溯的契约问题，而不是用不可复现方式美化分数。
