# 110 - 有界来源核验与事务内复用

## 目标

解决第 109 步三轮调查完成模型和工具调用后，最终来源重建超过 600 秒的问题。基线为 main 的 `b150018`；不读取或提交用户未跟踪的 `output/`。

## 设计与边界

先保存 [来源核验设计](../architecture/110-bounded-source-validation.md)。重复路径为后续计划→前轮路由→审查→假设→综合调用→上下文→前轮计划；多处再次回查同一假设和计划，形成重复展开的菱形依赖。

只复用当前一次来源遍历中已经完整成功的计划、假设、审查和路由 DTO。最外层返回就清空，即使下一次操作仍在同一事务中也重新核验；不跨 Session、事务、savepoint、协程、模型请求或业务写入复用。保留原父租约进出检查，不缓存预算、执行资格或当前经验有效性。v1/v2、未持久化行和带 pending ORM 变化的入口沿用原路径。

独立审查指出，不能只因计划/假设不可变便缓存整个来源链：它们仍依赖任务状态、轮次完成、成功调用集合、事故输入、历史租约及经验审核授权。因此每次访问复用表前和结果保存前，都以一条 PostgreSQL 查询重查依赖行的表名、主键、xmin、ctid 集合。插入/删除、跨会话修改和同事务多次 UPDATE 均会改变集合；发生变化就拒绝本次核验，下一作用域重新读取。行标记不作为业务版本持久化，也不把它们当作授权或审计记录。

戳查询每表最多读取 513 条，超过 512 条或合计超过 4,096 条拒绝；成功节点和正在访问节点合计上限 64。保护完整成功集合，不能仅记录最初读到的几个调用 ID。冻结经验链包括对应审核授权行；当前停用/替代集合不影响历史恢复。

初始刷新已加载列并比较，防止将新数据库戳绑定到旧 identity map；每次检查还比较内存中的加载列，防止原地 JSON 修改绕过 dirty 标记。返回 DTO 深副本，异常、取消、循环、容量超限、flush 及事务/savepoint 变化会使整次遍历失效。即使父函数吞掉子核验异常，也不能得到成功缓存。

## 实际改动

- `persistence/source_revision.py` 管理固定表集合和一次查询的新鲜度门禁，只读取有界行标记，不加载大模型输入/日志正文，也不接受调用方 SQL 标识符。
- `persistence/source_validation.py` 管理一次遍历的生命周期、输入指纹、结果副本、变化/异常失效和上限；数据库新鲜度与复用生命周期职责分开。
- 协调、综合、审查、路由各持久模块只给既有重建函数加复用边界，原业务规则和错误校验继续执行，不增加编排框架。
- 没有修改 HTTP 字段、运行状态、模型输入、图结构、checkpoint 版本或数据库 schema；head 保持 `0066_coordination_experience`。

## 测试方法与结果

分组回归 **1,519 项全部通过**：单元/API 1,477 项（500.88 秒）；新增作用域与三轮性能/恢复 24 项（223.04 秒）；既有专业并行、计划持久化及第三轮提交窗口恢复 16 项（242.75 秒）；第二轮综合/审查成功来源歧义拒绝 2 项（91.74 秒）。同一用例的前期定向试跑不重复计数。

Ruff/格式检查覆盖 645 个 Python 文件通过；mypy 检查 353 个源文件通过。Git 差异及文档相对链接检查通过，head 保持 `0066_coordination_experience`。无 schema 或迁移变更，本轮未重复整组迁移回归；所有数据库用例仍从正式 head 建立独立 Testcontainers PostgreSQL 17.6。没有升级用户 Compose 数据库或调用外部模型。

新增作用域边界及三轮性能测试共 **24 项通过，223.04 秒**。作用域用真实 PostgreSQL 检查菱形节点只重建一次、DTO 深副本、独立操作/事务/会话/运行/协程隔离、同事务第二次 UPDATE 的 xmin 相同但 ctid 改变、集合增删、跨会话改动、savepoint 来回、陈旧 ORM、原地 JSON、pending 降级、失败/取消、吞掉异常及新成功调用集合。经验授权案例来自真实关闭/复盘/生成/确认业务链。

