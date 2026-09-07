# 74 - 冻结场景身份的确定性与历史兼容

## 目标

修复第 73 步确认的跨进程恢复问题：同一冻结场景中的集合序列化顺序不同，导致新 Worker 误认为证据内容已经变化。修复必须保留历史任务、材料散列和预算事实。

## 设计与边界

先更新 [专项设计](../architecture/74-stable-frozen-identity.md)、数据库及 API 入口。新任务使用 `frozen-v2:` 版本化内容身份，对必需证据集合和 JSON 对象键做确定性排序；不改变证据数组顺序、场景真值或快照格式。

旧任务通过完整内容身份对应的有限兼容表恢复，覆盖三个内置场景各自的 v1、v2，共六份内容和 20 个旧身份。兼容表在开发时按原序列化规则枚举生成，运行时仅查表。未知或自定义旧内容不被静默接受，需要找回原版本材料并建立经过验证的兼容映射。

## 实际改动

- `frozen_identity.py` 负责确定性内容身份与旧身份授权查询；`frozen_legacy_ids.py` 单独保存发布版本的兼容目录。
- 冻结数据源提供新身份及对应的旧身份集合；`TaskGraphFactory` 将兼容范围注入固定任务运行器，现场来源不使用旧冻结身份。
- 准备任务时先检查有效租约及预算，再读取持久任务。全部任务使用同一个受支持旧身份时沿用原值，再经事务内完整分派校验恢复；新任务使用新身份。
- `create_round` 在原规范化分派和散列核对之外，检查任务表的完整 assignment 与分派一致。兼容旧 ID 不能绕过角色、时间窗、服务和任务内容约束。
- 不新增迁移、接口或依赖，不改写旧任务、证据、报告、事件或 checkpoint。历史有快照任务复用原材料；无快照策略的旧任务保留重新取证语义；重试不清零额度。

## 测试方法与结果

验收日期：2026-09-07。身份单元测试使用独立 Python 解释器；恢复测试通过 stdin 传递测试连接配置，启动不同 `PYTHONHASHSEED` 的独立 Worker 进程，共用真实 PostgreSQL 业务库及隔离 checkpoint 库。旧任务由保留原身份算法的测试数据源生成，随后由标准来源恢复。假模型和冻结来源参与测试，不调用真实模型或外部遥测。


```powershell
.venv/Scripts/python.exe -u -m pytest tests/unit/investigation tests/unit/evaluation/test_dataset.py tests/unit/evaluation/test_runner.py tests/integration/test_frozen_process_recovery.py tests/integration/test_frozen_identity_scope.py tests/integration/test_investigation_task_store.py tests/integration/test_task_result_transactions.py tests/integration/test_checkpoint_recovery.py tests/integration/test_fixed_task_graph.py tests/integration/test_budgeted_graph.py tests/integration/test_model_budget_graph.py tests/integration/test_async_evidence_graph.py tests/integration/test_task_evidence_store.py tests/integration/test_evidence_snapshot_recovery.py tests/integration/test_live_task_graph.py tests/api/test_collaboration_api.py tests/api/test_task_evidence_api.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

统一验收 **390 passed，耗时 227.59 秒**。其中本片新增的 22 项测试覆盖多进程确定性、六份内容的全部旧身份、内容变化、未知/混合身份、任务时间窗篡改，以及三种独立 Worker 进程恢复组合。第一次启动使用哈希种子 1，恢复进程使用种子 2；新任务、旧任务有快照、旧任务无快照均成功，保留原任务 ID、分派和已提交报告。模型调用预留累计 5 次，工具调用预留有快照累计 3 次、无快照累计 4 次，材料记录和散列不被改写。

现场 v2、原固定图、任务幂等/事务、checkpoint、预算、专业取证快照、协作 API 及冻结评测回归通过。Ruff 检查、518 个文件格式检查和 mypy（207 个源文件）通过；本片 9 篇 Markdown 的结构、本地链接及 `git diff --check` 检查通过。初次数据库测试因 Docker 未启动而未运行，启动 Docker 后完成上述真实 PostgreSQL 验收；未迁移已有业务环境。

## 限制与下一步

- 兼容表只覆盖已发布内置版本的原 JSON 序列化形态；不因 `frozen:` 前缀相同而放行，也不枚举任意自定义数据的排列。
- 新身份的规则有独立版本，后续调整规范化算法时仍需兼容设计，不能直接覆盖旧 ID。
- 下一片实现 ContextBuilder 的任务输入契约、不可变材料清单与既有模型预算接入，随后再加入按需读取、工作摘要及动态协作。

## 面试表达

> 我在跨进程测试中发现，同一冻结数据里的集合序列化顺序会改变证据身份，导致 Worker 重启后误判数据发生变化。我增加版本化的确定性内容身份，并为已经发布的旧版本建立有限兼容表，让旧任务沿用原 ID 和原材料散列。恢复仍核对完整任务分派，不会因为 ID 兼容就跳过服务、角色或时间窗检查。用不同哈希种子的独立 Worker 进程验证失败恢复、材料复用和预算累计，而不是只在一个进程里重新创建对象。
