# 版本化执行条件与可信核验事实

## 目标和语义

第120步只能保留未核验阻断。本片从精确原计划生成条件目录，由可信engineer提交完整核验快照，保存连续不可变版本、来源和当前适用性。verified表示工程师判断原前置条件当前成立，或原问题已得到有依据且解除相关执行疑虑的答复；不是客观根因证明。unresolved包含无法判断、条件不成立或答复仍阻断，保留解释。not_applicable是对该原条目不适用的实质判断，不能自动等价通过；出现该值须重新审视计划。

每份内容固定approval_required=true、execution_eligible=false。全部verified为ready_for_review；任一unresolved为blocked；无unresolved但存在not_applicable为plan_revision_required。所有新版本均是新执行依据，未来审批必须精确绑定该版本ID/hash；原119决定保留原历史/阶段语义，不覆盖新核验。原request只容许一个决定，不能对它再次approve。本片不创建重新审批、执行、图恢复或父运行收尾入口，不改120规则或原false字段。

代码校验身份、结构、引用和时效，不自动判定人工解释的真伪或证明因果。verified仍是带来源的工程师判断，必须由新审批审查；两个固定授权标志严格拒绝数字0/1和字符串。

## 目录、来源和版本

目录顺序为原preconditions后原unresolved_questions，每类保留原序位和原文，不合并跨类别相同文案。condition_id为规范JSON{plan_id,plan_content_sha256,kind,position,text_sha256}的SHA256；text_sha256是原文UTF-8散列。kind为precondition/question，position从1开始，每类最多10条。客户端只提交condition_id、status、explanation和sources，不提交原文或自定义条目。

来源为有界判别联合：evidence只引用原决定保留的证据ID，仍是原调查材料且不宣称当前遥测；human_observation保存明确标识的人工陈述和带时区观察时间，归一UTC，不能写入工具证据集合；preflight绑定同批准下真实120检查ID/hash，须无来源错误且创建时未过期，不能将参数观测自动推导为自由文本已满足。非unresolved至少有一个来源；每条最多3个、不重复；每批最多3个不同预检。人工观察须在批准之后且不晚于提交，时间使用大写T/Z或带冒号偏移的ISO8601、最多六位小数，偏移不超过±15:59，UTC归一越界拒绝。每批规范正文最多24KiB（原始正文64KiB），防止完整快照与原文展开超出持久材料上限。

每个决定最多8个核验版本，首次expected_condition_version=0且previous_content_sha256=null，后续必须精确CAS最新版本及摘要；每次覆盖完整目录，顺序/ID必须一致，不接受缺漏、额外或重复条目。批次ID=uuid5(decision_id,'candidate-condition-verification:v1:'+version)。过往核验不被修改，纠错以新版本完整表达，新增版本令旧版本superseded。当前性与历史完整性分离；损坏最高版本拒绝读取/重放，不能回退旧版本。

## 接口和事务

POST /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions，操作verify_candidate_conditions；即使legacy_local也必须Bearer engineer+verify_action，executor虽有verify_action仍不能代替engineer。命令含decision_id、decision_content_sha256、expected_incident_version、expected_condition_version、previous_content_sha256、items、idempotency_key和reason。可信原始正文、规范命令、run/job资源和主体精确绑定；新建201，精确同主体完整命令重放200，同键不同正文409；404/409业务结果独立保存。GET同路径返回目录、最新版本和适用性，GET /versions/{version}读取指定历史版本及适用性，GET /commands有界分页审计，均只读且必须可信身份。

新授权→父work→candidate job→Incident统一锁顺序；历史授权不反向加锁。新版本要求原approved/current决定、精确事故mitigating新版本、原输入仍适用、原预算合法且无在途调用，全部来源真实且新鲜。核验不调用模型/网络，不预留、追加或清空原预算。期限为min(原请求expires_at,本次created_at+300秒,所有人工观察observed_at+300秒,所有预检expires_at)，创建及提交前都必须未到期限。旧核验到期可在原批准仍有效时创建新版本，不能延长原批准。

同键重放可读superseded/stale/expired历史，不重采、不续期。查询完整重建最高版本前缀，目录与版本号不因过期倒退；最新版本在来源/事故/原预算变化时stale，到原期限或核验期限expired，旧版本superseded。approval_required指未来审批覆盖新依据的要求，不代表已创建新请求。

## 持久化和验收

0077_candidate_conditions新增candidate_condition_verifications和candidate_condition_commands，唯一(decision_id,version)、authorization_id及(run,subject,key)，前驱精确绑定；内容、审计不可变，批次和applied结果同事务闭环，有事实拒绝降级。原生独立校验可信正文、目录/状态/来源形状、真实批准链、当前阶段、前驱、期限及原预算；完整source任意浮点散列、规范时间和稳定UUID由应用重建，不新增扩展。应用另对原始JSON计算实际摘要，避免数值布尔相等掩盖损坏。复用同一已核验上下文和预检详情（每版本最多3个、前缀最多8版本），不反复遍历完整原调查链。

验收覆盖三种状态、稳定完整目录、严格来源/类型/尺寸、首批与后续CAS/并发、幂等与不可变审计、当前性/替代/过期、预算/身份/写后回滚、损坏最高版本拒绝回退，以及双来源与旧执行隔离。仅隔离PostgreSQL和假模型；不重新执行无关全量发布。具体结果完成后写进度记录。下一片优先新核验依据的独立审批，再交付候选专用执行、发布版本状态/CAS与验证；父运行/前端等仍独立推进。
