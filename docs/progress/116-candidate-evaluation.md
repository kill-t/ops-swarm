# 116 - 候选评估意图的受限消费

## 目标

第 115 步仅记录未分派意图，原 await_candidate 也没有实际消费端。本片让可信工程师显式发起独立候选评估，通过真实结构化模型调用与确定性白名单构建候选；保留开放假设、证据来源和全部失败边界。

## 设计与边界

依据 [专项设计](../architecture/116-candidate-evaluation.md)，基线 main 的 `b83312d`（第 115 步）。两种来源分别绑定完整终端交接或当前人工候选路由散列；人工来源要求精确等待版本且没有未解除 constraint。完整历史仍保留，取消、补充或 Incident 迁出 investigating 后另投影 stale。

旧规划入口依赖固定 RootCause 和 SUPPORTED，不能用来给开放假设伪造因果事实。本片只抽取其确定性步骤构建，模型选择 rollback_release、rollback_configuration、restore_dependency 或 unsupported，不提交自由参数、风险、权限或任意命令。候选必须引用当前审查接受的支持；参数也只来自显式引用证据。有效参数形成 planning_review，否则 await_human；所有产物 execution_eligible=false，不创建 ActionPlan、审批、操作、新轮次或预算追加。

独立持久任务、最多三次执行尝试及真实 candidate_attempt_id 使用原共享账本和 deadline。work→job→Incident 锁防止来源/预算/租约竞争，实际模型请求在事务外有界执行；成功调用可跨尝试复用，原子保存候选和成功状态。父 work、handed_off 尝试、run 和已 END 的 graph-v15 不重开，原父 ID 仅保留来源含义。

## 实际改动

- candidate 模块按 DTO、模型、输入组装、纯评估、可信命令、来源重建、查询、任务/账本、Worker 和 CLI 分离。冻结完整调查 JSON、当前审查、人工前缀和能力目录，记录专用上下文清单；不裁剪必要材料、不提供工具。
- 迁移 0072_candidate_evaluation 新增任务/尝试/结果/命令四表和原账本新归属，原生核验实际来源链、完整输入与分配、累计预算、安全步骤、审计及成功闭环；事实不可改写，有事实拒绝降级。旧预算入口和父收尾拒绝借用新执行身份；终端来源只豁免精确归属的在途调用。
- 可信 POST /api/v1/investigations/{run_id}/candidate-evaluations，以及 GET /{job_id}、/commands。完整原始正文、规范命令、主体和资源绑定同源授权，精确重放与业务拒绝审计独立保存。GET 不写入。
- planning/steps.py 提取现有模板，旧 planner 的 RootCause/SUPPORTED 行为兼容；新增独立候选 CLI 和 Compose 可选 profile，原环境未部署或迁移。
- 主 Agent 完成范围设计、整合、API 与统一验收；两个边界明确的开发子任务分别负责领域/SDK/Worker和数据库护栏/迁移，随后独立只读核查主任务来源链。主 Agent 重放并补齐历史用量校验，不把未核实报告计为验收。

## 测试方法与结果

本片验收通过，相关 Python 测试最终去重 **1,742 项通过**：全单元 1,401、迁移相关 319、新候选 API/Worker 18、旧重评/路由兼容 4。重复专项复核不叠加数量，未选测试不宣称通过。

| 验证范围 | 实际结果 |
| --- | --- |
| 全单元 | 1,401 通过，52.09 秒；含新增领域、SDK、Worker 及历史用量 68 项 |
| 全迁移筛选组 | 304 通过、433 未选，357.95 秒；新增正文绑定前的完整兼容回归 |
| 最终迁移与正文规范化 API | 39 通过，57.87 秒；38 项原生迁移（含前组 23 项）+1 项新 API；迁移去重 319 项 |
| 新候选 API/Worker 全组 | 17 通过，429.25 秒；额外正文规范化 API 见上一行，新 API 合计 18 项 |
| 用量/事故变化/原子失租与旧接口兼容 | 9 通过，298.63 秒；其中 5 项含于候选全组，旧 SDK 重评 1 项与原路由 3 项不重复计数 |
| 静态、配置与入口 | Ruff 通过、737 文件格式一致、mypy 419 源文件通过；唯一 head 0072；Compose profile 配置和候选 CLI help 通过 |

