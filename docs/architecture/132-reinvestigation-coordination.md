# 独立回流协调阶段与原账本首次调用

## 目标和容量

基线2d9c1ce。独立进程消费131唯一准入，持久冻结回流输入、独立阶段/尝试/租约，并在原运行账本预留后实际调用一次结构化协调模型，保存待分派建议。原run保持settled，旧图END、checkpoint、131正文not_dispatched和活动槽位保持。本片不创建新的Round/Task、不执行专业调查或处置；新阶段状态表示实际进展，不改写131历史。

固定reinvestigation-coordination-v1及reinvestigation-coordinate-v1提示。原协调策略和已有轮次/任务必须重建；建议轮次严格为最高实际轮次+1且不超过原max_rounds，建议任务最多min(原max_tasks_per_round,原max_total_tasks-实际任务数)。已耗尽、来源变化、过期或必要输入超预算时保存明确blocked阶段，零调用，不增加原上限或预算。首个回流仍为131规定的一次，不创建新run清零额度。

## 输入与模型边界

完整来源由131历史、130/129/128/127及原候选链核验。冻结材料包含原调查完整用户材料、当前审查/反证与未解问题、全部原人工输入、候选和计划/条件、实际执行完整结果、恢复样本/结果以及130/131来源引用。材料保留种类、稳定引用、内容摘要和正文；必要内容不裁剪，超窗停止。旧失败样本为历史材料，不描述为新的当前证据。

仅调用无业务工具的结构化协调模型，输出沿用严格CoordinationProposal，再确定性检查精确下一轮、角色/任务键唯一及剩余任务容量。建议是not_dispatched，不构成业务任务、审批或执行资格。每次调用保存完整ModelInput、分配、材料清单、工具集合和空裁剪清单，供应商用量未知保持未知。

## 持久化、租约和预算

迁移文件0087_reinvestigation_coordination.py（revision为0087_reflow_coordination）新增阶段、尝试及不可变结果。阶段身份复用准入run UUID，首次准备后source/packet不可修改；status为queued/running/succeeded/blocked/failed，最多3次尝试，每次独立UUID与lease_token，父work→阶段→原candidate→Incident锁及SKIP LOCKED领取。保存blocked原因后不反复抢占前沿，过期租约结算自己的在途调用为unknown并保守占用，再建立新尝试；旧租约结果不能推进。

调用账本新增operation=coordinate_reinvestigation和reinvestigation_attempt_id，归属为该根真实回流尝试，原investigation_attempt_id仍引用原交接父尝试，仅作来源。每次模型前在原父work锁下检查并预留实际allocation，所有重试与原调用共享总次数、每任务工具上限、Token、估算费用及原deadline；不复制余额。预算前置额外容量与预留后的累计容量分开检查，自己的精确reservation不能使自己因131的“再留一次调用”门槛被拒绝，其他在途调用继续阻断。实际用量超预留只保存审计，不能发布建议。

新阶段使用reflow_budget_snapshot重建含reserved与新operation的完整账本，SQL与Python快照逐字段对照；131仍使用原历史前缀函数，不能为新阶段放宽旧准入语义。

成功调用在业务结果保存前崩溃可跨尝试复用，不重复预留；失联调用记unknown，不退还额度。调用结算后、结果写入前继续核验事故/告警、原deadline和租约，迟到响应不推进。阶段/尝试、冻结输入、调用和结果原生绑定，同事务完成、不可变历史及有数据禁降级均需验证。

## 历史与接口

原handoff只为精确operation且具备真实回流job/attempt/admission/130来源的调用增加窄归属核验；该核验不递归调用完整132来源。131历史预算按旧前缀重建，当前显示自己的在途调用仍可blocked；132单独根据真实own call区别当前适用性和累计预算，不能全局放宽旧查询或旧写权限。

GET /api/v1/investigations/{run_id}/reinvestigation-coordination及/input仅可信Bearer/read，查询不创建任务；CLI支持--once与持续运行，默认Compose不启动。准备/领取/调用由独立Worker承担，HTTP不执行长任务。旧预算输入/交互/清单查询认识新operation并重验精确来源，旧版本分支保持。

完整上下文可能超过旧默认24,000单次输入上限，此时明确blocked且零调用。正向测试在根运行创建时固定262,144上下文窗口、200,000单次输入、3,000,000累计输入与20美元估算上限，只证明配置足够时路径可执行；不修改既有运行预算，也不将其作为全部场景的推荐值。

## 验收

真实隔离PostgreSQL与假模型覆盖双来源、冻结完整输入/人工约束、轮次任务/上下文阻断、原账本真实新增预留及未知占用、并发单胜、真实租约/总截止、过期接管/迟到隔离、成功调用复用和崩溃回滚；原父及128～131保持、完整历史在途可读、伪造owner/预算/结果被拒绝。静态检查、进度文档与独立提交后才标记已验收。
