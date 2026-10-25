# 新核验依据的独立可信审批

## 目标与边界

第121步的完整核验是新执行依据。本片一次交付可信engineer请求与可信approver的approve/reject/expire闭环，每核验版本一个独立不可变请求、每请求一个决定，不向旧119请求追加决定。新审批只覆盖精确核验ID/版本/hash及其原计划、原决定；verified仍是人工判断，批准不证明根因或替代当前参数/CAS核验。原核验、预检、计划、旧请求/决定及false字段保持；新内容execution_eligible固定false，实际执行另片交付。

Incident仍mitigating原版本，本片不追加状态事件，不改父run/work/handed_off尝试、图、checkpoint或预算，不宣称释放活动槽位。新核验版本即使再次全部verified，也使旧版本请求/批准superseded；不能因内容相似沿用授权。blocked和plan_revision_required不得创建请求或获批准。

## 接口与身份

根路径 /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions/versions/{version}/approval-request。version为1..8的规范单字符十进制路径，可信资源绑定包括run_id、job_id和version三个字符串，不能省略或借用其他版本的授权。

POST根路径：create_condition_approval_request，仅engineer+request_approval。命令verification_id、verification_content_sha256、expected_incident_version、idempotency_key、reason。POST /decision：decide_condition_approval，仅approver+decide_approval，命令action approve/reject/expire、request_id、request_content_sha256、expected_incident_version、acknowledge_verification（严格bool，默认false；仅approve必须true）及幂等键/理由。客户端不能改变核验内容、期限、身份或执行资格。所有接口即使legacy_local也要求Bearer。

GET根路径读取请求及适用性，GET /decision读取独立决定（已有请求但未决定为pending，无请求404），GET /commands按游标读取本版本两阶段业务审计。原始UTF-8正文最多64KiB，规范命令/真实主体/路径/资源/授权精确绑定；首次201，精确同主体完整命令重放200；404/409业务拒绝独立审计，已有事实损坏必须整事务拒绝，不能降级为新error。

## 来源、状态与期限

统一新授权→父work→candidate job→Incident锁顺序。单次完整重建原批准/计划及全部核验版本前缀，预检按事务缓存；历史授权只读。请求与approve要求目标为最新current且ready_for_review，Incident mitigating及精确版本、原输入、原期限/累计预算合法且无在途调用。请求created_at>=核验created_at，expires_at严格等于该核验expires_at；不续期原批准或核验。

请求ID=uuid5(verification_id,'condition-approval-request:v1')，决定ID=uuid5(request_id,'condition-approval-decision:v1')。请求状态固定pending；一旦有决定，请求投影recorded，不再作为待审批项。未决定请求按当前核验、原预算和期限投影current/superseded/stale/expired。approve新决定只能在原请求截止前且来源仍current；写后再次核验凭据、期限、预算/当前性，跨截止整事务回滚。批准决定按同一精确核验的当前性投影，绝不因自己的写入失效，也不产生新的资格期限。

reject只允许截止前，expire须now>=expires_at；两者可收敛superseded/stale及预算耗尽或在途调用下的旧请求，不要求当前Incident仍是旧阶段，因为它们只保存负面审批事实、不改变Incident。命令仍须绑定请求中的原事故版本/内容，完整历史损坏不能放行。负面决定投影recorded。每请求只能有一个决定，不能在已批准后再expire覆写；批准后来到期/替代按当前适用性失效。精确重放只读历史，不再次审查、续期或变更结果。

## 持久化与验收

0078_condition_approval新增condition_approval_requests、condition_approval_decisions及condition_approval_commands。请求/决定均保存原核验、原决定、原计划的精确引用摘要、操作者/授权、规范命令摘要和UTC时间；共享审计表有stage=request/decision并保存原正文、成功/重放/拒绝结果。请求及各自applied审计同事务，决定及applied审计同事务，原生延迟闭环/不可变/唯一键保护，无事实不得伪造重放，有事实拒绝降级。

原生复用assert_candidate_condition_verification(verification,FALSE,FALSE)重建历史完整前缀，不锁旧授权、不误用待追加核验模式；请求/approve另核当前状态、最新版本和原预算。应用核稳定UUID、原始JSON实际摘要和完整source浮点散列边界，原生独立核实际JSON链。未采用新图、模型或框架。

验收覆盖双来源请求/决定、三动作、替代与过期、核验追加和审批并发、身份/原始正文/三资源绑定、幂等单胜、完整性与写后回滚、原预算与负面收敛、原生不可变/同事务及旧链路隔离。仅隔离PostgreSQL和假模型，结果完成后记进度。下一片先补齐本地演练发布版本状态与CAS前置能力，再在这些精确批准事实上接入候选专用执行。现有release-500开关只能证明故障开关状态，不能证明当前部署版本或真实回滚；运行镜像元数据仍按原证明边界读取。执行任务、租约/结算、恢复验证和父运行收尾继续逐片交付。
