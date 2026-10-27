# 候选恢复结论与父运行持久收尾

## 目标与边界

基线5a439da。独立数据库收尾进程发现128已完成验证，完整重建127执行、128样本和原候选来源，在父work锁下原子保存不可变收尾事实、run.candidate_settled进度事件及父run.status=settled/completed_at。单次事务没有网络、模型或外部副作用，不另建无业务必要的任务/尝试表；崩溃回滚后重扫，已提交后重复扫描不再写入。

settled仅表示该父运行的候选处置阶段已记录结束，不等于恢复成功或旧根因已证实。run.result/metrics保持NULL，不复用succeeded/failed；原父work及handed_off尝试、111交接、审批/计划、127/128正文及parent_advanced=false、预算和图保持。Incident维持原mitigating状态/版本/事件，本片不关闭事故、不启动回流或LangGraph恢复。活动唯一索引继续包含settled，明确保留事故活动槽位，释放另片交付。

## 结果与当前性

独立事实保留128原始outcome/reason，另给确定性后续disposition：recovered→recovery_verified；只有applied_current且business_probe_failed的not_recovered→reinvestigation_required；来源失败、执行未知、未执行或被替代效果→human_resolution_required。这些是已持久后续意图，不是已开始重新调查或已完成人工处理。收尾不刷新历史样本，也不宣称服务在收尾时仍持续健康；后续事故恢复迁移须另验时效。

创建仅消费父运行仍await_candidate或await_human的原候选路径、未取消且精确当前人工输入、最高核验版本、mitigating及原审批事故版本；不得结算被替代依据或在途调用/候选执行/恢复任务。只读历史收尾不要求旧批准仍具当前写权限，也不延长已固定预算或采样窗口。扫描前通过SQL过滤当前资格，避免最老八份永久失格材料饿死后续合格运行；父锁后再次复核并完整加载业务历史。无当前资格跳过，已选材料历史损坏整笔失败；不把损坏解释为普通业务拒绝或未知结果。

## 事实与查询

新增investigation_candidate_settlements：复用run UUID的表内稳定id、唯一run/recovery_job、candidate/version/operation、原执行/恢复结果摘要、原handoff、原run状态/updated_at、incident_version、128outcome/reason、disposition、事件游标、policy_version=candidate-settlement-v1、created_at及完整内容摘要。一个父运行最多一份，不可覆盖。原子状态和精确事件由0084_candidate_settlement的原生约束保护，缺失来源、孤立父状态/事件/事实、跨事务补齐、改写和带数据降级均拒绝。

GET /api/v1/investigations/{run_id}/candidate-settlement只返回已保存事实，未收尾返回空；沿用可信Bearer/read（包括legacy_local）。查询完整重建128。为避免129→128→116→111→129递归，111交接仅通过独立窄校验核对收尾的身份、摘要、128精确结果、事件和父状态；129读取再执行完整来源校验。旧候选/审批/执行历史可读，但当前资格不放宽。人工等待历史仍反映当时事件，不伪造取消；新人工命令拒绝重新开启settled运行，旧幂等命令可重放。

## 验收与后续

覆盖两种原候选来源、恢复/未恢复/未知分支、并发与幂等、事务崩溃回滚、完整旧历史GET、当前资格失效、可信查询、原生孤立状态/事件/内容拒绝、原128材料篡改拒绝、活动槽位保留及预算/父尝试/Incident不变。迁移metadata/空库往返、旧v1/v2路径回归及静态检查。下一片先推进明确事故状态和活动槽位的受控收敛；自动回流、人工unknown解决/取消、多步骤、前端和图恢复继续分片。
