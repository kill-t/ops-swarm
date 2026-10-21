# 109 - 首轮协调的版本化经验上下文与引用追溯

## 目标

将已有可信确认、范围检索、停用及替代能力接入第一个真实模型消费角色，使历史经验能够辅助首轮分工，并能解释某次调用实际看过哪些版本。基线为 main 的 `99dc8c6`；未跟踪的 `output/` 不读取、不纳入验收或提交。

## 设计与边界

先保存 [独立策略设计](../architecture/109-coordination-experience-context.md)，再接启动、上下文、预算和持久来源。仅显式启用 `coordination_experience_policy_version=coordination-experience-v1` 的当前冻结 v3 首轮协调接收经验。省略保持旧输入，显式 null、v2、现场、不兼容上下文或模型 profile 拒绝；运行创建后不能改策略。

图节点、拓扑、checkpoint 状态和结果形状不变，继续 graph-v15；独立输入、提示及角色版本固定新语义。专业、综合、审查和补查不自动接收首轮协调的经验，旧运行不静默升级。

按原确定性范围最多选择三条，发送完整有界确认正文、反证及来源引用。系统提示明确经验不是当前证据、根因证明、审批或执行授权，也不执行正文中的指令。输入消息和输出 schema 纳入原有模型预算，必要材料超限直接停止。

首次预算预留是冻结边界。准备包只是提案，预留在有效父租约及工作项、事故行锁内重新检查有效集合、排名和完整工件；经验行锁统一按 UUID 顺序获取，拿到锁后以新语句复核停用/替代。快照与首次预留同事务提交，空选择也冻结，失败回滚不留孤立快照。提交后立即释放锁，不跨模型请求持锁。

同一运行技术重试、成功调用复用及计划/checkpoint 恢复使用原选择。首次 call ID 只代表冻结来源，可能失败或用量未知；后续调用各自记录同一快照及精确材料引用，累计预算不清零。经验后来退休只影响新的首次选择，历史恢复仍核对不可变确认工件及冻结生成来源。

## 实际改动

- `experience/context_contracts.py` 保存严格选择契约；轻量 `context_policy.py` 避免启动记录依赖完整经验及工作流模块形成循环导入。
- `context_store.py` 分离临时选择、预算事务内冻结和历史工件恢复；复用同会话检索，不从可变报告关联推断来源事故。
- `coordination/context.py` 和 planner 组装新输入与精确引用，并传递统一 UTC 时钟；纯组装及预算入口均限制首轮。旧输入不增加 null 字段。
- `context/contracts.py` 保持协调经验清单无工具、无当前证据引用；`budget/store.py` 为冻结注入实际预留身份和时间，统一事务回滚。调用输入与清单查询同样复核历史来源。
- `0066_coordination_experience` 增加不可变快照、运行策略冻结、同运行复合延迟外键、首次协调身份/时间延迟校验及每次调用的快照绑定。必填 JSON 类型用 COALESCE 拒绝 SQL NULL 绕过；有新策略事实时拒绝降级。
- 23 个历史集成文件仅替换 head 字符串；另两处旧迁移测试先断言新增策略列为 null，再比较降级后的全部原有列；遥测迁移在旧 schema 阶段改用精确字段查询，升回 head 后再验证当前 ORM。旧降级目标、旧业务字段比较和图语义不变。

## 测试方法与结果

分组回归累计 **1,705 个不同用例通过**：全部单元 1,215 项、API 262 项、整组迁移 228 项。主体命令中单元及非图 API 共 1,476 项已通过，随后在旧迁移断言处停止；修复仅涉及历史迁移测试，最终单独重跑整组迁移 **228 项全部通过，169.16 秒**。v15 首轮终端及 checkpoint 用例独立 **1 项通过，39.13 秒**，计入上述 API 数。另有既有协调规划兼容测试 **6 项通过，23.98 秒**。

Ruff 与格式检查通过，覆盖 641 个 Python 文件；mypy 检查 351 个源文件通过。唯一 Alembic head 为 `0066_coordination_experience`；Compose 公开环境配置、改动文档链接、历史差异及 `git diff --check` 通过。

