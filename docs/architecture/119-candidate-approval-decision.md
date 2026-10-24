# 候选审批请求的可信决定与过期收敛

## 目标与边界

基于第118步独立不可变pending请求，新增独立approved/rejected/expired决定及审计。请求、草案JSON及两个false资格标志不更新，旧审批/执行表不消费候选决定。批准是一份可信人工判断，不证明根因或未决条件已经成立；候选执行、前端接入及父运行后续结算另片交付。父run/work/handed_off尝试、图及checkpoint均保持，拒绝/过期只把本请求绑定的Incident收敛为investigating，不宣称释放父运行活动槽位或恢复旧图。

## 接口与命令

POST /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request/decision，操作decide_candidate_approval。始终可信Bearer approver+decide_approval，服务端注入主体；engineer/executor不能代批。GET同路径及/commands只读，不触发过期写入或迁移。过期采用显式action=expire的可信幂等命令，不引入空壳定时Worker。

DecisionCommand：action为approve/reject/expire（必填），request_id UUID、plan_content_sha256、expected_incident_version严格正整数、idempotency_key和reason（1～500字），acknowledge_unresolved_conditions严格bool默认false；approve必须true，reject/expire必须false。确认只表示阅读全部原前置条件与未解问题，不能提交新步骤、条件、证据或执行授权。原始UTF-8正文不超过64KiB，与真实授权、规范命令和run/job精确资源绑定。

首次201，精确同主体同完整命令重放200；404/409拒绝单独审计。每请求最多一个稳定决定，ID=uuid5(request_id,'candidate-approval-decision:v1')；并发approve/reject/expire只能单胜，另一主体/正文/键不能覆盖。重放可读取已失效历史，不重复迁移、不续期、不重置预算。

## 状态、资格与失败路径

锁顺序authorization→work→candidate job→Incident。所有新决定先完整重建原请求、候选、草案、原创建审计与事故事件，精确匹配request ID、计划摘要及原请求迁移后的事故版本。Incident必须仍awaiting_approval且版本与请求和命令一致，不能借旧请求改变后续事故阶段。

approve要求待审批请求availability=current、未到原expires_at、原累计预算未超限且没有在途调用；执行APPROVE_PLAN，Incident变mitigating、版本+1。reject允许原输入已补充/取消而stale，但必须未到expires_at；它撤销请求，不认可失效方案，执行REJECT_PLAN回investigating。expire要求now>=expires_at，可收敛stale/取消来源，不检查剩余模型额度或延长截止，执行EXPIRE_APPROVAL回investigating。过期的approve/reject返回409且不自动替换为别的决定，调用方须显式expire。

决定阶段不套用pending阶段current：批准后原请求历史自然stale，决定按原输入、父run状态、Incident mitigating及新版本和原期限独立投影current/stale/expired；拒绝/过期为recorded，保留原结果，不表示执行资格。待审批期的补充/取消先阻止批准；批准后的mitigating仍按第118步禁止通过人工输入接口隐式撤销，本片不新增撤销批准入口。决定投影继续拒绝后续阶段/来源/事故或期限变化下沿用，批准事实本身不可变。

approve写后再次检查期限、原预算与认证，跨截止整事务回滚；reject写后检查仍未截止；expire只能在截止后，仍检查认证。所有写后异常回滚决定、事故事件/快照和业务applied审计。GET只读。原始请求与历史来源损坏拒绝，不能降低为可用批准。拒绝/过期不要求剩余额度，因为它们不消费模型或授权执行。

## 持久化与原生边界

0075_candidate_decision新增candidate_approval_decisions与candidate_decision_commands。决定保存run/job/request、原计划摘要、前后Incident版本、唯一事件游标、approver主体/授权、幂等键、规范命令摘要、内容和散列及UTC创建时间。内容版本candidate-approval-decision-content-v1，含request_id、plan_id、plan_content_sha256、candidate_job_id、source_content_sha256、wait_version、outcome、evidence_ids、preconditions、unresolved_questions，execution_eligible固定false；不能抹去反证/未解约束或混入新授权。策略candidate-approval-decision-v1。

原生校验真实来源、旧请求创建事件/可信审计、raw/canonical正文、动作对应期限、相邻版本及同事务新事件/applied闭环；决定和审计不可更新删除，绑定的新事件不可篡改，有事实拒绝降级。审批阶段自身状态不令历史重建失败；负面决定只校验历史完整性，不用实时预算容量拒绝过期收敛。原成功调用的报告用量仍须字段完整、总量一致且不超过原分配；未知用量保持未知。不能通过篡改成功调用来模拟累计预算超额，后续无业务结果的unknown审计占用与原来源完整性分开核验。stable UUID及原始完整source散列延用应用校验边界；原生独立校验实际JSON链，不重算带任意浮点的完整source文本散列。

## 验收与后续

严格契约与纯状态规则，真实PG原生与API，双来源approve、reject/expire、精确幂等/并发单胜者、权限/版本/hash/正文、输入补充取消失效、合法批准不自我失效、截止与凭据写后回滚、事件可重放与旧表隔离、不可变及安全降级；全单元与相关历史迁移/审批回归、Ruff/Mypy及diff检查。仅隔离Testcontainers和假模型，不升级用户Compose库。

下一片120围绕新批准事实建立候选受控执行准入与真实参数前置核验；不能直接拿旧ActionStep/ApprovalRequest执行，也不能把本片false字段改为执行资格。父运行收尾/终端取消和新调查活动槽位、前端展示仍须明确交付，不能把Incident收敛描述为整个工作流已结束。
