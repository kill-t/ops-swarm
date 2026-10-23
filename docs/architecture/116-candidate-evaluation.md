# 候选评估意图的受限消费

## 目标与非授权产物

消费原 graph-v15 的 await_candidate 交接及第 115 步当前 candidate_evaluation 意图。可信工程师显式创建独立任务；Worker 在原共享账本预留后实际调用结构化模型，从三个白名单能力中提出单项候选或 unsupported。代码复核原证据闭包、参数来源、风险及验证规则，保存候选评估；无安全参数时保存明确阻断。所有产物 execution_eligible=false，开放假设原文不转换为 RootCause 或旧 SUPPORTED，不创建 ActionPlan、审批、操作或新调查轮次，原 run/work/handed_off 尝试和 checkpoint 保持。

## 来源、接口与持久化

POST /api/v1/investigations/{run_id}/candidate-evaluations：action=evaluate_candidate、source_kind=terminal_handoff|human_route、source_id、source_sha256（完整来源记录散列）、expected_wait_version（原候选交接为 null，人工来源为精确版本）、idempotency_key、reason。始终要求可信 Bearer/engineer/manage_incident，原始正文及完整规范命令绑定授权与主体；数据库独立规范化原始正文并逐项匹配命令，保留默认 action、Unicode 空白及 UUID 合法表示，等待版本保持严格整数。201 首次、200 完整重放、404/409 业务结果独立审计；凭据等锁后及提交前过期整体回滚。GET 同路径 /{job_id}、/commands 查询任务、不可变结果与审计，不写入。

固定 candidate-evaluation-v1。来源重建 handoff→最高轮 route→review→开放假设→成功调用/完整输入；人工来源额外核验 human route、成功重评及完整人工前缀。保留原反证、未解问题和材料版本；人工声明不是工具证据，constraint 未解除则拒绝准入。来源、输入、清单及分配冻结在任务中，查询按实际历史来源重建，当前版本、取消及 Incident 状态单独投影 stale，不改写历史事实。

迁移 0072 增加候选任务、尝试、结果、命令审计四表及原账本 candidate_attempt_id/evaluate_candidate 真实执行归属。每来源一个稳定任务、最多三次独立尝试。work→job→Incident 锁序，领取 SKIP LOCKED，租约默认 600 秒；重试不清额度。成功调用可跨尝试复用，结果和成功状态原子提交；终态/工件/审计不可变，有事实拒绝降级。

## 模型、能力与预算

模型输入为原完整调查材料、当前审查、完整人工前缀和固定能力目录，专用提示与上下文清单，不提供业务工具。proposal 只含能力标识或 unsupported、理由、证据引用及前置条件，不接受自由参数、权限或风险覆盖。引用限原证据，候选必须引用当前审查接受的至少一条支持；能力参数完全由候选明确引用的原证据的结构化属性与确定性模板构建。能力可行不等于因果已证明；未知、不适用或缺参保留 await_human/unsupported 结果，不强行选用历史根因。

候选模型调用使用原 profile、SDK 关闭重试、输出上限和供应商原始用量校验；在事务外有界执行。调用前在原 work 锁下读取全部账本、原 started_at/deadline 和实际完整输入分配，然后并发预留；未知用量保守占用。原父归属仅表示交接来源，新 candidate_attempt_id 才是执行身份。终端来源核验仅精确豁免具有完整候选归属的自身阶段调用，旧预算入口和旧调查收尾不得借用或结算新身份。模型返回、结算和发布前复核当前来源、时限及租约，取消/补充/失租后的迟到结果不得成为候选。

## 验收

纯规则及 SDK 假传输、两类真实 PostgreSQL 来源、可信/幂等/并发命令、共享预算与在途归属、未知与缺参、证据伪造、取消/过期/失租、成功调用复用、原子回滚、原生守卫和历史兼容。只在隔离演练测试库运行，不调用生产或真实付费模型。候选进入规划、审批绑定和实际处置继续独立交付。