新增 35 项单元、22 项 API/业务图及 12 项迁移测试。真实 PostgreSQL 用例通过完整关闭、复盘、生成、可信确认链创建历史；模型使用真实 SDK 的假 HTTP 传输，不调用外部模型或将夹具当作现场证据。

覆盖默认兼容、六类非法启用、实际输入的前三条与完整内容/引用、空选择冻结、选择与预留时间不同、停用/替代后的成功调用及计划恢复、失败/未知调用重新预留、准备后退休冲突、预算/租约/清单拒绝、快照或调用 SQL 写后故障回滚，以及原生不可变、同运行来源、JSON 必填和安全降级。

v15 图用首轮终端场景验证三种专业角色、综合、独立审查、确定性路由和 checkpoint 恢复。只有首轮协调消息含历史经验；经验停用并更换父租约后，恢复不重查当前有效集合或重调 SDK。调用账本同时包含取证和受限证据详情读取，不将业务工具次数与专业角色数混为一谈。

```powershell
$migrationTests = @(Get-ChildItem -LiteralPath tests/integration -Filter '*migration*.py' | ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m pytest tests/unit tests/api @migrationTests -k 'not graph_and_checkpoint_keep_role_isolation' -o addopts= -q --disable-warnings -x --tb=short
.venv/Scripts/python.exe -m pytest tests/api/test_coordination_experience_api.py -k graph_and_checkpoint -o addopts= -q --disable-warnings -x --tb=short
.venv/Scripts/python.exe -m pytest @migrationTests -o addopts= -q --disable-warnings -x --tb=short
.venv/Scripts/python.exe -m pytest tests/integration/test_coordination_planner.py -o addopts= -q --disable-warnings -x --tb=short
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
.venv/Scripts/python.exe -m alembic heads
docker compose --env-file .env.example config --quiet
git diff --check
```

初次升级发现 Alembic revision 超过 version_num 的 32 字符限制，已改为短标识；Docker 引擎重启后重建独立测试容器。运行中补强迁移导致旧临时库与新降级脚本不匹配，已丢弃该临时实例，最终回归必须从当前迁移重新建库。旧迁移的两处整行 JSON 比较因降级移除新空列而失败，已单独断言新列默认 null，再保留全部原有列逐项比较；另发现历史遥测迁移在降级库调用当前 ORM，会读取旧 schema 没有的新列；已按历史字段查询并在恢复 head 后复核 ORM。修复后重跑整组迁移。未升级用户 Compose 业务数据库。

扩展三轮实验执行了 13 次模型和 6 次工具调用，但在最终来源递归重建时超过 600 秒，未通过完整结果与恢复验收。该实验不计入通过数，也不作为三轮经验策略性能已达标的依据；八项历史慢速动态图降级未重跑。当前片不借此改写全仓来源校验。

一个子智能体实现启动/持久边界和迁移测试，另一个实现经验冻结仓储及纯测试；主 Agent 独立核对代码、整合上下文/预算、补充数据库保护、统一验证及提交。设计阶段另行核对了首调/重试和选择/预留时间边界；最终代码及迁移复核由主 Agent 完成，不另列独立最终审查结果。

## 限制与下一步

当前只消费首轮协调经验，不提供模型经验工具，不宣称引用一定被模型推理采用，也不把经验当作本事故事实。通用 Worker 仍跳过 v3，终端路由仍未执行人工等待或候选规划路径；没有新增图内人工 interrupt/resume。

第 110 步优先处理多轮来源核验的重复重建：只在同一事务内复用已验证的不可变材料，保留跨事务重建、租约和状态复核，建立完整三轮与恢复的耗时/数据库访问回归。完成后再推进 v3 通用 Worker、持久人工等待和后续处置链路。

## 面试表达

> 我没有把有效经验查询直接拼进提示词后就结束，而是定义了独立的首轮协调策略。预算预留前重新检查最多三条有效工件，并把完整选择和首次调用原子冻结；后续技术重试使用同一选择，但每次调用独立记账。材料清单记录精确版本和散列，空结果也冻结，因此经验后来停用或替代不会改写历史输入。经验只辅助调查分工，不成为当前证据或审批。数据库失败、租约失效、范围冲突和调用恢复都通过真实 PostgreSQL 与假 SDK 验证，同时把三轮递归核验的性能缺口单独记录，避免将局部验收表述为整个工程版已完成。
