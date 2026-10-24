# 候选评估结果的可信规划准入

## 目标与来源隔离

在第116步候选评估后，可信工程师显式请求确定性生成单步骤ActionPlan草案。复用ActionPlan、ActionStep与action-risk-v3规则，保留开放假设，不映射旧RootCause/SUPPORTED。旧action_plans强绑定hypotheses；旧审批/执行没有候选来源资格，因此新草案存入独立candidate_action_plans，旧接口不能找到此ID或把它当作当前旧计划。计划真实持久化并可查询，但approval_eligible=false、execution_eligible=false；可信审批绑定在下一片独立交付。

## 准入、接口与状态

POST /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan。可信Bearer工程师同时具备plan_action和manage_incident；操作create_candidate_action_plan，资源精确绑定run_id/job_id。命令action=create_plan、candidate_result_sha256、expected_wait_version（原终端null/人工精确版本）、expected_incident_version（严格正整数）、idempotency_key、reason；不接受动作、参数、风险或自由计划正文。

每候选任务一个稳定v1草案，ID为uuid5(job_id,'candidate-action-plan:v1')。work→candidate job→Incident行锁；校验原候选成功闭环、完整来源链、当前输入/Incident investigating、结果完整散列、等待版本及事故乐观锁。仅planning_review且存在确定性step时生成草案；完整证据引用、前置条件、未解问题保留在计划内容中，不声称人工已确认。状态固定draft，不改变原候选、run/work/父尝试、Incident或checkpoint。

在原始deadline和共享预算内准入，不追加预算；无模型或外部请求，因此不新预留调用次数/Token。仍拒绝在途调用、原累计超配或超预算，写入后再次校验截止时间和凭据期限。版本变化、取消、事故关闭及截止后拒绝新计划；完整同主体/同命令重放允许读取既有历史草案，适用性按当前来源、事故版本与原截止时间单独返回current/stale。GET同路径读取，GET /action-plan/commands查询审计，GET不写入。

## 持久化与失败路径

迁移0073增加不可变candidate_action_plans和candidate_plan_commands。计划行固定候选job、结果摘要、原人工版本、事故版本、操作者及授权、完整规范命令摘要、内容和散列。内容保存ActionPlan及原开放假设ID、证据引用、前置条件和未解问题。只引用不可变事实，不复制第二套当前状态。

完整原始正文与规范命令逐请求绑定可信授权；首次201、精确重放200、404/409业务拒绝均独立审计。幂等键按run/主体作用域，另一正文或同来源的另一请求不能悄悄覆盖。计划与applied审计原子提交，原生守卫重验真实来源、参数/风险、当前版本/截止、原始正文绑定和同事务闭环；事实不可改写，有事实拒绝降级。源变化后历史内容不改写。未完成或损坏来源不降级生成可用计划。

## 验收与后续

纯构建/风险/引用测试、真实PostgreSQL元数据与降级、两个候选来源、可信权限、完整幂等/并发、hash/等待/事故版本冲突、unsupported和过期拒绝、未知用量保守占用、输入取消/补充、写后截止或认证过期回滚、原始正文与规范命令原生绑定、旧规划/审批/执行隔离及历史查询。只用隔离数据库和假模型，不升级用户Compose库。

下一片118明确候选草案的可信审批准入和有效性绑定，再接执行；本片不提供审批、处置或预算追加。

后续审批会迁移Incident状态与版本。第118步须定义审批/执行阶段的独立来源有效性规则，绑定新批准事实；不能直接套用本片仅用于草案准入的current判断，让阶段自身状态迁移失效，也不能据此跳过真实输入变化的复核。
