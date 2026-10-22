# 113 - 可信人工重评输入准备与就绪诊断

## 目标

第 112 步已保存可信人工补充，但工程师还不能核对这些材料将进入哪份重评输入，也不知道原预算为何不允许继续。本片通过独立持久任务和 Worker 实际生成完整输入、材料清单及预算就绪诊断。

基线为 main `a8b5420`，开始时仅有未跟踪用户目录 `output/`，未读取或提交该目录。主 Agent 完成边界、实现、集成与验收；一个子 Agent 先只读核查租约/预算耦合，再独占迁移、映射及对应测试和纯单元测试，最后独立审查。子 Agent 未单独提交。

## 设计与边界

设计见 [人工输入准备](../architecture/113-human-input-preparation.md)，同步第 4/5/64/65 篇。交接原计划包含恢复执行；实际核查发现调用账本、调用幂等身份、上下文校验均绑定旧 running 父租约和既有三轮角色，不能重用 handed_off 尝试或把已 END 的 checkpoint 当 interrupt。本片先交付可运行的输入准备闭环；实际模型重评须为原共享账本增加明确的新阶段执行归属，不以准备排队代替已经恢复的表述。

| 对象 | 本片行为 |
| --- | --- |
| 原 run/work/attempt/图与 checkpoint | 保持 await_human/completed/handed_off 与原版本，不重跑、不重新开始 |
| 准备任务 | 每等待版本唯一；稳定任务身份，queued/running/succeeded/failed，最多三次独立租约尝试 |
| 准备输入 | 绑定原交接和全部人工事件前缀；使用最高终端轮次真实成功审查的材料、Schema 与来源 |
| 预算 | 从原账本、started_at/deadline 和模型策略诊断；不预留、不追加、不清零，不产生模型费用 |
| 成功工件 | 完整待重评 ModelInput、材料清单、分配估算或显式输入阻断，以及原预算快照和诊断 |

可信 prepare 命令包含精确 handoff_id/source_sha256、等待版本、幂等键和原因；仅尚未取消且至少有一次人工补充可创建，每运行最多 15 个准备任务。同主体同键完整规范命令重放原任务，即使后来补充或取消也可查询历史。新业务错误 404/409 与准入分开审计，原始有界正文绑定授权散列。当前可用性与历史工件分开：新补充或取消得到 stale；原 deadline 经过后得到 blocked，历史工件不改写。

新系统提示替换原角色提示，原审查 user 材料完整进入 original_investigation，输出 Schema 沿用原审查；原调用完整输入/清单保存在来源快照并可按 call ID 追溯。人工陈述/约束/问题保留主体、事件、提交和声明观察时间，不变为工具证据。必要材料超窗时阻断，不裁掉约束后宣称 ready。ready 只是当时诊断，不是预留或调用授权。

## 实际改动

- 新增 human_prepare 契约、准入核验、完整来源重建、纯输入组装/预算诊断、事务命令服务、查询、独立任务仓储、Worker 和 CLI；按职责分离。
- 新增可信 POST /human-wait/preparations、GET /preparations/{job_id}、GET /preparations/commands，包含当前任务/尝试、完整工件、历史及当前诊断；默认兼容模式也必须 Bearer。
- 提取原预算查询的事务内 read_budget_snapshot 和人工等待的锁/来源端口，旧公开接口和测试 monkeypatch 入口兼容；不新增第二套调用账本。
- 准备任务统一原 work→job 锁序，领取使用 SKIP LOCKED，最多收敛八个候选。租约默认 600 秒；过期旧尝试记 lease_expired，新尝试保留稳定任务身份。成功工件与任务/尝试原子提交，最后 flush 后再次校验原租约截止时间；失败结算同样防迟到。
- 独立源异常按不可重试业务失败结算；临时基础设施错误最多三次尝试。新补充/取消后的结果不能发布。历史来源按精确事件前缀核验，原始授权正文及预算实际前缀仍重建，不假定可变表不可变。
- 迁移 0069 增加准备任务、尝试、工件和命令审计四表，固定来源/身份、不可变终态/工件/审计、原始正文授权及延迟成功闭环；有事实拒绝降级，旧迁移/运行生命周期不改。既有测试仅更新正式 head 断言。
- Compose 增加 human-preparation-worker，只有业务数据库配置，没有模型密钥、checkpoint 或环境写工具；README 提供可信命令、查询及 CLI 调用方法。

## 测试方法与结果

