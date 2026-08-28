# 27 - 真实模型三场景批量评测

## 目标

把单场景真实冒烟扩展为可恢复、受总预算约束的三场景批量运行，并用同一份报告同时呈现成功率、失败计零分质量、成功样本质量、请求、Token、成本、耗时和波动。

## 设计与边界

- 复用 `evaluation_attempts` 作为唯一业务事实，不新增批次表或重复保存终态。
- 默认运行三个版本化冻结场景各一次，单 Agent 与多 Agent 共六条尝试。
- 场景位置与重复轮次共同决定模式先后顺序，减少固定顺序偏差。
- 新增批次输出成本预检和响应后累计费用检查，本轮批次上限为 0.10 美元。
- 失败在整体 Top-1 中计零分，同时单独保留成功样本质量，避免成功者偏差。
- 运行中尝试不进入完成报告，也不会被自动重放。
- 控制面仍不提供付费评测写接口，真实调用只能由本地显式命令启动。

## 实际改动

- 新增批量尝试摘要、单模式聚合、模式差值和完整矩阵报告模型。
- 新增批量运行器，检查场景、轮次、幂等键和输出费用上限。
- 新增真实模型批量 CLI，默认覆盖三场景一次，最多允许连续 10 轮但仍受费用护栏限制。
- 单场景运行器支持由批量层显式交替两种模式的先后顺序。
- `.env.example` 新增 `EVALUATION_MAX_BATCH_COST_USD`。
- 单元测试覆盖失败计零、成功质量、费用/耗时标准差、运行中拒绝和危险参数拒绝。
- PostgreSQL 集成测试覆盖六格矩阵、实际执行顺序和幂等重放不重复插入。

## 测试方法与结果

执行前预检得到六条尝试的最大输出 Token 费用为 0.0497664 美元，低于 0.10 美元批次上限。随后运行：

```powershell
uv run python -m opsswarm.evaluation.real_batch_main `
  --experiment-key deepseek-v4-flash-three-scenario-20260828 `
  --idempotency-prefix deepseek-v4-flash-three-scenario-20260828 `
  --repetition-start 1 `
  --repetitions 1
```

真实结果：六条尝试全部成功，根因 Top-1 和证据召回均为 1，安全违规率为 0。单 Agent 共请求 6 次、使用 9404 tokens、估算 0.005763 美元；多 Agent 共请求 12 次、使用 11395 tokens、估算 0.007137 美元。两种模式在数据库连接池场景都选择 `rollback_release`，使建议宏平均为 0.666667，并暴露建议枚举缺少“回滚配置”的契约问题。

使用完全相同的命令重放后，返回的仍是原尝试序号 `5、6、8、7、9、10`，约 12.86 秒完成，没有新建尝试或再次发起模型请求。

自动化验证：

```powershell
uv run ruff check src tests alembic
uv run mypy
uv run pytest tests/unit/evaluation/test_real_batch.py `
  tests/unit/evaluation/test_real_batch_main.py `
  tests/unit/evaluation/test_real_model.py `
  tests/unit/evaluation/test_real_main.py `
  tests/integration/test_evaluation_attempt_store.py -q
uv run pytest -q
uv run alembic check
docker compose config --quiet
git diff --check
```

Ruff 通过，mypy 检查 107 个源码文件无错误，定向测试和完整后端测试全部通过。迁移、Compose 和 Git 格式检查在提交前完成最终验证。

## 限制与下一步

- 只有一轮真实结果，耗时差异很容易受网络抖动影响，不能宣称多 Agent 更快。
- 批次费用是 usage 估算，不是供应商账户侧硬限额。
- 聚合报告当前由命令即时派生，尚未提供独立只读 API 或评测面板视图。
- 当前建议枚举把配置回滚和版本回滚挤进同一个标签，已经影响真实评分。
- 下一步先设计 `Recommendation` v2 和评测集 v2，保持 v1 数据、哈希和历史尝试不可变，再进行多轮重复评测。

## 面试表达

我没有把三个场景简单写成 for 循环。批量层先验证计划和总输出预算，每个样本仍通过调用前落库与幂等槽位保存，因此崩溃重放不会重复付费。聚合时同时展示失败计零分的整体质量和成功样本质量，避免只挑成功结果。真实运行还发现两种 Agent 都能找对数据库连接池根因，却因为建议枚举没有“回滚配置”而被迫选择 `rollback_release`。这说明评测不仅比较模型，也会反过来发现输出契约设计缺陷。
