# 可信人工输入实际重评与共享预算阶段归属

## 目标与边界

第 113 步已生成完整待重评输入，本片增加可信工程师显式重评命令、独立持久任务和 Worker，实际调用结构化模型并保存独立审查意见。只支持已有冻结 v3 graph-v15、尚未取消且精确等待版本的成功准备工件。旧调查 run/work/attempt、图、checkpoint、假设、审查和路由不改；意见不产生候选、任务或执行权限。人工材料不是工具证据。默认预算截止后拒绝调用，不延长或重置额度。

## 命令与任务

POST /api/v1/investigations/{run_id}/human-wait/preparations/{preparation_id}/reassessments，正文为 action=reassess、原 handoff_id/source_sha256、expected_wait_version、preparation_sha256（完整工件散列）、idempotency_key、reason。GET 同路径 /{job_id} 和 /commands 查询任务、尝试、结果及审计。三个入口始终要求可信 Bearer；写入只允许工程师。原始正文最多 64 KiB，完整规范命令及主体绑定幂等；每准备工件最多一个任务，旧成功命令在补充/取消后仍可重放。业务 404/409 先保存独立审计，凭据等锁后及提交前过期整体回滚。

任务固定 human-input-reassessment-v1，queued/running/succeeded/failed；最多三次独立租约尝试，稳定任务 ID，尝试独立 UUID。work→review job 是统一锁序，领取 SKIP LOCKED。模型调用在事务外并受原 deadline、租约及处理超时约束；成功调用后结果提交前崩溃，可在同任务新尝试中复用成功调用。预留后未确认返回则旧调用 unknown，不重新发送同一预留，新尝试另记调用并继续累计额度。补充或取消后的迟到响应不得发布意见，失败或租约收尾保留 unknown 占用。

## 共享账本与来源

沿用 investigation_call_reservations，不创建第二套额度。新增 human_review 操作及 human_review_attempt_id 显式实际执行归属；原 investigation_attempt_id 保留为不可变终端交接来源，不代表旧父尝试正在执行。新操作只绑定真实重评尝试、root、调用序号零，幂等键 human-review:{attempt_id}，旧调用键和角色语义不变。新字段为空时旧 DTO JSON 不增加字段，保留第 113 步历史工件散列。

每次调用前在同一原 work 锁内重建原准备来源、原始授权、精确等待版本和完整输入/清单，重新检查所有历史调用、次数、时限、Token 与费用占用，然后原子预留。预算查询包含新阶段全部占用，未知用量仍未知。独立阶段入口校验新租约，不放宽旧 CallBudgetStore 的 running 父租约规则。

交接历史核验仅将没有合法新阶段归属的 reserved 视为原调查未完成；初次交接仍禁止原调查在途调用。原调查收尾不得结算新阶段预留。准备工件按原账本前缀追溯，当前预算诊断仍读取全部账本。

## 模型与结果

完整复用准备工件 ModelInput、human-input-review-preparation-v1 提示、Schema 和无工具清单；模型配置必须匹配运行创建时的 profile，不静默更换模型或输出方式。复用 LangChain 结构化输出与供应商用量读取，SDK 重试关闭。HypothesisReviewProposal 经 Pydantic 和原假设支持引用/原轮次证据闭包校验后，作为独立结果保存；人工事件 UUID/陈述不能变成 evidence_id。成功账本与结果有精确调用来源，查询重新核验输入、分配、清单和结果，历史结果不因等待变化改写，当前适用性单独返回。

## 持久化与验收

迁移 0070 增加重评任务、尝试、结果、命令审计四表及账本真实执行归属；保护固定请求/来源、不可变终态/结果/审计、跨表成功闭环及拒绝有事实降级。旧迁移不改。验证真实 PostgreSQL、可信与幂等竞争、预算累计/过期、模型输出/伪造引用、在途取消、迟到隔离、租约恢复、成功调用复用、原生约束、旧图和第 113 步工件兼容。仅使用隔离演练和 SDK 假传输，不宣称真实模型或生产验收。

重评在同一次读取中取得已经核验的准备工件和冻结来源，供后续精确绑定。准备接口仍返回原有视图。