完整 v15 图性能和更换父租约后的 checkpoint 恢复记录如下；SQL 数只统计测试业务 SessionFactory 绑定的引擎，不包括 checkpoint 引擎及 API 自己的独立引擎。耗时包含实际 SDK 假传输与业务来源核对，不是生产性能承诺。

| 首轮经验策略 | 调查耗时 | 调查 SQL / 门禁 | 调查及恢复总耗时 | 总 SQL / 门禁 |
| --- | --- | --- | --- | --- |
| 省略，保持旧输入 | 68.42 秒 | 14,236 / 3,106 | 76.84 秒 | 15,662 / 3,438 |
| 显式启用经验 | 89.76 秒 | 15,778 / 3,106 | 100.10 秒 | 17,330 / 3,438 |

两条路径均完成三轮专业调查、综合、独立审查和候选终端路由，账本均为 13 次模型、6 次工具调用。经验只进入首轮协调；停用经验并更换父租约后恢复不重查有效集合或重调 SDK。回归上界固定为调查及恢复合计 180 秒、25,000 条业务 SQL，不删除任何来源校验以达标。首次试跑设置 15,000 条 SQL 的试探阈值，因旧策略完整路径为 15,662 条触发断言；测量两种策略后固定当前上界。这是新增测试的阈值校准，不是现有性能标准被放宽。

```powershell
.venv/Scripts/python.exe -m pytest tests/integration/test_bounded_source_validation_graph.py tests/integration/test_source_validation_scope.py -o addopts= -q -s --disable-warnings -x --tb=short
.venv/Scripts/python.exe -m pytest tests/unit tests/api -o addopts= -q --disable-warnings -x --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_first_round_graph.py tests/integration/test_coordination_plan_store.py tests/integration/test_dynamic_third_round_routing_graph.py::test_third_round_routing_recovers_across_fact_commit_window -o addopts= -q --disable-warnings -x --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_dynamic_second_round_review_graph.py::test_second_round_review_rejects_multiple_successful_calls_in_same_slot tests/integration/test_dynamic_second_round_synthesis_graph.py::test_second_round_hypothesis_rejects_multiple_successful_calls_in_same_slot -o addopts= -q --disable-warnings -x --tb=short
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
git diff --check
```

一个子智能体只读核查安全边界并独立编写作用域测试；主 Agent 实现、补充失败失效测试、独立核查、运行全部数据库和回归验证并提交。独立复核发现循环/上限异常若被父函数吞掉，可能留下有效作用域；已统一置失效，并以吞掉循环异常的测试验证。

## 限制与下一步

复用是单次来源读取的优化，不是跨请求缓存或业务事实存储。并发变化采用保守拒绝，当前实现也可能因 identity map 中无关已加载对象变化而要求重新读取；不保证读写并发下零重试。物理行戳仅适用于当前 PostgreSQL 实现，不提供可移植数据库抽象。

两种冻结三轮场景及恢复完成验收；没有重新验收所有生产负载、现场工具或完整发布，也没有实现 v3 通用 Worker 和图内人工介入。下一步推进 v3 通用 Worker 接管与终端路由的持久交接，先明确等待状态、租约释放和两类终端分支，不把 `await_human` 或 `candidate_evaluation` 当作旧调查成功或执行授权。

## 面试表达

> 我先定位出来源校验中的重复 DAG 展开，没有直接关闭校验或加跨请求缓存。一次遍历内成功的不可变结果可以复用，但每次命中都重新检查相关行版本和集合，覆盖任务状态、成功调用唯一性及经验审核授权。事务、savepoint、异常和原地 JSON 修改都会使作用域失效。真实 PostgreSQL 与假 SDK 三轮演练从超过 600 秒未完成，变为约 90 秒完成调查、约 100 秒连同跨租约恢复；模型和工具次数不变，并为耗时、SQL 和安全边界保留回归测试。
