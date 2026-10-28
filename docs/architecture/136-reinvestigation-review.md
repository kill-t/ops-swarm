# 独立回流审查与精确提案绑定

基线e2b912a。本片只消费135成功且不可变的综合结果，冻结完整原输入来源、专业报告/新证据、实际窗口、人工约束和精确提案；135原记录仍保留pending_review历史，136另存recorded意见。独立审查是不同阶段、角色版本、提示、真实尝试和调用，不自审复用135综合模型输出，不宣称根因客观证明。

沿用HypothesisReviewProposal的有限契约：接受的支持引用只能来自本提案原支持集合；支持与反证均须属于同轮新证据，不接受历史证据补位。同一证据不能同时作支持和反证；进入evaluate_for_routing建议须支持充分且非空，不得同时补查。follow_up须支持不足且有限补查；human_review须支持不足且无补查。支持不足必须说明反证、证据缺口或竞争解释。所有建议都不创建任务、路由、计划、审批或授权。

新增investigation_reflow_review_jobs、attempts、results与账本reflow_review_attempt_id，操作review_reinvestigation，固定策略reinvestigation-review-v1、角色reinvestigation-reviewer-v1、提示reinvestigation-review-v1。每根一次独立阶段、最多三次尝试，默认120秒租约/最多300秒；原父work锁先于新job，再锁135来源、132来源、候选及Incident。SQL先筛选135成功项，等待项不占前八条扫描名额；网络在事务外执行，HTTP只读。

SQL独立重建135完整来源/输入/成功调用/结果及尝试闭包，Python再严格校验并编译审查上下文。保存135输入摘要和结果摘要；完整必要材料超容量则阻断，不裁掉约束。调用在原账本预留，继承所有角色、重试、unknown的原次数/Token/费用和总截止，不新建run或追加预算。旧131～135历史函数不改，新当前预算函数认识136调用。

准备、领取、预留、返回和发布重验原settled持槽、事故版本、新告警、总期限与预算。失效返回仅留unknown审计和真实已知用量，迟到不发布。成功调用跨新尝试复用；失联预留转unknown并保持原分配。当前成功尝试、独立结果和任务终态同事务/同时间提交，真实数据库时钟门禁拒绝越期；材料、终态及调用不可变，有数据禁降级。历史读不要求当前资格仍有效。

可信Bearer/read GET /api/v1/investigations/{run_id}/reinvestigation-review与/input，以及新调用的三类材料接口。Worker显式--once/--continuous，不加入默认Compose，不装配业务工具。下一片137才消费审查意见作确定性路由，原批准不能覆盖新依据或新动作。验收使用隔离PostgreSQL、假模型与固定HTTP，覆盖语义/引用边界、双来源、预算、并发、崩溃复用、失租/新告警、原生来源与不可变保护；实际通过结果以进度记录为准。
