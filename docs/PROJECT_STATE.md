# OpsSwarm 新窗口交接

更新：2026-10-30。第138步已验收，随切片记录独立提交；提交身份以Git核对。

## 当前事实

- 仓库固定为 `D:\PyCharm\job-project\ops-swarm`，使用仓库`.venv/Scripts/`。最新功能为第138步，本片基线main的ff74710；继续前核对Git，唯一迁移head为`0093_reflow_candidate`。
- 用户原有未跟踪目录`output/`未读取、修改或提交。验证使用隔离PostgreSQL与假模型，不升级用户Compose、不连接生产。
- MVP已有演练闭环；完整工程版独立回流链已从131准入推进到137路由，本片实现138候选评估受限消费。v1/v2及v3 graph-v15历史路径保留。
- 138只消费137的candidate_evaluation，来源精确绑定137正文与135/136完整新依据，保留人工约束、反证、未解问题和请求/实际观测窗口。新证据支持经纯规则和确定性模板评估，结果仅供后续规划审查，不证明根因或赋予执行资格。
- 独立任务、有限租约尝试及结果进入持久层；新调用追加原账本，原次数、Token、费用、unknown和总截止不重置。成功调用跨尝试复用仍检查当前来源。原父run仍settled并持槽，旧图、旧候选和旧批准不变，没有创建新计划、审批或动作。
- 可信只读API及显式Worker CLI已接入，不加入默认Compose。实现与验证以[138记录](progress/138-reinvestigation-candidate.md)为准；早期137的时间守卫现象保留在[137记录](progress/137-reinvestigation-routing.md)，不为交接重跑旧全量或放宽守卫。

## 下一步：第139步

核对**回流新候选的可信规划准入**，先确认原规划模块的复用边界，再确定最小切片：精确绑定138结果及137、135/136新依据；保留人工约束、未解问题和观测范围；不得沿用旧批准，原累计预算与期限继续有效。候选可行不等于可执行，后续计划、审批和处置必须分别绑定新依据。

follow_up实际分派、预算追加、held/unknown人工处理、终端取消、多步骤、前端与完整发布仍待后续，不能描述为已有能力。

## 最小阅读与代码入口

先读[AGENTS](../AGENTS.md)、本文、[进度索引](progress/README.md)当前位置与[138设计](architecture/138-reinvestigation-candidate.md)，其他资料按本片依赖定向补读。

| 入口 | 用途 |
| --- | --- |
| [138 policy](../src/opsswarm/investigation/reinvestigation_candidate/policy.py)、[context](../src/opsswarm/investigation/reinvestigation_candidate/context.py) | 新证据闭包、纯模板复用、完整上下文及原预算 |
| [source](../src/opsswarm/investigation/reinvestigation_candidate/source.py)、[job_store](../src/opsswarm/investigation/reinvestigation_candidate/job_store.py)、[ledger](../src/opsswarm/investigation/reinvestigation_candidate/ledger.py) | 精确137/135/136来源、独立租约与成功调用恢复复用 |
| [0093迁移](../alembic/versions/0093_reinvestigation_candidate.py)、[API测试](../tests/api/test_reinvestigation_candidate_api.py) | 原生来源/输入/预算/提交守卫及代表性验收链 |
| [原规划模块](../src/opsswarm/planning/candidate/store.py)、[117设计](architecture/117-candidate-plan-admission.md) | 下一片先核对兼容性，不能伪装成旧候选来源 |

总方向见[64路线](architecture/64-full-multi-agent-roadmap.md)、[65设计](architecture/65-agent-system-design.md)；数据库/API变更同步[04](architecture/04-database-design.md)/[05](architecture/05-api-design.md)。旧131～137冻结函数及图恢复语义保持。

## 开发与验证偏好

遵循AGENTS按风险验证：受影响单元、必要真实PG/API和代表性链；通过后不因文档更新重跑，失败精准定位复验，长链串行。日志留忽略缓存，进度一般几百字，交接只留当前事实和下一步。子任务按职责最少分配，主Agent统一核验及提交；不读取或提交`output/`。
