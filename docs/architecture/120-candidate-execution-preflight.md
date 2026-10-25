# 候选受控执行准入与当前参数前置核验

## 目标与边界

第119步的人工批准不证明自由文本前置条件成立。第120步新增可信执行者发起的只读执行前检查，独立保存拟操作身份、批准及计划摘要、当前参数观测、全部原条件和阻断原因。只允许本地演练地址和三个固定GET，不调用修改接口、旧执行器、模型或Shell，不改原草案、请求、决定或父图。当前任意自然语言条件没有版本化核验器，因此全部保留为未验证阻断；即使参数匹配也不授予执行资格。这个检查事实可解释为什么当前不能执行，不是可被未来直接消费的执行令牌。

## 命令、状态和接口

POST /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights。必须Bearer executor+execute_action，即使legacy_local也要求可信身份。命令只含decision_id、decision_content_sha256、expected_incident_version、idempotency_key、reason，不能提交当前参数、条件已满足、URL或执行权限。首次201、同主体同完整命令200；同键不同命令409，其他404/409拒绝保存独立业务审计。GET同路径有界按游标读取不可变历史，GET /commands读取结果审计，不发起参数查询。

拟操作ID=uuid5(decision_id,'candidate-operation:v1')，只标识同一精确批准下的原单步骤；检查ID=uuid5(operation_id, canonical_sha256({subject_id,idempotency_key}))，主体和键按完整摘要绑定。新键是新检查，不覆盖历史。检查一直为blocked且execution_eligible=false，真实修改、租约执行与恢复验证另片设计。GET或同键重放只返回原观测，不刷新采集时间、不续期，不宣称观测仍实时有效。

## 来源、观测与失败路径

授权→父work→candidate job→Incident锁顺序下完整重建第119步历史。新检查要求approved/current决定、精确摘要、Incident mitigating及决定新版本、原输入未补充/取消、原期限未到、原累计预算合法且无在途调用。使用同一已验证行解码计划/候选，不重复遍历来源。只读本地HTTP属于确定性操作前检查，不是调查Agent工具或模型调用；不预留或清空调查调用额度，独立观测保留实际检查记录，调用前后复核原预算/期限。

观测使用异步HTTP、固定本地ExecutionSettings、禁用环境代理和重定向，单次请求超时且响应有界。配置读取database_pool_size并严格比较原current_value；Redis读取available并比较expected_available。release-500仅有故障开关而无当前/目标版本证明，因此记录开关、明确runtime_version_unverifiable，不能把镜像摘要或enabled冒充版本比较。目标、动作、参数类型不在白名单则在任何网络访问前拒绝。读取失败/超时/无效响应保存受限错误观测和阻断，不回退冻结证据。自由文本preconditions及unresolved_questions逐项完整保留为unverified阻断，不尝试关键词匹配或让批准代替验证。

观测时间必须处于本次读取开始与报告创建之间，且不早于批准。expires_at=min(原请求expires_at, observed_at+30秒)，创建及提交前必须尚未过期；它只是历史观测的适用期限，不是新批准。写后再次检查凭据、原期限和累计预算；跨截止或凭据过期整事务回滚，不留下半个检查/applied审计。网络读取失败是可保存的检查结果，来源损坏/失效是409准入拒绝。所有报告只描述采集时事实；未来执行须重新核验并通过CAS或等价机制防止检查与修改间漂移。

## 持久化与验收

0076_candidate_preflight新增candidate_execution_preflights和candidate_preflight_commands。检查与applied命令同事务，不迁移Incident、不追加假的状态事件；外键、原生可信正文/摘要、精确批准/输入/期限和原预算守卫、不可变与延迟同事务闭环保护事实。原生核验历史避免锁定旧授权，不能调用要求原决定创建时Incident时间的插入守卫来读取后续历史。完整source任意浮点散列、稳定UUID及正文/时间规范化沿用应用重建边界，不新增UUID扩展；原生另核真实JSON来源链。

验收包括纯契约/白名单/参数漂移及未决条件、HTTP只读/有界/错误；真实PG迁移和原生命令/同事务/不可变守卫；双来源API、角色与正文绑定、幂等/并发、版本/摘要/期限/预算拒绝、写后回滚、旧执行表和父运行保持。实际结果后写进度记录。后续需要版本化结构化条件核验或可信条件解除事实，以及独立执行租约、CAS、结果和恢复验证；父运行结算、前端、图恢复和预算追加仍后置。