覆盖两种实际持久来源与完整 SDK 输入、白名单步骤的真实成功闭环、显式引用参数、unsupported、未知用量、伪造证据、超用量、可信权限/原始正文/完整命令、并发幂等创建、错误结果审计、取消/补充/事故关闭、原 deadline、租约到期、模型成功复用、真实 result flush 后失租的事务回滚、不可变工件、原生安全步骤和有事实拒绝降级。当前适用不表示还有预算或执行资格。

最终 Git 差异、暂存范围、文档本地链接与 git diff --check 均在提交前核对；未包含 output/。

```powershell
.venv/Scripts/pytest.exe tests/unit -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration -k migration -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_evaluation_api.py -o addopts='' -q
.venv/Scripts/pytest.exe tests/integration/test_candidate_evaluation_migration.py tests/api/test_candidate_evaluation_api.py::test_canonical_command_accepts_default_action_unicode_trim_and_uuid_forms -o addopts='' -q
.venv/Scripts/pytest.exe tests/api/test_candidate_evaluation_api.py::test_post_call_guards_and_unknown_usage_remain_conservative tests/api/test_candidate_evaluation_api.py::test_result_flush_then_lease_expiry_rolls_back_and_reuses_model_success tests/api/test_human_reassessment_api.py::test_reassessment_sdk_consumes_exact_input_and_preserves_old_work_and_facts tests/api/test_human_routing_api.py::test_success_persists_exact_intent_without_dispatch -o addopts='' -q
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/alembic.exe heads
docker compose --profile candidate-evaluation config --quiet
git diff --check
```

主任务最后复核还补齐了原生 raw_body→规范命令完整绑定；拒绝不同合法正文与命令交叉配对，同时兼容真实 Pydantic 默认 action、Unicode 空白裁剪及 UUID 表示，严格拒绝浮点/字符串/布尔等待版本。

迭代中修正迁移 SQL JSON 字面量冒号被误识别成绑定参数、人工事件表行与组合 DTO 的结构差异、旧测试 head 断言，以及测试中把字典直接传给模型散列函数、误用执行表名和关闭事故时漏填必需时间戳的问题。独立核查发现历史调用未解析实际用量，现已拒绝非法/超分配用量并保留 None 未知。失败或中断迭代不计为最终通过。

## 限制与下一步

结果为候选评估，能力可行不等于根因已证明，也不提供规划或执行授权。第 115 步原始 not_dispatched 意图保持历史，由新任务消费并追溯；未实现补查意图实际分派、可审计预算追加、人工约束解除或图内 interrupt/resume。

下一片 117：候选评估结果的可信规划准入。独立设计候选版本、证据与当前适用性绑定，衔接现有规划/审批时保持开放假设与旧路径兼容，不沿用历史审批。仍使用隔离本地 Compose 演练环境。

数据库验证使用正式迁移的 Testcontainers PostgreSQL 17.6，模型为 SDK 假传输或假端口，不是现场、生产或付费模型验收。没有升级用户业务数据库，没有前端改动；output/ 未读取、未提交。

## 面试表达

“我把模型提出能力与系统决定能否使用这项能力分开。候选绑定原审查和明确引用的工具证据，代码从证据推导参数、风险和验证规则；模型不能提供任意参数或执行授权。独立 Worker 使用原预算与新的真实尝试身份，重试可以复用成功请求；输入变化、过期或失租都不能发布。历史开放假设保留，候选需要后续可信规划和新审批。”
