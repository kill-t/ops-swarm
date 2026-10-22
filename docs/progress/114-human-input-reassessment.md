# 114 - 可信人工输入实际重评与共享预算阶段归属

## 目标

第 113 步只生成输入，本片让可信工程师显式提交重评命令，由独立 Worker 实际消费完整输入、调用结构化模型，并保存独立意见。基线为 main `550ed59`；开始时只有用户未跟踪 `output/`，本片不读取或提交该目录。

## 设计与边界

设计先落 [第 114 步架构](../architecture/114-human-input-reassessment.md)，同步数据库/API及第 64/65 篇。重评是新阶段：稳定业务任务和独立执行尝试引用原准备工件，不把旧 handed_off 父尝试改回 running，不重跑已 END 图，不覆盖旧假设、审查、路由或审批。

原调用表新增 human_review 操作和 human_review_attempt_id；旧 investigation_attempt_id 对此操作仅表示原交接来源，新字段才是实际执行身份和租约。旧记录序列化省略空新字段，保留已保存预算工件的规范 JSON。所有阶段共享同一运行额度、started_at 和 deadline；没有预算追加或重置。

新 POST /human-wait/preparations/{preparation_id}/reassessments 要求可信工程师、完整规范命令、精确等待版本及完整 preparation_sha256。首次 201、原命令重放 200、冲突 409、未知对象 404；404/409 先落独立业务审计。状态和命令分页 GET 同样始终要求 Bearer，兼容模式不能绕过。每准备工件一个重评任务，最多三次尝试。

原 work→重评 job 锁序统一预算与状态竞争；在外部请求前重建来源并预留。模型请求在事务外执行，受原运行期限、新租约及 120 秒单请求上限约束。供应商未报告用量保持未知，返回超预留仍保存实际用量，但不发布业务意见。输入补充/取消后仅允许审计收尾，迟到结果不能推进流程；过期尝试只将自己未确认调用收敛为 unknown。成功调用与意见分开持久化，结算后提交意见前中断，新尝试可复用成功调用。

## 实际改动

- 新增 human_review 契约、可信准入、查询来源重建、确定性预算规则、命令事务、独立租约仓储、共享账本阶段入口、模型适配器、Worker 和 CLI。
- 准备 GET 增加 artifact_sha256，便于工程师直接绑定完整输入工件；原工件内容和散列不改。重评调用的完整模型输入、材料清单和交互可由现有只读接口查询；该清单不开放专业证据读取工具。
- 结构化输出沿用 HypothesisReviewProposal 和 ReviewModelResult，经 Pydantic、原假设支持引用及原轮次完整证据闭包校验。人工事件 UUID 或声明不能变为 evidence_id。
- 历史交接核验区分有真实独立归属的新阶段在途调用；初始交接仍拒绝原调查在途。旧预算预留/结算不接受新阶段操作或归属，旧父尝试收尾只处理原调查调用。
- 迁移 0070 增加任务、尝试、结果、命令四表及预算归属复合外键，保护固定来源、不可变终态、调用一次结算、成功闭环和有事实拒绝降级。旧迁移不修改，既有测试只更新正式 head 断言。
- Compose 提供需显式启用 human-reassessment profile 的 human-review-worker，无 checkpoint 或故障环境写工具。模型配置必须与原运行 profile 完全一致，SDK 重试关闭；README 提供命令及查询。
- 主 Agent 负责集成、故障修复、统一验证和提交。两个子任务分别提供原生迁移/映射与模型适配/测试；子任务中途因额度限制中断，剩余内容由主 Agent 接手并核实，不把未验证报告当作验收。

## 测试方法与结果

```powershell
.venv/Scripts/python.exe -m pytest tests/integration/test_human_reassessment_migration.py tests/api/test_human_reassessment_api.py -x --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_human_reassessment_migration.py::test_0070_metadata_and_empty_roundtrip tests/api/test_human_reassessment_api.py::test_reassessment_sdk_consumes_exact_input_and_preserves_old_work_and_facts tests/api/test_human_reassessment_api.py::test_expired_attempt_keeps_unknown_and_new_attempt_uses_same_budget tests/api/test_human_reassessment_api.py::test_result_flush_then_lease_expiry_rolls_back_business_success -x --tb=short
.venv/Scripts/python.exe -m pytest tests/unit --tb=short
.venv/Scripts/python.exe -m pytest tests/api/test_human_reassessment_api.py::test_repeated_infrastructure_failure_is_limited_to_three_attempts --tb=short
.venv/Scripts/python.exe -m pytest tests/unit tests/api/test_call_budget_api.py tests/api/test_model_budget_api.py tests/api/test_context_manifest_api.py tests/api/test_context_evidence_api.py tests/api/test_human_wait_api.py tests/api/test_human_preparation_api.py --tb=short
$migrationTests = @(rg --files tests/integration -g '*migration*.py')
.venv/Scripts/python.exe -m pytest @migrationTests tests/integration/test_call_budget_store.py tests/integration/test_model_budget_store.py tests/integration/test_budgeted_call_runner.py tests/integration/test_budgeted_graph.py tests/integration/test_dynamic_worker_handoff.py --tb=short
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m opsswarm.investigation.human_review.main --help
.venv/Scripts/python.exe -m alembic heads
docker compose --env-file .env.example --profile human-reassessment config --quiet
git diff --check
```

