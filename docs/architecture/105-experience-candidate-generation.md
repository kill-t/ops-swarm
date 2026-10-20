# 105 - 经验候选契约、来源追踪与持久生成任务

## 目标与选择

把已关闭事故的不可变复盘、证据和验证结果整理成待审核候选，交付可信请求、持久任务、独立 Worker 和查询的完整最小链路。首版固定 `report-fact-draft-v1`，采用 `generation_method=deterministic_report_projection`，只投影历史事实，不调用模型或宣称归纳出通用规律。候选只处于 `pending_review`，不参与经验检索、上下文、审批或执行。后续如接模型，应使用独立版本并增加模型输入、调用预算与实际用量审计，不能把本片算作已完成。

## 来源与边界

请求绑定精确报告 ID 和预期内容 SHA-256。只接受已关闭事故且报告冻结了成功调查、主假设、计划、成功执行与通过验证的完整链路。缺少材料返回 `source_not_ready`，不能从标题、计数或当前 latest 作战室补造事实。主假设的 supported 是历史审查结果，不是客观根因证明。

来源包使用 `experience-source-v1`：报告 ID/schema/hash、事故 ID/版本、服务、本地环境、调查数据来源模式，以及报告精确 ID 对应的报告、调查、假设关系、证据、计划、执行和验证样本。最多 20 条证据、100 个样本和 128 KiB canonical JSON；超限失败，不裁掉反证或失败样本。重算报告与证据摘要，核对引用、服务、版本、规则与统计闭包。环境固定 `local_fault_lab`；调查 frozen/live 来自持久调查且与报告一致，验证另存 verifier_name，不能据此声称来源全部为现场。

来源随任务冻结并保存 canonical hash；Worker 读取和结算时复核冻结材料及精确源记录，漂移失败关闭。原始历史内容不改写，不创建第二套当前服务信息。候选文本、引用、前置条件与限制有界；确定性投影不能新增证据、隐去反证、改写验证判据或继承历史批准。

来源沿用现有复盘的主假设及单步骤处置契约；本片不消费尚未完成处置闭环的 v3 开放假设，也不预建未来多步骤报告转换。

## 接口与身份

- `POST /api/v1/incident-reports/{report_id}/experience-generation-jobs`：只接受 `expected_report_content_hash`。服务端固定策略和范围；首次 201、重复 200，同报告同策略最多一个任务，预期摘要不符 409。
- `GET /api/v1/experience-generation-jobs/{job_id}`：返回来源、任务状态、次数、有界尝试历史与可选候选 ID，不返回租约令牌。
- `GET /api/v1/experience-candidates/{candidate_id}`：读取不可变待审核版本、来源散列及完整有界草稿。

三个入口始终要求 Bearer 可信主体，`legacy_local` 不能放行。生成任务使用已有 `manage_report` 权限，仅工程师可提交；读取使用 read 权限。新命令仍先持久授权准入，任务保存首个请求者和授权记录关联；不能由调用方提交 actor/source/状态/策略。认证和授权先于幂等读取，重复请求不替换原请求者。

## 持久化与状态

迁移 `0062_experience_candidates` 增加三表，不扩张专用于调查的 work_items 外键语义：

1. `experience_generation_jobs`：绑定报告、策略和来源快照，首个请求者/授权、queued/running/succeeded/failed、最多 3 次尝试、可领取时间、租约和错误；报告+策略唯一。
2. `experience_generation_attempts`：稳定任务下每次新的尝试身份、序号、Worker、开始/到期/完成时间、running/succeeded/failed/lease_expired 和稳定错误。公开查询不返回租约。
3. `experience_candidates`：任务唯一绑定版本 1、pending_review、来源 hash、有界草稿 JSON/hash 和生成时间。候选与任务完成在同一事务提交。

任务查询与重复提交持有任务行共享锁，避免 READ COMMITTED 下把并发领取或完成混读为来源冲突。任务身份稳定，技术重试生成新尝试；FOR UPDATE SKIP LOCKED 互斥领取。租约过期可接管，旧 token、到期结果或旧尝试不能推进任务。重试保留次数，达到上限终止；没有外部模型调用或 Token/费用指标。来源/契约错误直接终止，未分类内部错误以有限退避重试，只保存稳定安全错误码。成功后重复结算只返回同一候选，提交后进程中断不会重复创建。

来源、任务请求身份与策略、尝试身份和候选正文由数据库触发器保护不可变；状态和租约只按领域允许形状写入。存在经验任务/候选/尝试时拒绝降级，避免丢失来源与恢复事实。

## Worker 与验收

独立命令 `python -m opsswarm.experience.main --once` 处理至多一个持久任务，常驻模式有界轮询；仅连接本地业务数据库，不持有模型、MCP 或环境写工具，不参与多 Agent 图编排。任务时间统一 UTC。

验收覆盖来源闭包/篡改/超限、候选保留引用与来源区分、可信身份与 legacy 拒绝、重复与并发请求、竞争领取、过期接管、迟到结果、次数耗尽、原子提交和幂等恢复。使用假来源/固定探针及真实 PostgreSQL，不调用外部模型。验证后更新进度并独立提交；经验审核与启用后置。
