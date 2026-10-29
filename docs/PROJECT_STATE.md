# OpsSwarm 新窗口交接

更新：2026-10-29。本轮仅整理交接和开发约定，未开发第138步。

## 当前事实

- 仓库固定为 `D:\PyCharm\job-project\ops-swarm`。最新功能提交为 `0de0c3e 新增回流审查的确定性路由`（main），迁移唯一head为 `0092_reflow_routing`；继续前再核对Git。
- 整理交接前，工作区仅有用户原有未跟踪目录 `output/`，未读取或修改，不提交。使用仓库 `.venv/Scripts/`；数据库验收用隔离PostgreSQL，未升级用户Compose或连接真实模型/生产。
- MVP已有演练闭环；完整工程版仍在推进。v1/v2和v3 graph-v15历史路径保留；独立回流链已完成131准入→132协调→133分派→134专业执行→135综合→136独立审查→137确定性路由。
- 137只保存不可变 `not_dispatched` 意图：`candidate_evaluation`、`follow_up`或`await_human`，尚未创建新候选、计划、审批、动作或后续补查任务。原父run仍settled并持槽，旧图不重开；135的pending_review、136的recorded及旧批准均保持。
- 137关键验证和静态检查已通过，详见[验收记录](progress/137-reinvestigation-routing.md)。旧长链曾在134前置时间守卫失败，最后串行复验通过；测得主机/隔离PG约0.6秒时差，唯一原因未证明。不要为交接重跑旧全量，也不要放宽守卫。

## 下一步：第138步

**受限消费回流候选评估意图**，先核对现有候选模块能复用的边界，再确定最小可运行切片。

1. 仅消费137的candidate_evaluation，绑定精确137路由及135/136完整新依据；不借用旧候选来源或旧批准。
2. 保留人工约束、反证、综合未解问题、请求/实际观测范围。支持引用限于同源新证据，未知假设不能强行映射已知根因。
3. 新阶段的真实尝试、调用、幂等及恢复继续使用原运行账本；所有角色/重试/unknown累计，原次数、Token、费用和总截止不能清零。来源失效或迟到不发布，成功调用复用仍需来源核验。
4. 本片先完成候选评估的受限消费；新计划、审批及处置另行绑定。follow_up分派、预算追加、held/unknown人工处理、终端取消、多步骤、前端与完整发布仍待后续实现。

这些是下一片目标，不是已实现事实；若实际代码显示需调整范围，先更新专项设计。

## 最小阅读与代码入口

先读[AGENTS](../AGENTS.md)、本文、[进度索引当前位置](progress/README.md)及[137设计](architecture/137-reinvestigation-routing.md)；其他材料按依赖定向补读，不串行重读全部历史。

| 入口 | 用途 |
| --- | --- |
| [137 policy](../src/opsswarm/investigation/reinvestigation_routing/policy.py)、[source](../src/opsswarm/investigation/reinvestigation_routing/source.py)、[queries](../src/opsswarm/investigation/reinvestigation_routing/queries.py)、[迁移](../alembic/versions/0092_reinvestigation_routing.py) | 纯规则、精确来源、历史/当前诊断及原生保护 |
| [136 contracts](../src/opsswarm/investigation/reinvestigation_review/contracts.py)、[queries](../src/opsswarm/investigation/reinvestigation_review/queries.py)、[ledger](../src/opsswarm/investigation/reinvestigation_review/ledger.py) | 成功意见、完整来源与独立阶段的原账本模式 |
| [原候选 contracts](../src/opsswarm/investigation/candidate/contracts.py)、[source](../src/opsswarm/investigation/candidate/source.py)、[evaluation](../src/opsswarm/investigation/candidate/evaluation.py)、[job_store](../src/opsswarm/investigation/candidate/job_store.py) | 既有候选流程；先判断兼容性，避免复制整个模块 |
| [137 API测试](../tests/api/test_reinvestigation_routing_api.py)、[领域测试](../tests/unit/investigation/test_reinvestigation_routing.py) | 可复用的来源与边界验收入口；避免每个断言重复搭建旧长链 |

总方向见[64路线](architecture/64-full-multi-agent-roadmap.md)、[65设计](architecture/65-agent-system-design.md)；数据库/API变更更新[04](architecture/04-database-design.md)/[05](architecture/05-api-design.md)对应事实。旧131～137历史函数和图恢复语义不得静默改写。

## 开发与验证偏好

用户最新要求：把Token用于业务逻辑和工程质量，减少重复测试、长输出和过细文档。具体执行规则已写入AGENTS与进度规范：

- 每片先按风险选择受影响单元、真实PG集成及少量代表性端到端；预算/权限/事务/恢复等边界不能省。共享基础改动或发布时再扩大回归，不默认全量。
- 检查通过后不因文档修改重跑；失败先定位，只复验相关范围。长集成链默认串行，报告留缓存，对话只报必要结论。
- 进度一般几百字，交接只更新当前事实和下一步，架构细节链接引用；不用历史测试总量或详尽耗时证明进度。
- 子智能体仅在边界清晰且能独立推进时使用最少数量；主Agent整合核验并独立提交，不为分工重复检索和测试。

## 新窗口可直接使用的指令

> 请按AGENTS和docs/PROJECT_STATE.md继续第138步。先核对Git、137路由及原候选模块，完成最小设计、实现、按风险验证、短进度记录和独立中文提交。原预算、精确来源、权限和恢复护栏保持；减少重复读取、重复长链测试和文档复制，不默认重跑全量。output/不读取或提交。