最终去重 **1,675 项 Python 测试通过**，包括全量单元 1,312、关联旧 API 37、全迁移/共享预算/动态交接 313、新重评 API/Worker 13。实际执行如下：

| 验证范围 | 实际结果 |
| --- | --- |
| 全量单元 | 修正旧操作分类断言后，1,312 项通过，135.63 秒 |
| 单元与关联旧 API 初次组合 | 1,347 项通过、1 项旧分类断言失败，946.20 秒；其中 37 项 API 均通过。修正断言并增加新操作容量用例后，全单元重新通过，不把初次组合写成全绿 |
| 全迁移、共享预算与动态交接 | 313 项通过，915.81 秒；包括 279 项迁移、25 项预算及 9 项动态交接 |
| 新迁移及新重评 API 初次组合 | 27 项通过，708.42 秒；15 项迁移已计入上行，12 项 API 单独计入 |
| 最终元数据与补充场景 | 4 项通过，235.79 秒，无排序警告；其中 3 项为复跑，新增意见写入后、提交前租约过期回滚为第 13 项 API，不重复计数 |
| 供应商空错误码复核 | 1 项通过，54.97 秒；已含 13 项 API，不重复计数 |
| 模型适配与准备规则复核 | 51 项通过，7.16 秒；已含全单元，不重复计数 |
| 静态与入口 | Ruff 通过，702 个文件格式一致，mypy 394 个源文件通过；CLI help、唯一 head `0070_human_input_reassessment`、Compose profile 配置通过 |

初次收集时延迟类型注解未启用，导致 NameError，修复后重跑；Docker 引擎关闭导致一次 setup 失败，启动本地 Docker Desktop 后重跑。旧单元测试将根级操作限定为三种，新增 human_review 后更新契约断言并增加容量边界，全单元复跑通过。迁移组合首次产生外键排序环警告：新 FK 实际在四表建立后通过 ALTER 添加，ORM 相应标记 use_alter=True，最终正式迁移/metadata/空库往返复核无警告。上述失败和警告保留记录，不当作通过结果。

覆盖完整 SDK 输入与输出方式、未知/非法/解析失败用量、HTTP 输入/清单/交互追溯、无专业工具权限、可信身份、幂等与创建竞争、404/409 审计、原状态和预算不变、并发预算预留、在途补充/取消、调用超时限、租约接管与旧结果拒绝、成功调用复用、三次尝试停止、伪造人工证据引用、返回超预留后的意见阻断、写后凭据/租约过期回滚、供应商 code=None 的失败收敛及不可变守卫。完整 Git 差异、`git diff --check` 和五个入口文档本地链接检查通过。原三轮来源准备回归通过，仍保持原 13 次模型账本；本片实际重评场景主要覆盖首轮终端，不将其描述为新的三轮端到端发布。

## 限制与下一步

本片是实际模型重评与持久意见，不是图内 interrupt/resume 或完整恢复执行；成功意见没有候选/处置/审批资格，不自动创建补查任务。仅接受原冻结 v3 graph-v15 的可信等待和成功准备输入，原 deadline 过期仍停止。当前可用性表示输入版本是否仍适用，不代表操作授权或原预算已获追加。

测试使用正式迁移的隔离 PostgreSQL 17.6、SDK 假传输或假模型端口；不是生产、现场或真实付费模型验收。未升级用户 Compose 数据库，未启动用户 Compose 业务服务，未重跑完整工程版发布或所有历史动态图慢速恢复。

下一片 115：重评意见的确定性后续路由与持久交接。先区分继续人工等待、补查意图和候选评估条件，继续遵守冻结轮次/任务总量、证据权限与共享预算；可审计预算追加、候选/规划及多步骤处置继续按明确边界交付。

## 面试表达

“人工补充后不是把旧图重新跑一遍。我为重评建立独立任务和执行尝试，引用完整准备工件，并在同一运行账本里原子预留。模型返回后还要检查用量和证据闭包；取消、版本变化和租约接管都不能让迟到结果推进流程。成功模型调用与业务意见分开保存，崩溃恢复能够复用成功调用，同时保留所有未知占用和历史事实。”