使用仓库 .venv、正式迁移建立的真实 PostgreSQL 17.6 和独立 checkpoint 数据库，调查模型为真实 SDK 假传输。准备 Worker 不装配模型。第一次集成测试因 Docker 引擎未运行而 setup 失败，启动本地 Docker Desktop 后重跑；不将该错误记为功能通过。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit tests/api --tb=short
$migrationTests = @(rg --files tests/integration -g '*migration*.py')
.venv/Scripts/python.exe -m pytest @migrationTests --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_call_budget_store.py tests/integration/test_model_budget_store.py tests/integration/test_budgeted_call_runner.py tests/integration/test_budgeted_graph.py --tb=short
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_human_preparation.py --tb=short
.venv/Scripts/python.exe -m pytest tests/api/test_human_preparation_api.py -x --tb=short
.venv/Scripts/python.exe -m pytest tests/api/test_human_preparation_api.py::test_preparation_attempt_limit_stops_repeated_infrastructure_failure tests/api/test_human_preparation_api.py::test_preparation_auth_expiry_after_writes_rolls_back_job_and_audit tests/api/test_human_preparation_api.py::test_preparation_uses_latest_third_round_source_without_new_model_calls -x --tb=short
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m opsswarm.investigation.human_prepare.main --help
.venv/Scripts/python.exe -m alembic heads
docker compose --env-file .env.example config --quiet
git diff --check
```

本轮最终验收通过，去重共 **1,868 项 Python 测试**。结果如下：

| 验证范围 | 实际结果 |
| --- | --- |
| 全量单元与 API | 1,577 项通过，946.49 秒 |
| 全部正式迁移测试 | 264 项通过，307.99 秒，包含本片 13 项及 metadata/空库往返 |
| 原共享预算仓储、模型预算、运行器及图 | 25 项通过，69.65 秒 |
| 新准备纯规则单独复跑 | 32 项通过，主 Agent 复核 5.30 秒；其中两个永久来源异常分类用例在全量收集后加入，额外计入去重总数，其余不重复计数 |
| 新 API/Worker 分组复跑 | 最初 11 项通过，201.38 秒；后加 3 项通过，184.32 秒；均已包含全量结果 |
| Ruff / 格式 / mypy | 静态检查通过，685 个文件格式一致，381 个源文件类型检查通过 |
| CLI / 迁移入口 / Compose | help、唯一 head `0069_human_input_preparation`、Compose config 均通过 |
| 文档与 Git | 五个入口文档本地链接存在，完整差异与 `git diff --check` 通过 |

本片迁移 13 项也曾单独通过（33.51 秒），不重复计数。初次人工过期用例错误地以 BASE_TIME 推算 deadline，已改为原账本实际 deadline+1 并验证通过，未改变运行时限规则。

覆盖可信身份/兼容模式、正文冒名、原始 JSON 表示重放、版本/幂等/同版本创建竞争、404/409 审计和分页、完整材料与来源散列、预算/窗口不足和未知用量、原状态及账本不变、并发领取、租约接管、三次停止、迟到结果、取消/补充竞争、写后期限回滚、不可变守卫与授权篡改。第三轮最大轮次路径在有界测试内生成正确最高轮来源，准备前后原预算仍为 13 次模型预留。

## 限制与下一步

本片没有向模型发送人工材料、没有重评结论或实际恢复执行，也没有预算追加、图内 interrupt/resume、候选执行或新页面。仅支持当前冻结 v3 graph-v15 的人工等待，不能当作现场或生产证据。超限输入保留完整来源与阻断原因，不保证每份材料在原 24,000 输入上限内可调用。原 deadline 经过后必须拒绝模型调用；准备可诊断过期，不延长时限。

仅在 Testcontainers 执行迁移与端到端验证，未构建/启动用户 Compose 服务或升级用户业务数据库。完整发布、真实模型与全部历史动态图慢速恢复未重跑。

下一片 114：复用准备输入的有界模型重评与共享账本阶段归属。先为独立执行任务/尝试设计原账本的租约入口、固定模型操作和上下文/结果版本，调用前重新校验当前等待及全部累计额度；成功结果独立保存，不覆盖旧假设、审查或路由，不继承历史审批。

## 面试表达

“我先把人工恢复前的输入做成可审计工件。工程师提交精确等待版本，独立 Worker 重建最高轮的真实调查材料，并完整保留人工约束和来源；预算不足会明确阻断。历史工件与当前适用性分开，补充或取消不会改写历史，也不能让迟到结果推进流程。准备就绪不代表调用授权，下一阶段仍必须在原共享账本里预留和累计费用。”
