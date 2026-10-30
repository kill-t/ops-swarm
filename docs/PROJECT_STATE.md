# OpsSwarm 新窗口交接

更新：2026-10-30。第139步已验收，随切片记录独立提交；提交身份以Git核对。

## 当前事实

- 仓库固定为 `D:\PyCharm\job-project\ops-swarm`，使用仓库`.venv/Scripts/`。本片基线为main的6af0a90；最新功能第139步，唯一迁移head为`0094_reflow_candidate_plan`，继续前再核对Git。
- 用户原有未跟踪目录`output/`未读取、修改或提交。验证使用隔离PostgreSQL和假模型，不升级用户Compose、不连接生产。
- MVP已有演练闭环；独立回流链已从131准入推进至138候选评估、139可信规划草案。v1/v2及v3 graph-v15历史路径保留，原父run仍settled并持槽，旧图不重开。
- 139由可信工程师显式绑定138完整结果、137路由及事故版本，生成独立单步骤draft；保留135完整开放假设、138前置/未解问题与137人工限制、观测窗口。纯模板不映射旧根因或假设ID，也不证明客观因果。
- 新计划与applied命令审计同事务，原生重建真实调用者/发布尝试闭包并校验完整正文、当前依据、版本、原预算/总截止及本次凭据期限。服务端凭据期限沿已有可信授权快照边界记录，不是数据库向外部凭据目录重认证。精确重放可返回历史，当前适用性独立诊断。
- 139没有新模型调用或预算预留；unknown继续保留原分配，原次数、Token、费用及截止不重置。新草案approval_eligible/execution_eligible固定false，旧批准保持，未创建新审批、动作或139 Worker。
- 验证入口和结论见[139记录](progress/139-reinvestigation-candidate-plan.md)；138恢复复用语义见[138记录](progress/138-reinvestigation-candidate.md)。按风险验证，不为交接重跑旧全量或放宽守卫。

## 下一步：第140步

核对**回流新草案的可信审批请求准入**，先确认原审批模块可复用边界，再确定最小切片。必须绑定139草案、138完整结果与137/135/136新依据，保留原预算和期限；旧批准不能覆盖新草案，不改变139不可变历史内容或把draft描述为已有授权。审批阶段自身状态迁移与输入变化分别校验，明确有效性、幂等、审计与提交失败路径。

follow_up实际分派、预算追加、held/unknown人工处理、终端取消、多步骤、前端及完整发布仍待后续，不能描述为已有能力。

## 最小阅读与代码入口

先读[AGENTS](../AGENTS.md)、本文、[进度索引](progress/README.md)当前位置与[139设计](architecture/139-reinvestigation-candidate-plan.md)，其他资料按本片依赖定向补读。

| 入口 | 用途 |
| --- | --- |
| [139 contracts](../src/opsswarm/planning/reinvestigation_candidate/contracts.py)、[policy](../src/opsswarm/planning/reinvestigation_candidate/policy.py)、[budget](../src/opsswarm/planning/reinvestigation_candidate/budget.py) | 新草案精确绑定、开放假设与原累计预算 |
| [store](../src/opsswarm/planning/reinvestigation_candidate/store.py)、[queries](../src/opsswarm/planning/reinvestigation_candidate/queries.py)、[authorization](../src/opsswarm/planning/reinvestigation_candidate/authorization.py) | 可信准入、完整命令、历史重放与当前诊断 |
| [0094迁移](../alembic/versions/0094_reinvestigation_candidate_plan.py)、[API测试](../tests/api/test_reinvestigation_candidate_plan_api.py) | 真实138闭包、原生编译、提交守卫与代表性验收链 |
| [原候选审批模块](../src/opsswarm/approval/candidate/store.py)、[118设计](architecture/118-candidate-approval-admission.md) | 下一片先核对兼容性，不伪装旧候选或借用旧批准 |

总方向见[64路线](architecture/64-full-multi-agent-roadmap.md)、[65设计](architecture/65-agent-system-design.md)；数据库/API变更同步[04](architecture/04-database-design.md)/[05](architecture/05-api-design.md)。131～138冻结SQL函数及旧图恢复语义保持。

## 开发与验证偏好

遵循AGENTS按风险验证：受影响单元、必要真实PG/API与代表性链；通过后不因文档更新重跑，失败精准定位复验，长链串行。日志留忽略缓存，进度一般几百字，交接只留当前事实和下一步。子任务按职责最少分配，主Agent统一核验及提交；不读取或提交`output/`。
