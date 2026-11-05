# OpsSwarm 新窗口交接

更新：2026-11-05。第140步已验收；第141步实现收尾并独立提交，单元与迁移通过，完整API链保留未验收欠项。

## 当前事实

- 仓库固定为 `D:\PyCharm\job-project\ops-swarm`，使用仓库`.venv/Scripts/`。本片基线main的50d6c4b；新增迁移head为`0096_reflow_approval_decision`，继续前核对Git。
- 用户未跟踪目录`output/`未读取、修改或提交；续接时另见`tmp/`，同样未读取、修改或提交。验证用隔离PostgreSQL和假模型，未升级用户Compose、不连接生产。
- 独立回流链已从131准入推进至140可信审批请求，141新增独立人工决定。v1/v2及v3 graph-v15保持，原父run仍settled持槽，旧图不重开。
- 141由可信approver绑定140请求、139完整草案和138/137/135/136新依据，提交approve/reject/expire；决定、Incident相邻版本迁移、事件及applied审计同事务闭合。批准进mitigating，拒绝和显式到期回investigating；本片无执行动作或执行资格。
- 批准按自己的mitigating/新版本及事件判断current，原草案/请求stale是正常阶段迁移。新告警、版本变化及原截止仍阻断；精确重放只读历史、不迁移或续期。负面决定不要求现在剩余额度或原截止未到，但完整历史仍继承131的父run为settled持槽不变量；释放后兼容待后续。
- 原账本、unknown分配、Token/费用/次数和原截止不重置，无新模型调用或预留。本次凭据期限由服务端注入并在提交时复核，历史读取不因原操作者凭据现在过期改写合法事实，沿139/140快照边界。
- 新契约/策略、临界完整草案及关联旧决定/身份单元通过；隔离PG元数据、0095空库往返、严格原始命令和凭据过期提交回滚通过。Ruff/格式/Mypy通过。补齐原生非法目标跳转守卫，API真实拒绝与孤立迁移等部分断言通过；整链失败不能作为完整API验收。
- API欠项：一次批准在告警注入前提前拒绝，原因未定位；随后Linux诊断在131历史授权守卫中止。WSL/Docker时钟倒退另有实测，不能把所有失败均归因于环境。详见[141记录](progress/141-reinvestigation-approval-decision.md)，报告在`.pytest_cache/slice141-*`。
- 按用户本轮要求停止长链复跑、自然TTL等待及共享环境修复，不放宽业务守卫。夹具从创建时固定合法1800秒额度，使用实时UTC和三份固定失败样本；未扩展运行中预算。测试只使用本项目临时容器，未启动整套Compose；共享内核参数未改。本轮收尾时Docker不可用，未重新启动，也不恢复其他项目容器。


## 下一步

本轮不继续长链复测。完整API欠项应在推进实际执行前关闭，不自动进行共享调参。后续目标为**第142步新批准的受限执行前检查**：先读旧120模块复用边界，精确绑定新决定与完整新草案，保留全部未决条件与观察窗口；检查事实不能代替条件核验、动作授权或新鲜执行依据。

follow_up分派、预算追加、held/unknown人工处理、终端取消、多步骤、前端和完整发布仍待后续；原settled父run的旧等待输入命令未开放。

## 最小阅读与代码入口

先读[AGENTS](../AGENTS.md)、本文、[进度索引](progress/README.md)与[141设计](architecture/141-reinvestigation-approval-decision.md)，其他按本片依赖定向补读。

| 入口 | 用途 |
| --- | --- |
| [141 contracts](../src/opsswarm/approval/reinvestigation_decision/contracts.py)、[policy](../src/opsswarm/approval/reinvestigation_decision/policy.py) | 严格动作、完整草案保全及负面时限 |
| [store](../src/opsswarm/approval/reinvestigation_decision/store.py)、[queries](../src/opsswarm/approval/reinvestigation_decision/queries.py) | 原子决定、可信审计、精确重放与自己的批准阶段 |
| [0096迁移](../alembic/versions/0096_reinvestigation_approval_decision.py)、[API测试](../tests/api/test_reinvestigation_decision_api.py) | 原生四事实、真实前缀、孤立迁移与写后依据守卫 |
| [140请求](architecture/140-reinvestigation-approval-request.md)、[旧120设计](architecture/120-candidate-execution-preflight.md) | 本片来源与下一片复用边界，不借用旧批准执行 |

总方向见[64路线](architecture/64-full-multi-agent-roadmap.md)、[65设计](architecture/65-agent-system-design.md)；数据库/API事实同步[04](architecture/04-database-design.md)/[05](architecture/05-api-design.md)。131～140冻结SQL与旧图恢复语义保持。

## 开发与验证偏好

遵循AGENTS按风险验证：受影响单元、必要真实PG/API与代表性链；通过后不因文档更新重跑，失败精准定位复验，长链串行。进度通常几百字；主Agent统一核验及提交。`output/`与`tmp/`保持未读取、修改或提交。
