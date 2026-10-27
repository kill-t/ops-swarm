# 验证失败的有界回流准入与原预算继承

## 目标与边界

基线d73ce87。可信工程师显式申请130 observed_failure后的重新调查，保存唯一不可变准入请求及逐请求业务审计；本片止于not_dispatched。原settled运行继续持槽，Incident保持130 investigating及精确版本，原work/attempt/checkpoint、128/129/130与调用账本均不改。不创建新run、模型调用、调查任务或预算预留；预算继承表示后续只能使用原budget_run_id，不表示已经执行。

candidate-reinvestigation-admission-v1固定max_reflows=1、reflow_number=1；root_run_id、previous_run_id、budget_run_id均为当前来源run，明确首个回流的关系。稳定请求id复用原run UUID，避免额外UUID扩展；每根运行最多一份请求，不能用另一主体或幂等键重复消费上限。后续执行阶段须在该根下记录稳定阶段/新尝试，并在原账本预留；不能用另一个run重置deadline、轮次/任务上限、预算或旧审批。

## 状态与预算

仅完整重建130及129/128/127与原候选链后，允许outcome=observed_failure、slot_released=false且父marker为空的来源。当前Incident必须等于130 after_status=investigating及after_version；命令固定130内容摘要及该版本。若130最后状态事件之后又追加incident.alert_attached，即使告警接收时刻更早也拒绝新准入，当前读取标stale；按事件游标识别实际入库顺序，新证据处理另片提供。恢复成功、held、缺失或损坏来源拒绝，已有成功请求精确重放仍可读取历史。

请求保存完整CallBudgetSnapshot（原policy/model_budget、started_at、deadline、全部调用与未知用量）和内容摘要。原父work锁串行化预算检查与命令；不允许reserved在途调用，原账本逐项核验、总次数/每任务工具次数/累计Token/费用重算，未知仍占原预留。原deadline必须未过，实际写后及数据库提交时重新检查。准入至少保留一次协调模型调用的完整单次输入/输出上限及估算费用容量；这是准入门槛，没有授予模型调用额度，也不保证完整调查可执行。未来分派须重新构造上下文、按实际分配检查原容量并预留，原轮次/任务约束仍需定义后续阶段语义。

GET分开返回不可变record和当前availability/blocking_reason_codes：来源无请求为pending；新请求后事故版本变化为stale，deadline经过为expired，预算变化/不足也阻断。未定义新阶段归属的调用不算合法预算变化：给已完成旧任务追加在途调用仍被原交接完整性校验拒绝，本片不放宽归属。历史记录用当时完整账本前缀重建，不因后续真实调用或事故变化改写；新增调用必须晚于准入。记录不是批准、执行权限或证明当前证据仍新鲜。

## 接口、审计与持久化

POST /api/v1/investigations/{run_id}/reinvestigation-admission：可信engineer/manage_incident，正文action=request_reinvestigation、convergence_sha256、expected_incident_version、idempotency_key、reason。服务端注入主体与authorization_id，验证原始UTF-8正文hash、规范命令、路由资源、凭据及授权时刻；legacy_local也要求Bearer。新成功201，精确主体/完整命令重放200，换正文同键409，已有根请求换键或主体409。业务404/409独立落审计，来源损坏不降级为普通无请求。

GET同路径只读；GET /commands提供可信read游标审计。每authorization_id只保存一条命令结果；applied/replayed/error绑定原始正文、规范命令和主体，成功审计引用唯一请求。写后凭据或原deadline过期整事务回滚，鉴权失败不伪造业务成功审计。

0086_reinvestigation_admission新增candidate_reinvestigation_admissions及candidate_reinvestigation_commands，不修改0085历史。原生守卫重建完整130来源、原预算/调用前缀、精确准入命令与授权、当前事故及固定上限，禁止更新/删除、孤立请求、缺成功审计、重算hash的伪造预算或跨事务补齐；有数据禁止降级。锁顺序authorization→父work→run→candidate→Incident，查询共享父锁。无额外Worker或Compose服务。

## 验收

真实隔离PostgreSQL验证双来源成功/精确重放/并发单胜、跨主体/换键/正文冲突、非法来源/版本/期限/容量、未知用量保留、完整历史/当前诊断、原父与槽位/预算不变、写后崩溃/认证或期限过期回滚及原生不可变/伪造/缺审计/提交时效。单元覆盖预算边界与确定性判定，假模型固定传输。下一片接持久分派阶段与原账本实际预留，另行定义回流轮次、任务容量、上下文和独立版本，不直接复用旧graph checkpoint。
