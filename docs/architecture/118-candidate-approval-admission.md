# 候选草案的可信审批请求准入

## 目标与边界

在第117步不可变候选草案上，由可信工程师显式提交独立待审批请求。仅完成请求、原子事故迁移与追溯；批准、拒绝、过期收敛和执行由下一切片完成。草案内容保持draft及原资格标志，不写入旧审批/执行表，不重启父图、Worker或候选任务。

## 命令与可信身份

POST /api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request；操作create_candidate_approval_request，需要可信engineer及request_approval、manage_incident。命令action=request_approval（默认）、plan_content_sha256、expected_wait_version（必填可null）、expected_incident_version（严格正整数）、acknowledge_unresolved_conditions（严格true）、idempotency_key和reason（1～500字）。确认表示已阅读原前置条件与未解问题，不表示条件已满足或根因已被证明。客户端不能覆盖动作、证据、条件、期限或批准资格。GET同路径和/commands提供只读历史与分页审计。

首次201，精确同主体同完整命令重放200；404/409业务拒绝单独审计。可信授权与原始UTF-8正文（上限64KiB）、规范命令、实际主体及run/job资源精确绑定。凭据在锁后和提交前再次检查。

## 事实与事务

新增独立不可变candidate_approval_requests和candidate_approval_commands。每草案一个请求，ID为uuid5(plan_id,'candidate-approval-request:v1')；保存精确草案ID/内容摘要、候选/运行、原人工版本、事故迁移前后版本、Incident事件游标、可信主体/授权、规范命令摘要、创建和到期时间。状态固定pending；审批决定后续使用独立事实，不改写请求。

锁顺序authorization→work→candidate job→Incident。首次提交要求完整候选和草案历史可重建、草案current、事故investigating且版本与原草案及命令一致、原输入未变化且候选成功、无在途调用、原预算未超限。无模型或工具调用，不追加或重置预算。过期时间为min(创建时刻+30分钟,原run started_at+max_duration_seconds)，不得续期原运行。

同事务执行REQUEST_APPROVAL，把Incident迁移为awaiting_approval并递增版本，追加原有可重放Incident事件；保存新请求和applied审计。原生守卫复核来源、可信正文、时间、事件及审计闭环，并针对候选对应版本校验Incident真实OLD状态及相邻版本；请求/迁移事件/事故更新/applied审计在同事务内绑定；写后认证/截止失效回滚整个事务。事实不可更新删除，有事实拒绝降级。

## 阶段有效性与失败路径

草案创建时的current仍保持第117步语义。审批请求读取先重建完整不可变草案及候选链，再独立判定审批阶段：终端来源仍await_candidate；人工来源仍await_human且最新等待版本与请求相同、未取消；Incident仍awaiting_approval且版本为请求绑定的迁移后版本；原run截止和请求期限未到。迁移事件必须为同事故、同版本、同主体、同reason、同时间的investigating→awaiting_approval/request_approval。因此本次迁移不自我失效，真正补充、取消或后续事故变化仍使历史请求stale。超过期限返回expired；GET不写状态，不自动恢复事故。

原可信人工等待入口在investigating外，仅允许同运行、同候选审批迁移后事故版本且历史完整的awaiting_approval阶段继续补充/取消；补充可连续进行，已有请求按原等待版本失效，不能据新输入沿用审批。旧审批不满足此入口，后续批准后的mitigating仍拒绝补充。

完整重放可返回既有stale/expired历史，不续期、不再次迁移、不扩大权限。新的不同命令、摘要/等待/事故版本冲突、另一主体抢占及损坏来源拒绝；旧审批和执行入口不能消费候选请求。下一片补齐可信审批决定与过期收敛，批准也不能跳过未决执行前置条件核验。

## 验收

纯契约与阶段策略；两来源真实PostgreSQL/API、并发精确重放、权限/正文/版本冲突、输入变化/取消/截止、提交后不自我失效、历史只读、事件/SSE事实、写后过期回滚、原生篡改与降级、旧审批执行隔离；静态检查和相关历史迁移回归。仅隔离Testcontainers和假模型，不升级用户Compose库。
