# 候选发布执行的可信准入与持久请求

## 切片边界与原因

第122步提供精确核验批准，第123步提供本地演练版本CAS。实际代码尚无远端按稳定命令键的只读回执查询、受限派发期限或租约fencing；外部CAS成功而响应丢失后，不能再次PUT来探测结果，因为无旧记录时该请求会实际执行。因此本片先交付可信executor准入、当前版本只读采集和不可变执行请求，状态prepared、dispatch_eligible=false，未派发且不创建空壳Worker或队列。本片不是实际发布执行，也不是委派后续权限。

目标为把完整审批依据与未来唯一操作身份、确切CAS参数绑定，允许检查/追溯准备结果。后续须补齐只读回执、派发期限/租约/迟到结果规则，再启用独立执行Worker；不得为赶进度沿用旧回滚执行器或把GET目标版本当作本操作成功证明。

## 接口、身份与来源

根路径/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions/versions/{version}/release-execution-request；version是1..8规范单字符。POST prepare_candidate_release_execution仅可信executor+execute_action，正文approval_decision_id、approval_content_sha256、expected_incident_version、idempotency_key、reason。GET读取请求/本地适用性，GET /commands按游标读取业务审计；legacy_local也要求Bearer。主体、原始正文/规范命令、run/job/version三资源精确绑定，客户端不能改动作、版本、CAS修订、身份、期限或执行资格。

当前来源复用122的load_basis/read_facts/source_availability，完整重建原批准与最高核验前缀。只接受当前有效approved核验决定，原输入/mitigating事故版本、无在途调用的原累计预算和原期限均合法。原计划必须仅有order-service的rollback_release步骤，参数精确为2026.01.01-rc1→stable，其他动作/目标/版本先拒绝，再做网络GET。

仅允许GET /__admin/simulated-release，使用已有本地地址白名单、异步总超时、64KiB响应上限、禁止压缩/重定向/环境代理。严格读取123的完整ReleaseState（simulation必须真实布尔true、source/服务/版本固定），当前版本须等于原计划rc1；现场失败不回退旧开关、冻结数据或缓存。无PUT、Shell、SQL工具或真实镜像变更。

## 持久事实、时效与重放

0080_candidate_release_request新增candidate_release_execution_requests及candidate_release_execution_commands。每个精确核验批准最多一份请求，稳定ID=uuid5(approval_decision_id,'candidate-release-request:v1')，独立operation_id=uuid5(approval_decision_id,'candidate-release-operation:v1')；未来远端键固定candidate-release-{operation_id}，不借用旧操作表或请求者的HTTP幂等键。

内容保存精确核验批准/核验/原计划引用、原唯一步骤及完整版本观测/采集时间，CAS参数由服务端按原计划及观测修订构建。源计划和审批false字段不改，新内容dispatch_eligible固定false。请求created_at为采集并重验后的UTC时间，expires_at=min(原核验/批准截止,observed_at+30秒)，不重置原预算、原运行deadline或凭据。原始正文最多64KiB；规范命令及内容有界。

锁序为新授权→父work→candidate job→Incident；锁内执行至多配置超时的只读GET，返回后重验凭据、原来源/预算及观测时间窗口。请求与applied业务审计同事务保存，写后跨期限或凭据到期整体回滚。原生复用122历史完整性与当前来源门禁，另核精确计划、正文/hash、观测/CAS及相邻时限，保护不可变、唯一性和同事务闭环。

首次201；同主体同键同完整命令200，重放不网络采集/续期/新建操作；同键异体或不同命令占用已有批准409。合法业务404/409保存错误审计；既有请求或来源损坏不能用原键或换键降级为普通拒绝。GET和重放只读历史，返回pending/current/superseded/stale/expired；current仅表示本地审批来源与观测时效，requires_dispatch_recheck=true，不能证明远端此刻仍匹配，也不能发起写入。过期后不刷新同一批准的准备事实，不能绕过原期限。

## 验收与后续

验证双来源完整准入、严格角色/正文/三资源、唯一操作及CAS、当前/过期/替代/预算、幂等与并发单胜且仅一次采集、真实123只读ASGI接入、网络上限/超时/失败无回退、写后回滚、原生不可变/同事务/损坏拒绝；原Incident、事件、父run/work/handed_off尝试、预算、图和lab版本均不变。

后续先提供远端只读操作回执与受限派发/对账协议，再将当前有效请求接入独立持久执行任务、租约、恢复和结果；实际恢复验证、父运行结算、活动槽位释放和前端仍独立交付。
