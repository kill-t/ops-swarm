# OpsSwarm 新窗口交接

更新：2026-10-31。第140步已验收，随切片记录独立提交；提交身份以Git核对。

## 当前事实

- 仓库固定为 `D:\PyCharm\job-project\ops-swarm`，使用仓库`.venv/Scripts/`。本片基线main的2f114cb；最新功能第140步，唯一迁移head为`0095_reflow_approval_request`，继续前再核对Git。
- 用户原有未跟踪目录`output/`未读取、修改或提交。验证用隔离PostgreSQL和假模型；本轮启动本地Docker Desktop以运行测试容器，未升级用户Compose、不连接生产。
- MVP已有演练闭环；独立回流链已从131准入推进至138候选评估、139草案、140可信审批请求。v1/v2及v3 graph-v15保留，原父run仍settled并持槽，旧图不重开。
- 140由可信工程师选择139精确草案并严格确认已阅读未决条件，创建独立pending请求；请求与Incident的investigating→awaiting_approval/相邻版本、持久事件和applied审计原子闭合，绑定迁移前实际事件前缀及唯一迁移游标。旧草案和批准保持，尚无新审批决定或动作。
- 审批阶段不套用旧investigating/current：合法迁移后139草案stale是正常历史状态，新请求按自己的精确新版本、原settled持槽、无新告警及原截止判断。GET只投影current/stale/expired，不写状态；精确重放不续期或再次迁移。
- 请求到期为min(创建+30分钟,原运行总截止)，unknown保留原分配，原次数、Token、费用和总截止不清零，无新模型调用或预留。本次凭据期限由服务端注入并在提交时复核；请求人的旧凭据现在过期不改写已提交历史，沿既有可信授权快照边界。
- 原生守卫拒绝孤立迁移、自洽篡改及写后依据失效；迁移后的异常整笔回滚。验证、早期前置问题及复现见[140记录](progress/140-reinvestigation-approval-request.md)；139事实见[139记录](progress/139-reinvestigation-candidate-plan.md)。不为交接重跑旧全量或放宽守卫。

## 下一步：第141步

核对**回流审批请求的可信决定与过期收敛**，先确认原决定模块复用边界，再确定最小切片。批准、拒绝及到期须精确绑定140请求、139草案与138/137/135/136完整新依据，校验可信身份、事故版本、幂等、预算/期限及同事务审计。阶段自身迁移与真实输入变化分开判断；旧批准不得覆盖新请求，批准也不能跳过未决执行条件或直接授予任意执行权限。

follow_up分派、预算追加、held/unknown人工处理、终端取消、多步骤、前端和完整发布仍待后续；本片没有开放原settled父run的旧等待输入命令。

## 最小阅读与代码入口

先读[AGENTS](../AGENTS.md)、本文、[进度索引](progress/README.md)当前位置与[140设计](architecture/140-reinvestigation-approval-request.md)，其他材料按依赖定向补读。

| 入口 | 用途 |
| --- | --- |
| [140 contracts](../src/opsswarm/approval/reinvestigation_candidate/contracts.py)、[policy](../src/opsswarm/approval/reinvestigation_candidate/policy.py) | 严格阅读确认、原期限与独立审批阶段 |
| [store](../src/opsswarm/approval/reinvestigation_candidate/store.py)、[queries](../src/opsswarm/approval/reinvestigation_candidate/queries.py) | 原子迁移、可信审计、历史重放及阶段诊断 |
| [0095迁移](../alembic/versions/0095_reinvestigation_approval_request.py)、[API测试](../tests/api/test_reinvestigation_approval_api.py) | 真实历史/事件前缀、孤立迁移和提交守卫、代表性验收链 |
| [原决定模块](../src/opsswarm/approval/decision/store.py)、[119设计](architecture/119-candidate-approval-decision.md) | 下一片先核对兼容性，不伪装旧候选或沿用旧批准 |

总方向见[64路线](architecture/64-full-multi-agent-roadmap.md)、[65设计](architecture/65-agent-system-design.md)；数据库/API变更同步[04](architecture/04-database-design.md)/[05](architecture/05-api-design.md)。131～139冻结SQL和旧图恢复语义保持。

## 开发与验证偏好

遵循AGENTS按风险验证：受影响单元、必要真实PG/API与代表性链；通过后不因文档更新重跑，失败精准定位复验，长链串行。日志留忽略缓存，进度一般几百字，交接只留当前事实和下一步。子任务按职责最少分配，主Agent统一核验及提交；不读取或提交`output/`。
