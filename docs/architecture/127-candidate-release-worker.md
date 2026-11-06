# 候选发布可信执行任务与独立Worker

## 交付边界

基线d79ddcb。复用124完整准备事实、125回执和126持久窗口，交付显式可信执行准入、独立任务/尝试、受限远端注册/执行、只读对账及不可变结果。只支持原order-service rc1→stable单步骤，不改124内容或false字段，不更新Incident、父run/work/handed_off尝试、预算、图或活动槽位；应用成功不等于恢复健康。验证与父运行收尾另片交付。

## 身份与任务

根路径为124的release-execution-request/execution-job，run/job/规范version三资源。POST create_candidate_release_execution_job仅可信executor+execute_action（legacy_local也须Bearer），正文request_id/request_content_sha256/expected_incident_version/idempotency_key/reason。GET任务含最多3个尝试和不可变结果；GET /commands查询业务审计。首次201、同主体同键同完整命令200；同准备请求唯一任务，固定uuid5(request_id,'candidate-release-execution-job:v1')。404/409业务拒绝保存审计，原事实损坏不降级成普通拒绝。

新job、attempt、command、result四表。任务原文固定精确124请求及窗口、创建时凭据ID/主体/角色/期限与绑定指纹（对配置摘要再次散列，不能保存令牌或原secret摘要），不因重领续期。远端not_before=任务创建时刻，expires_at=min(124准备期限,创建时凭据期限)，且不晚于原运行期限；领取60秒租约覆盖该至多30秒窗口。创建及每次写出前完整重建审批最高版本、原输入/Incident版本、当前累计预算/期限与白名单CAS，并检查当前配置的同凭据版本仍有效。

API使用当前进程认证配置，Worker每次写前从配置provider重新加载环境配置；环境文件/运行进程的配置更新不具有跨进程即时撤销语义。凭据ID复用、摘要轮换、主体/角色/期限改变都不沿用原任务委派。历史authorization不是永久执行令牌；对账GET不依赖旧凭据仍有效。

## 写出、恢复与失败

固定新授权→父work→candidate job→Incident→执行job锁序。领取跳过占用的父work并创建独立尝试，最多3次。Worker 按领取时在父锁下核验的阶段选择首次派发或只读恢复，执行各分支时再检查当前租约及来源。首次网络POST前在独立事务永久提交phase=dispatched及首次尝试/时刻；原远端窗口已在入队时固定。POST只注册一次，PUT只执行一次，POST后、PUT前再用新事务完整重验来源/凭据/租约。远端126以固定窗口及CAS最终拒绝迟到写入。任何重领dispatched任务仅GET，连注册POST也不重发。

网络错误、响应丢失或重验失败后只读同operation核对；有效观察精确绑定operation、完整CAS/摘要/固定窗口、来源、状态/回执关联及时间。结果保存材料与created_at：applied_current/applied_superseded只证明该历史应用及模拟效果；远端明确cancelled或prepared+expired为not_applied；prepared+ready、查询失败/冲突为unknown并停止，不换键/自动重发。尚未派发就失效为blocked。未知终态后没有自动轮询或人工重开接口，本片不宣称自动恢复全部未知结果。

派发后原审批、凭据或预算过期不阻断有效新租约GET收敛，原124历史损坏仍拒绝结算。旧租约迟到不可落结果，重领可按原操作读取。第3次崩溃到期由后续扫描把最后尝试标记lease_expired并以其关联unknown/blocked终态，避免永久running。结果、任务终态和尝试结算同事务保存；父流程始终不推进。显式任务取消入口后续提供，本片不把阻止写出或远端取消接口描述为已交付控制面取消命令。

## 验收

离线模型、真实PostgreSQL/正式迁移与实际ASGI端口验证双来源准入、原文/身份/预算、并发单胜、前后写出检查、响应丢失、四个崩溃边界及仅GET恢复、失租迟到、三次耗尽、严格远端材料和原父事实隔离。CLI提供--once及持续处理模式，默认不启用新的Compose服务；测试不升级用户Compose数据库、不连接生产或真实模型。
