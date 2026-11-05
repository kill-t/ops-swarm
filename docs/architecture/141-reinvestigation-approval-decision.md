# 回流审批请求的可信决定与过期收敛

第141步承接[140审批请求](140-reinvestigation-approval-request.md)，只增加独立人工决定。旧图、旧候选批准、139草案与140请求均保持不可变，原父run继续settled持槽；批准不代表未决条件已满足，也不提供动作执行资格。

## 命令与状态

可信approver通过`decide_approval`权限提交`approve/reject/expire`，沿用[119](119-candidate-approval-decision.md)的角色授权语义，不另加主体互斥规则。路由为`/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan/approval-request/decision`，POST操作`decide_reinvestigation_approval`，GET及`/commands`只读。命令严格包含action、request_id、plan_content_sha256、expected_incident_version、idempotency_key、reason和布尔acknowledge_unresolved_conditions；只有approve必须显式确认true，其余省略时默认false，不能确认true。客户端不能指定凭据期限、方案或执行资格。

每个请求最多一个稳定决定，id为uuid5(request_id, `reinvestigation-approval-decision:v1`)。三种动作均要求140完整历史闭包及精确awaiting_approval/请求版本；批准还要求当前依据、无新告警、原预算与无在途调用，未到期才可批准或拒绝。拒绝允许来源stale；expire必须显式在请求到期后提交。负面决定不要求剩余额度、当前路由有效或原截止尚未过去，避免无法收敛；完整历史仍继承131的父run为settled持槽不变量，释放后兼容不在本片扩展。approve令事故进入mitigating，reject/expire回investigating，版本相邻增加；均不释放持槽、不重开图、不预留模型调用。

## 持久化与守卫

独立表`reinvestigation_approval_decisions`绑定run、138任务、请求、精确草案hash、迁移前真实basis_event_cursor、迁移后事件游标与前后事故版本，并保存决定内容hash、可信准入、服务端credential_expires_at及创建时间。policy为`reinvestigation-approval-decision-v1`。内容version为`reinvestigation-approval-decision-content-v1`，包含request_id、plan_id、plan_content_sha256、outcome、完整139 `plan`内容及固定execution_eligible=false，保留开放假设、全部未决条件、窗口及证据版本。决定正文上限128KiB，为139合法64KiB草案预留外层绑定字段空间；原生精确内容校验仍拒绝额外字段和截断。命令表`reinvestigation_approval_decision_commands`记录原正文、规范命令、摘要、applied/replayed/error及决定引用。稳定UUID由应用生成并在读取时核对；原生守卫沿139/140边界保护唯一业务身份和完整事实链，不新增UUID扩展。

决定、Incident迁移、精确事件及applied审计同事务闭合。0096原生守卫独立重建完整历史与原始可信正文，拒绝孤立退出待审批、伪造前缀、自洽hash篡改和写后依据/凭据失效；迁移后异常整笔回滚。批准提交前复核原账本/期限，unknown仍按原分配计；拒绝及过期只复核动作时限、可信准入和精确阶段。可信凭据期限沿139/140的服务端快照边界，不声称数据库独立认证外部凭据目录。

## 历史与查询

批准后的current按自己的mitigating/新版本及唯一迁移事件、原父run持槽、无新告警和原截止判断，不能复用140硬编码awaiting_approval的current。批准可投影current/stale/expired；负面决定为recorded。请求与草案变成stale属于合法阶段迁移。精确主体/完整正文/幂等键重放返回原决定，不重新迁移或延长期限；历史读取不因原创建者凭据现在过期而否认当时合法事实。

## 验收

实际状态见[141进度](../progress/141-reinvestigation-approval-decision.md)：单元与迁移通过，完整API链未验收。

单元覆盖严格契约、内容保全、状态/时间边界与可信准入；隔离真实PostgreSQL覆盖元数据/升降级、原生孤立迁移/篡改/提交守卫；代表性API链覆盖可信批准、幂等和冲突、负面收敛及阶段自身有效性。沿精简策略复用合法历史准备，不重复搭建每条历史链或调用真实模型。
