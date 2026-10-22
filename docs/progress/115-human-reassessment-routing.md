# 115 - 重评意见的确定性后续路由与持久交接

## 目标

第 114 步能实际消费完整人工输入并保存独立意见，但业务侧无法查询这份意见对应的后续路径。本片增加确定性、不可变的未分派意图，贯通成功重评到持久交接；不把模型意见当成根因证明或执行授权。

## 设计与边界

实现依据 [专项设计](../architecture/115-human-reassessment-routing.md)，基线为 main 的 `da93df0`（第 114 步）。原 `ReviewRoutingDecision` 对内部原因有严格校验，因此保存原规则结果 `base_decision`，由新外层叠加人工约束与预算阻断，保留完整判断依据。使用原最高轮和既有任务数，第三轮绝不产生第四轮。

人工 constraint 没有可信解除事实，不能由模型 rationale 或文本关键词自行消除；保守保持等待。预算从原共享账本读取，未知用量保留分配占用；补查要求至少 N+2 次后续模型及 N 次工具容量，候选意图至少一次模型容量。Token 和费用只检查最低必要余量，未来输入仍需实际分配与预留；意图不承诺可执行。原 deadline 不延期。

新成功结果与路由同一事务提交，原 run/work/attempt/图/checkpoint 不变。有其他阶段预留未结算时暂不冻结预算，成功意见先保留；同一 Worker 有界补齐当前成功且缺路由的结果。补齐捕获实际时刻与完整已结算账本，不伪造原重评完成时的预算。后续补充、取消或 Incident 迁出调查状态不会改写历史路由，适用性单独返回。

## 实际改动

- 新 `human_routing` 按契约、纯策略、持久写入、历史读取和有界补齐分离职责；重用原路由规则和重评来源校验。`read_job_packet` 在同事务返回已验证意见与来源，旧 `read_job` 包装保持行为，不跨事务缓存。
- 迁移 `0071_human_reassessment_routing` 新增 `investigation_human_review_routes`：每重评唯一，绑定成功意见、调用、准备来源、等待版本，保存完整决策与原预算快照。原生守卫保护来源、当前输入、原任务上限、约束、截止时间、非空最低容量及不可改写/删除；有事实拒绝降级。
- 统一 work→job→Incident 锁，补齐使用 work 的 SKIP LOCKED，最多八候选；SQL 排除非调查 Incident 和在途调用。扫描游标避免反复占据首批，单候选 savepoint 隔离已知来源拒绝，记录有限错误码，损坏来源不生成普通有效意图或终止其他重评。
- 成功提交接入路由写入；重评 Worker 先补齐历史成功结果，再领取新模型任务，无额外调度框架或路由租约表。
- 新可信只读 `GET /api/v1/investigations/{run_id}/human-wait/preparations/{preparation_id}/reassessments/{job_id}/route`，返回历史 `record`、pending/current/stale、独立当前阻断原因；GET 不写入。current 只表示输入适用。
- 数据库/API/总路线/专项入口及 README 同步；旧迁移不改，旧测试只同步当前唯一 head 的断言。主 Agent 完成设计、实现与验收，一个只读子智能体独立审查来源、锁与失败路径，关键结论由主 Agent 核实并补齐验证。

## 测试方法与结果

本片验收通过，最终去重 **1,640 项相关 Python 测试通过**：全单元 1,333、迁移筛选组 281、新路由及原重评 API/Worker 26。专项复跑不重复计数，未选场景不宣称通过。可复现结果如下：

| 验证范围 | 实际结果 |
| --- | --- |
| 全量单元 | 1,333 项通过，51.01 秒；含新增纯路由矩阵 21 项 |
| 迁移筛选组（-k migration） | 281 项通过、433 项未选，419.58 秒；含正式 metadata 与空库往返 |
| 新路由与原重评 API/Worker | 26 项通过，977.55 秒；新路由 13 项、原重评回归 13 项 |
| 路由/模型/准备规则专项复核 | 72 项通过，14.72 秒，均含全单元，不重复计数 |
| 原子回滚与原生约束重点复核 | 2 项通过，111.63 秒，含新 API，不重复计数 |
| 先变更后补齐、来源拒绝、Incident 竞争 | 4 项通过，246.83 秒，含新 API，不重复计数 |
| 最终 JSONB 容量与 metadata 复核 | 1 项通过，18.17 秒；决策上限调整为 1 MiB，正式约束与 ORM 一致，含迁移筛选组，不重复计数 |
| 静态检查 | Ruff 通过，713 个文件格式一致；mypy 401 个源文件通过，唯一 head 为 0071 |

开发中首轮纯规则夹具的输出总量不足以支持其补查预期，另有异常类型断言不匹配，已调整夹具和断言，未放宽规则。补充 SQL CASE 检查时缺少表达式括号导致临时库迁移 setup 失败；修正后迁移组合通过。新 API 夹具起初误用不存在的时钟方法，回滚用例另漏传必需 usage 参数，已修正；中断的迭代组合和重复用例不计为最终全绿或新增数量。

```powershell
.venv/Scripts/pytest.exe tests/unit -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration -k migration -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_human_reassessment_api.py tests/api/test_human_routing_api.py -o addopts='' -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
git diff --check
```

完整 Git 差异、暂存范围、文档本地链接与 git diff --check 检查通过。

覆盖候选、补查与等待意图、反证、完整人工约束、预算边界、三轮终端实际重评、原状态/账本保留、可信读权限与跨资源拒绝、截止后历史稳定、旧结果并发补齐、锁竞争、先补充/取消再补齐、路由后变更转 stale、路由写入失败原子回滚及成功调用复用、原生错配/null/负数攻击、不可变守卫、篡改读取拒绝、Incident 竞争及来源拒绝后 Worker 存活。

## 限制与下一步

所有结果均为 `not_dispatched`；没有新补查任务、候选评估、处置方案或审批资格。原图 v15 已 END，不是 interrupt/resume。原预算过期仍停止，可信版本化预算追加是独立后续能力。

数据库测试使用正式迁移的隔离 PostgreSQL 17.6，模型使用 SDK 假传输/假端口；不是现场、生产或付费模型验收。没有升级用户 Compose 业务库或改动前端。`output/` 未读取、未提交。

下一片 116：候选评估意图的受限消费。先明确原终端 await_candidate 与重评候选意图的来源边界，以独立持久任务、当前适用性和共享预算验证准入；未知假设不得强行映射到既有根因或沿用旧审批。预算追加、补查意图实际分派及图内恢复继续独立推进。

## 面试表达

“模型重评后，我没有直接按模型建议执行，而是保留原审查规则，再用原轮次、任务容量、人工约束和共享预算编译确定性后续意图。成功意见和意图原子提交，旧结果可以无模型补齐。历史决定固定在当时预算和输入版本，查询另外报告当前适用性；取消、补充、事故状态变化和并发竞争都不能把旧意见变成新执行授权。”
