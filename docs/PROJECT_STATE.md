# OpsSwarm 项目状态与开发交接

核对日期：2026-10-28。最新完成切片为第136步独立回流审查与精确提案绑定，基线main的e2b912a（第135步）。本文随第136步独立提交保存，实际提交与工作区以Git核对，结果见[第136步验收记录](progress/136-reinvestigation-review.md)。

## 1. 新会话阅读顺序

1. 阅读 [AGENTS.md](../AGENTS.md)、本文和 [进度索引](progress/README.md)。
2. 先读[第136步设计](architecture/136-reinvestigation-review.md)和[进度记录](progress/136-reinvestigation-review.md)，再读[第135步设计](architecture/135-reinvestigation-synthesis.md)和[进度记录](progress/135-reinvestigation-synthesis.md)，再读[第134步设计](architecture/134-reinvestigation-execution.md)和[验收记录](progress/134-reinvestigation-execution.md)，再读[第133步设计](architecture/133-reinvestigation-dispatch.md)和[验收记录](progress/133-reinvestigation-dispatch.md)，再读[第132步设计](architecture/132-reinvestigation-coordination.md)和[验收记录](progress/132-reinvestigation-coordination.md)，再读[第131步设计](architecture/131-bounded-reinvestigation-admission.md)和[验收记录](progress/131-bounded-reinvestigation-admission.md)，再读[第130步设计](architecture/130-candidate-incident-convergence.md)和[验收记录](progress/130-candidate-incident-convergence.md)，再读[第129步设计](architecture/129-candidate-parent-settlement.md)和[验收记录](progress/129-candidate-parent-settlement.md)，再读[第128步设计](architecture/128-candidate-recovery-verification.md)和[验收记录](progress/128-candidate-recovery-verification.md)，再读[第127步设计](architecture/127-candidate-release-worker.md)和[验收记录](progress/127-candidate-release-worker.md)，再读[第126步设计](architecture/126-bounded-release-dispatch.md)和[验收记录](progress/126-bounded-release-dispatch.md)，再读[第125步设计](architecture/125-release-receipt-reconciliation.md)和[验收记录](progress/125-release-receipt-reconciliation.md)，再读[第124步设计](architecture/124-candidate-release-execution-admission.md)和[验收记录](progress/124-candidate-release-execution-admission.md)，再读[第123步设计](architecture/123-lab-release-state-cas.md)和[验收记录](progress/123-lab-release-state-cas.md)，再读[第122步设计](architecture/122-condition-basis-approval.md)和[验收记录](progress/122-condition-basis-approval.md)，再读[第121步设计](architecture/121-candidate-condition-verification.md)和[验收记录](progress/121-candidate-condition-verification.md)，再读[第120步设计](architecture/120-candidate-execution-preflight.md)和[验收记录](progress/120-candidate-execution-preflight.md)，再读[第119步设计](architecture/119-candidate-approval-decision.md)和[验收记录](progress/119-candidate-approval-decision.md)，再读[第118步设计](architecture/118-candidate-approval-admission.md)和[验收记录](progress/118-candidate-approval-admission.md)，再读[第117步设计](architecture/117-candidate-plan-admission.md)和[验收记录](progress/117-candidate-plan-admission.md)，再读 [第 116 步设计](architecture/116-candidate-evaluation.md) 与 [验收记录](progress/116-candidate-evaluation.md)，随后阅读 [第 115 步设计](architecture/115-human-reassessment-routing.md) 与 [验收记录](progress/115-human-reassessment-routing.md)，再读第 114 步实际重评、第 113 步输入准备、第 112 步可信人工命令、第 111 步终端交接、第 110 步来源核验及第 64～65 步中的恢复和人工介入章节。
3. 阅读 [完整路线](architecture/64-full-multi-agent-roadmap.md)、[Agent 专项设计](architecture/65-agent-system-design.md) 中本片相关章节；数据库和 API 事实以 [数据库设计](architecture/04-database-design.md)、[API 设计](architecture/05-api-design.md) 和实现为准。
4. 核对 Git、相关代码、迁移与测试，再确定下一片状态、接口、持久化和失败路径。

架构规定目标与契约，代码和迁移展示实现，进度记录提供验收证据。本文是当前事实摘要，不替代专项来源。

## 2. 项目目标与边界

在 MVP 上建设完整多 Agent 事故响应平台，展示任务分解、专业调查、共享事实、分层记忆、上下文管理、动态补查、独立审查、人工介入和持久恢复。多 Agent 是既定方向，按小切片推进；真实环境操作仅限隔离的本地 Docker Compose 演练系统，不读取原 RAG 仓库。

## 3. 当前架构与技术栈

- Python 3.12、FastAPI、Pydantic 2；PostgreSQL 17、SQLAlchemy 异步、Psycopg 3 与 Alembic；独立调查/验证/经验候选/人工重评/候选评估 Worker，业务数据库和 checkpoint 分离。
- LangGraph 管理固定 v1/v2 图、专业并行与 checkpoint；v3 图 v15 已按持久计划动态执行并综合三轮专业任务，三轮开放假设均形成独立审查和确定性路由，第三轮在冻结上限停止。图 v1～v14 保留各自历史语义。LangChain 管理模型适配及新策略专业工具循环；内部 Harness 已有任务、共享预算、上下文与逐次交互组件，整体仍未完成。
- 显式启用动态 Worker 后可领取冻结 v3 graph-v15，完成后将父运行置为 await_human/await_candidate，工作项完成并释放租约，父尝试 handed_off。默认仍只领 v1/v2；历史图不自动升级。交接不是旧根因成功或人工 interrupt/resume。动态演练来源 release-500-worker-v1 按事故时间锚定，限 order-service 发布故障，不是现场证据。
- await_human 支持可信工程师补充与取消、状态及业务审计查询，即使 legacy_local 也必须 Bearer。连续等待版本和完整命令幂等绑定原交接及主体；取消后 run 为 cancelled，Incident 不随人工取消迁移（调查期仍 investigating，候选待审批期仍 awaiting_approval），父工作项/尝试与预算不变。独立准备 Worker 生成完整输入、清单和原预算诊断；第 114 步显式可信重评命令由独立 Worker 实际调用结构化模型，独立保存意见。新尝试在原共享账本预留，原图与父尝试保持；第 115 步保存后续确定性未分派意图、原预算快照和完整人工约束；意见与意图仍无审批或执行资格。图内 interrupt/resume 尚未实现。
- v1/v2 仍固定日志、指标、变更三个专业任务和综合节点；v3 已动态选择首轮角色与材料，为两次 `follow_up` 分别编译并执行第二、三轮任务，三轮结果分别综合、独立审查和路由。第三轮即使保留完整补查建议也只能进入人工等待并记录轮次上限，不能创建第 4 轮。专业新策略只读取已保存证据和系统选中的 Git 手册，不动态查询现场遥测。
- 来源包括冻结快照及 Loki、Prometheus、本地变更服务。新建现场 v2 的日志与指标任务分别通过独立只读 MCP 获取本地 Loki 与 Prometheus 证据，变更仍直连；历史直连和仅日志 MCP 策略按原路径恢复。所有现场任务使用固定 UTC 窗口和受限异步执行器。
- Vue 3/TypeScript 作战室、REST/可重放 SSE、OpenTelemetry/Jaeger 与 Compose 保留。第 104 步提供可选控制面 Bearer 认证、角色权限和授权准入审计；当前页面尚未适配 Bearer，默认兼容演练模式继续使用自填身份。
- 精确依赖见 [uv.lock](../uv.lock)、[web/package.json](../web/package.json)。业务迁移唯一head为 `0091_reflow_review`；第136步仅在 Testcontainers PostgreSQL 测试数据库执行迁移，未升级用户的 Compose 业务数据库。

## 4. 已完成内容与本会话成果

MVP 历史交付见 [第 63 步](progress/63-mvp-compose-release.md)：调查、证据、审批、白名单执行、恢复验证、人工关闭、不可变复盘报告、改进行动项与作战室已形成演练闭环。原单 Agent 路径、固定多 Agent 路径、评测数据和历史结果保留。第 81 步没有重新执行完整 MVP 发布演示。

此前工程版记录可由 Git 历史逐项核对：

| 切片 | 提交 | 已交付边界 |
| --- | --- | --- |
| [64](progress/64-full-multi-agent-roadmap.md)、[65](progress/65-agent-system-design.md) | `7d4a265`、`01469ee` | 总路线、长期约束与 Agent 专项设计落盘；属于设计验收 |
| [66](progress/66-investigation-task-persistence.md) | `01c82b0` | 轮次、专业任务、尝试事实与必要查询 |
| [67](progress/67-fixed-graph-task-integration.md) | `f61386f` | 冻结 v2 固定图接入任务、结果、事件和恢复核对 |
| [68](progress/68-shared-call-budget.md)、[69](progress/69-model-input-budget.md) | `6b5c845`、`14c4b42` | 次数/时限预算；显式模型输入、Token 和估算费用预留，未知用量不补零 |
| [70](progress/70-bounded-async-evidence.md)、[71](progress/71-task-evidence-snapshot.md) | `da72125`、`089ead7` | 有界异步取证、迟到隔离；模型前持久取证快照与重试复用 |
| [72](progress/72-explicit-observation-windows.md)、[73](progress/73-live-task-evidence.md) | `daeda09`、`487ef9f` | 显式观测窗与现场 v2 接入；假传输现场回归不等于新的真实现场验收 |
| [74](progress/74-stable-frozen-identity.md) | `34bcbf9` | 冻结身份跨进程确定性、已发布内置版本的有限历史兼容 |
| [75](progress/75-task-context-builder.md) | `6fc4f67` | 最小 ContextBuilder、任务隔离、不可变材料清单与输入预留原子提交 |
| [76](progress/76-context-evidence-reader.md) | `05844ca` | 已保存调用范围内的证据目录与受控详情读取服务/API |
| [77](progress/77-repeated-call-identity.md) | `090a44e` | 同尝试重复调用槽位、兼容旧幂等键、共享预算和迁移保护 |
| [79](progress/79-bounded-evidence-reading.md) | `9475621` | 专业目录与受限证据读取循环、完整配对交互、逐次预算及恢复验证 |

第 81 步提交为 `e744dd2`，第 82 步提交为 `d524db9`，第 83 步提交为 `5d3d702`，第 84 步提交为 `e4922d3`，第 85 步提交为 `f5287a6`，第 86 步提交为 `778e434`，第 87 步提交为 `8eb4712`，第 88 步提交为 `956dad6`，第 89 步提交为 `3e98d7a`，第 90 步提交为 `e902e0b`，第 91 步提交为 `3e2813c`。第 92 步第二轮专业任务执行对应提交 `cdd9e0d`，第 93 步第二轮综合与开放假设恢复对应提交 `48bef25`，第 94 步第二轮独立审查对应提交 `0115537`，第 95 步第二轮确定性再次路由对应提交 `5da470d`，第 96 步第三轮计划持久化对应提交 `f4ca64c`，第 97 步第三轮专业任务执行对应提交 `7d2cb7b`，第 98 步第三轮综合对应提交 `59217c2`。第 99 步第三轮独立审查对应提交 `2ac9b69`。第 100 步第三轮最大轮次停止路由详见 [当前验收记录](progress/100-third-round-terminal-routing.md)，对应本片独立提交。

第 111 步把当前图接入显式动态 Worker 能力，新增独立终端 outcome 和不可变交接事实，原子保存当前父尝试、两类等待状态、用量与可重放事件并释放租约。最高轮来源、真实时钟租约、不同快照恢复、并发重放和前后提交崩溃均通过验证；在途调用禁交接由应用与原生守卫校验。旧 complete/数据库拒绝 v3 succeeded；等待继续占用事故活动槽位。新增只读 terminal-handoff 接口与原生状态守卫；对应提交 `55d70dc`。

第 112 步在原交接上增加连续版本人工事件和逐请求业务结果审计。可信工程师可提交最多 15 次有界陈述/约束/问题，或取消本次自动调查；取消生成终态，保留原交接、completed 工作项、handed_off 尝试和共享预算，事故仍 investigating。完整规范命令绑定主体和幂等键，旧成功命令在取消后仍可重放。原始正文与实际授权来源重建校验，业务 404/409 先提交审计；父工作项锁、等锁后及提交前凭据期限检查覆盖并发与回滚。原生守卫、历史交接读取/重放和保守降级均通过，人工材料不是工具证据，尚未进入模型。

第 113 步在第 112 步（提交 `a8b5420`）之上增加可信 prepare 命令、独立持久准备任务/尝试及 Worker。完整输入绑定原交接、最高终端轮次的成功审查材料和全部可信人工补充，保留约束与来源，必要材料超窗明确阻断。不可变工件保存输入、材料清单和原预算诊断；查询重算当前适用性，历史工件不因后续补充、取消或时间经过而改写。每等待版本一个任务、最多三次尝试，租约接管、写后过期回滚、可信审计及原生闭环通过。准备不预留预算、不发送模型请求、不改变旧 run/work/attempt 或 checkpoint。

第 114 步在第 113 步（提交 `550ed59`）之上新增可信重评命令、独立任务/尝试、同一账本的新操作与实际执行归属、结构化模型调用及不可变意见。完整输入与证据闭包保留，人工陈述不变为工具证据。补充/取消后的响应不发布，失联预留记 unknown，不退还占用；成功调用结算后中断可跨尝试复用。模型返回后继续检查真实累计用量，超预留只保留审计，不生成业务意见。历史交接可在新阶段在途期间读取；旧预算与收尾不能冒用新尝试。

第 115 步在第 114 步（提交 `da93df0`）之上新增重评后续路由。原审查规则与原最高轮/任务数保留，外层按人工 constraint、原截止时间和共享预算最低容量确定等待/补查/候选评估意图，全部 not_dispatched。成功意见和意图原子提交，旧成功结果由原重评 Worker 无模型补齐；work→job→Incident 锁、SKIP LOCKED、有界公平扫描与来源拒绝隔离保护并发。可信 GET 区分历史决定、当前适用性与当前阻断，补充/取消后历史不改。未创建新的补查任务、候选或授权。

第 116 步增加双来源可信候选任务和独立模型评估：来源为原 await_candidate 交接或第 115 步当前候选意图，固定完整记录散列与等待版本。参数、风险和验证规则由候选明确引用的证据构建；产物 planning_review/await_human，始终 execution_eligible=false。独立执行身份、完整来源与上下文、原预算预留和迟到隔离已验收，结果见本片记录；未创建规划、审批或操作，原 run/work/attempt/图保持。

第117步在第116步独立候选结果上增加可信ActionPlan草案准入。原始正文/规范命令/主体及run/job路径精确绑定可信授权；work→job→Incident锁下核验结果hash、等待和事故版本、完整来源、原预算及deadline，不新增模型或工具调用。独立草案保留开放假设、步骤、前置条件和未解问题，固定draft且无审批/执行资格，旧流程按旧表隔离。来源、事故版本或截止变化后历史不改而投影stale，精确重放允许读取；写后时限/凭据过期整体回滚。

第118步在第117步草案上新增独立可信待审批请求，绑定精确内容摘要、原等待版本及事故迁移前后版本/事件。请求、Incident REQUEST_APPROVAL与applied审计原子提交，原生检查真实OLD状态与相邻版本；草案仍不可变、无执行资格。独立审批阶段current不受本次合法迁移影响，真实补充/取消、事故变化或原截止后按历史stale/expired读取；相同命令重放不续期、不再迁移。可信人工输入入口仅为精确同运行候选待审批阶段增加访问，连续补充可使旧请求保持失效。没有批准、拒绝、过期收敛、候选执行或新前端展示。

第119步增加独立不可变approved/rejected/expired决定、可信approver命令和原子事故迁移/审计。批准须原输入、事故版本、原预算与期限有效，迁移mitigating后按决定阶段current投影；拒绝可收敛stale输入，到期须显式expire命令回investigating，GET无写入。原成功用量完整性与累计容量分开检查，unknown不补零；错误正文/权限/版本及写后超时或认证到期均拒绝或回滚。原请求/草案与父图不改，未交付候选执行、撤销批准、新前端、父活动槽位释放或定时过期Worker。

第120步建立独立可信执行前检查：executor绑定精确approved决定、mitigating事故版本、原输入/累计预算/期限，读取本地演练当前参数并保存不可变观测与业务审计。只允许三个固定GET，响应有界、总超时、失败不回退冻结证据；配置/Redis比较当前参数，release开关不能证明版本。全部原自由文本条件及未解问题保持未核验阻断，报告始终blocked且execution_eligible=false；同键重放与GET只读原观测，30秒观测期限不续原批准。新操作身份不借旧表，原Incident及事件、父run/work/attempt/图均不改；来源损坏409、已有事实不降级，写后认证/期限失败原子回滚。当前不是实际执行或父活动槽位释放。

第121步从精确原计划生成固定条件目录，可信engineer按完整条目追加最多8个不可变核验版本，前驱版本/hash以CAS保护。来源区分原证据、人工观察与真实同批准预检；状态分别为verified、unresolved、not_applicable，汇总ready_for_review、blocked或plan_revision_required。最高版本损坏不能回退；适用性区分当前、被替代、失效与到期。所有内容固定approval_required=true和execution_eligible=false，新审批必须绑定精确核验版本，原119批准不覆盖新依据。原预算/期限和写后认证仍强制，原Incident/事件、父图与旧执行表保持，尚无核验审批或实际执行。

第122步为最新有效ready_for_review核验建立独立可信工程师请求和审批者approve/reject/expire决定，绑定核验ID/版本/hash、原计划/决定和原事故版本。请求期限等于核验期限，原预算不重置；新版本、来源变化或到期使旧批准失效。请求/决定及各自applied审计原子提交，历史不可变；已有决定的请求投影recorded，负面决定允许在预算耗尽后收敛但不能跳过历史完整性。最高版本损坏、三资源授权、并发单胜、写后截止/认证回滚和换键拒绝降级均通过。没有Incident迁移或新事故事件，原父图及活动槽位保持，内容execution_eligible仍false；不是实际执行或图恢复。

第123步补齐显式启用的本地发布演练版本/CAS。PostgreSQL保存stable/rc1版本、单调revision、命令序号与不可变命令；版本及修订共同防止漂移/ABA，重放区分历史结果与当前适用性。新模式旧开关、订单及指标共享持久状态，重启不重置，存储失败/损坏不回退健康；默认内存兼容模式保持。应用配置启用、启动/退出及失败清理、原生原子状态守卫和多实例并发已验证。它是模拟行为版本，不改变真实镜像/签名清单，不消费候选审批，也未接入候选执行或父运行收尾。

第124步将当前有效核验批准、原发布步骤和只读演练版本/修订绑定成不可变执行请求，状态prepared、dispatch_eligible=false且须派发前复查。可信executor精确绑定三资源及原始正文，完整重建审批与最高核验前缀，先核原预算/期限与白名单再固定GET123；观测30秒时效、写后认证/期限回滚、并发单胜和不降级审计均通过。GET/重放不重采、不续期，原lab状态、Incident/事件、父图和预算保持。因123尚缺远端只读回执/受限派发恢复协议，本片没有接入Worker或外部写入，不能将准备事实描述为执行完成。

第125步复用原持久演练命令，提供按稳定键的只读回执：命中200、缺失404均带完整模拟来源，查询不写、不初始化，损坏503。共享头锁保证目标/邻接/当前状态一致，独立有界GET客户端与纯对账区分not_recorded、matched_current、matched_superseded和conflict。未知不自动重发，历史成功不证明当前恢复；未新增控制面执行结果、派发或Worker，124请求仍不可派发。

第126步交付远端固定窗口的注册/实际CAS/查询/取消：持久保存原命令和最多30秒窗口，规范候选键受数据库当前时钟、取消状态及原子成功命令约束，锁等待不续期、长事务越期整体回滚；应用与取消单胜，丢失响应按原ID只读。该管理协议不等于控制面可信审批后的执行链路，Worker与unknown结果尚未接入，124请求仍不可派发，父运行不变。

第127步将精确124准备请求接成可信执行任务及独立Worker，固定凭据版本/原窗口并重验审批最高版本、原预算/期限，首次POST前提交永久派发标记，注册后再核验才唯一PUT。重领只GET，三次失租收敛，完整远端材料和执行结果不可变保存；真实126正常CAS、丢响应/ABA及四个崩溃边界已验收。原false字段和父run/Incident/预算/图保持，结果仅证明模拟操作历史；尚无客观恢复验证、父收尾或活动槽位释放。unknown终态没有自动重开/业务取消接口。

第128步增加独立只读恢复Worker，自动发现127终态并绑定原执行/结果摘要/operation；120秒窗口受原运行总期限约束，最多3个30秒尝试、每次3份订单业务样本且间隔至少2秒。逐次样本持久化，重试不续期或跨尝试拼接，明确recovered/not_recovered/source_failed/execution_unknown/not_executed；版本/effect_current不是健康证明。原生和应用均重建完整历史、拒绝迟到及伪造材料，结果不推进父链，不能证明生产可用率或恢复因果。新Worker/GET入口见第128篇；unknown人工处理、父运行收尾和回流仍待后续。

第129步新增独立数据库收尾进程，完整重建原执行/恢复历史并确认当前依据，在父work锁和单事务内保存唯一收尾事实、run.candidate_settled进度事件及父run.status=settled/completed_at。独立outcome/reason保留原五类结果，disposition区分验证恢复、需要重新调查和需要人工解决；后续意图尚未实际执行。原交接/work/handed_off尝试、127/128正文与false字段、预算和图保持。旧历史GET通过窄校验兼容新终态，当前写资格失效；Incident仍mitigating且活动索引包含settled，事故状态和槽位收敛仍待后续。SQL先过滤永久失格材料，避免八条旧记录阻塞后续合格来源。

第130步消费129唯一收尾事实，按同次三份业务样本的固定期限、当前事故版本和最新告警保存不可变收敛决定。新鲜恢复经mitigating→verifying→resolved并释放活动槽位；明确验证失败经verifying回investigating但保留槽位。未知、来源失败、过期、新告警或依据变化只保留决定，不能当作健康结论。0085原生校验两跳、精确事件、提交时效和同事务marker，父settled时刻与128/129、预算、图保持。历史按保存的告警引用和事件重建，后续合法关闭/复发仍可读取；等待事故锁的迟入库告警可以正常重开事故。

第131步增加可信工程师显式回流准入，精确绑定130 observed_failure、当前事故版本和事件游标后的新告警。每原run最多一次请求，固定root/previous/budget_run均为原run，完整预算快照保留原总截止、累计次数/每任务上限/Token/估算费用和未知用量；门槛只保证当时容纳一次完整协调调用。0086保存不可变请求及逐authorization审计，原生验证完整来源/账本前缀、同事务成功审计和实际提交deadline。请求not_dispatched，原父/槽位/预算/图保持；当前旧预算入口要求running父租约，未来需专属阶段同原账本预留，不能声称实际回流已交付。

第132步消费131唯一准入，新增独立回流协调阶段/尝试/租约和完整冻结输入，以coordinate_reinvestigation操作在原账本真实预留和结算。严格保留原剩余轮次、任务数、累计预算和总截止；必要材料超窗则blocked零调用。成功调用可跨尝试复用，失联预留保留unknown占用，新告警/来源变化/迟到结果不能发布。产物仍为not_dispatched协调建议，原父settled、图、checkpoint及128～131保持；尚未创建回流专业任务。

第133步将132成功建议编译为独立业务轮次及pending专业任务，原逻辑轮次/累计任务上限和原账本保持。旧最高轮与132来源需要保持，故任务使用独立表而不追加旧表；不是新run。分派冻结完整预算前缀和事故事件游标，取证窗口限制在失败验证之后，服务由系统注入，旧快照不复用。并发、幂等和整轮原子写入由短事务及原生守卫保护；真实专业尝试、取证和模型调用仍待下一片。

第134步新增按133 task.id绑定的独立执行投影、真实尝试/租约、新证据/packet/专业结果；133队列及原父settled/持槽、旧图/checkpoint保持。日志/指标用既有只读MCP，变更用受限线程；窗口向内取整并保存原请求与未观测区间，短窗零调用blocked。所有专业/重试在原账本预留，按真实逻辑任务计工具次数，并发预留保守累计。成功快照和成功模型调用可跨尝试复用，未知不补零，新告警/超期/迟到不发布。当前没有回流综合、独立审查或后续处置。

## 5. 当前能力与限制

第 79 步通过显式 `context_policy_version=evidence-read-v1` 开启专业按需读取；必须同时提供 v2 与 function_calling 模型预算 profile。省略字段仍按旧规则选择 task-context-v1 或 null，v1/历史 v2 不升级。

- 专业模型先接收本任务目录，最多每轮一个 read_evidence 或 SpecialistReport 请求。工具参数只有证据 ID 与快照散列，运行、任务、尝试和角色由系统注入；报告只能引用本尝试成功读取的证据。
- 每次模型/业务工具调用前预留，返回与结算同事务保存。下一输入从成功且完整配对的账本重新组装并在预算事务内复核。内部循环没有独立 checkpoint；未完成任务技术重试从原快照目录开始，新尝试不清空预算。
- 新增显式 `summary-context-v1`，同样要求 v2 与 function_calling 模型预算，并固定摘要策略。综合接收三份完整工作摘要替代独立报告段，保留完整有界证据；组装和预算预留事务均重算来源。旧策略综合仍接收三个已提交专业报告和完整有界证据。冻结和现场模式均使用假模型/假传输回归，没有新的真实模型或现场发布验收。
- 默认预算仍为 12 次模型、18 次工具、每任务 6 次工具、300 秒。原始取证与按需读取共用额度；完整三轮并完成第三轮审查需要 13 次模型调用，创建运行时必须显式固定更高调用预算，系统不静默提高默认上限。默认输出预留 4096、累计 24000 无法支持至少七次模型调用；开启者还必须显式配置合适模型预算，测试使用单次输出 1024。
- call-budget 是累计实际调用预留和用量的查询入口。旧 result.tool_call_count / 评测字段仍表示固定图三次取证，不能称作新循环总工具数。
- 输入采用保守字节估算；未知用量保持未知。合法响应也受 32 KiB 规范化 JSON 上限约束，未通过校验的原始正文不保存为历史；失败仍保留状态与可取得的供应商用量。没有证明 Token 或费用下降。
- 工具失败可能被框架转换成本地错误消息，但不完整历史在下一请求前即被拒绝；既有 Worker 技术重试仍有界且消耗原预算。

第 80 步新增摘要材料；第 81 步扩展为新建 evidence-read-v1 或 summary-context-v1 运行固定 `working_summary_policy_version=task-working-summary-v1`，任务成功时生成有界不可变摘要，与结果/状态/事件同事务提交。查询、成功结果恢复与父运行完成均复核摘要来源；失败调用按引用保留，不推断问题分类。摘要可通过只读 API 查询，并在 summary-context-v1 中进入综合输入；旧策略不消费摘要。

第 82 步新增 `dynamic-coordination-v1` 策略、结构化提案及编译结果。模型可选范围仅为本轮目标、日志/指标/变更角色、任务目标和回看时长；编译器注入事故服务、UTC 截止时间、证据快照身份和尝试上限，拒绝轮次、角色、窗口或总任务额度越界。该能力目前是纯领域闸门，尚未接入模型、业务数据库或工作流。

第 83 步把首次固定的完整协调策略、编译计划、父 Worker 尝试、实际轮次和任务原子持久化。查询从事故、策略、父尝试、轮次和任务重建核对；冲突重放、固定/动态历史混用、来源篡改与租约失效均拒绝。截至该步尚无协调模型、HTTP 查询、v3 启动或动态图。

第 84 步新增根级 `coordinate` 结构化模型调用，使用原运行共享次数、时限、Token 与估算费用预算；上下文只含事故与固定协调策略，没有工具或执行权限。提案通过 Pydantic 与确定性编译后才结算成功，并作为计划来源保存。模型成功后、计划提交前崩溃会复用调用；计划提交后崩溃会直接复核并返回原计划。第 86 步已将该入口兼容接入 v3，原 v2 测试路径和清单版本保持不变。

第 85 步新增隔离的 `workflow_version=v3` 和 `graph_version=dynamic-investigation-graph-v1`。v3 只接受冻结多 Agent 与 function_calling 模型预算，创建时固定完整协调策略、summary-context-v1、工作摘要、task-evidence-v1、调用预算和模型预算；原运行查询返回全部启动契约。现有 Worker 的领取 SQL 只声明 v1/v2，能够越过队首 v3 继续领取兼容任务；旧处理器和固定图在 checkpoint 访问前再次拒绝 v3。截至该步尚未创建或恢复动态图 checkpoint。

第 86 步新增独立 `START → coordinate → END` v3 图和装配工厂。协调节点在当前父租约内调用既有规划服务，checkpoint metadata 与状态都保存并核对工作流/图版本；恢复实例注入新租约，checkpoint 不保存租约令牌。模型成功后计划提交前、计划提交后 checkpoint 前两个窗口都通过业务调用或计划复用收敛。直接集成测试中的 v3 父运行保持 running 且没有最终结果；通用 Worker 仍跳过 v3，因为当前图尚无专业结果、综合和终态校验。

第 87 步保留上述仅协调图 v1，并将新 v3 运行固定到 `dynamic-investigation-graph-v2`。图 v2 从已持久化计划稳定排序并 `Send` 1～3 个专业分支，按每个任务的 UTC 窗口过滤冻结材料，复用已有任务尝试、取证、共享预算、专业循环和工作摘要。并行单节点失败后，新父租约只恢复必要任务；业务结果已提交但 checkpoint 未写时直接复用原结果。上下文清单记录真实 v2/v3 版本。首轮完成仍不结算父运行，通用 Worker 继续跳过 v3。

第 88 步继续保留图 v1/v2，新运行固定到 `dynamic-investigation-graph-v3`。图 v3 在首轮实际选择的 1～3 个任务后，只用其已提交工作摘要和证据执行一次根级综合；输出为自由文本开放假设、支持证据、反证和未解问题，系统注入 `pending_review`。开放假设及成功综合调用来源不可变持久化，模型成功后落库前和落库后 checkpoint 前两个窗口均可跨父尝试恢复。它不含旧 `RootCause`、处置建议或执行资格，父运行仍保持 running，通用 Worker 继续跳过 v3。

第 89 步继续保留图 v1/v2/v3，新运行固定到 `dynamic-investigation-graph-v4`。图 v4 在开放假设后执行无工具的独立审查，只读取原综合调用的同源首轮材料和当前不可变假设，保存支持充分性、接受的支持、反证、证据缺口、竞争性解释与建议方向。成功审查调用和 `recorded` 审查事实均可跨两个提交窗口恢复；建议不创建任务、不代表根因证明或执行资格，父运行仍保持 running，通用 Worker 继续跳过 v3。

第 90 步继续保留图 v1/v2/v3/v4，新运行固定到 `dynamic-investigation-graph-v5`。图 v5 在独立审查后用纯规则检查开放假设反证、审查反证/缺口/竞争解释及冻结轮次和任务容量，保存 `follow_up`、`await_human` 或 `candidate_evaluation` 不可变事实。路由不增加模型/工具调用，不创建第二轮任务，也不改变父运行、假设或审查状态；事实提交与 checkpoint 之间可以跨父租约恢复，通用 Worker 继续跳过 v3。

第 91 步继续保留图 v1～v5，新运行固定到 `dynamic-investigation-graph-v6`。图 v6 只消费 `follow_up` 路由，把全部建议按稳定角色顺序编译为第二轮计划，复用首轮冻结快照和截止时间，并在一个租约事务内创建轮次、待执行任务和事件。计划以 `source_review_route_id` 一一绑定同运行路由；另外两种路由不创建计划。该节点不调用模型或工具、不执行第二轮、不结算父运行，计划提交前后两个 checkpoint 窗口均可恢复，通用 Worker 继续跳过 v3。

第 92 步继续保留图 v1～v6，新运行固定到 `dynamic-investigation-graph-v7`。图 v7 只从已持久第二轮计划稳定分派专业任务，使用独立 `follow_up_results` 隔离两轮同角色结果，并让任务运行器显式核对期望轮次。第二轮复用冻结范围、任务证据、专业循环、工作摘要和原运行共享预算；部分失败接管只重试失败任务，结果落库后 checkpoint 前中断不会重调外部请求。第二轮结算为 `completed`，父运行仍为 `running`；另外两种路由不执行补查，通用 Worker 继续跳过 v3。

第 93 步继续保留图 v1～v7，新运行固定到 `dynamic-investigation-graph-v8`。图 v8 只在 `follow_up` 第二轮任务全部完成后，使用第二轮已提交工作摘要和完整证据执行一次根级综合，并以独立 `follow_up_hypothesis` 保存绑定第二轮计划的 `pending_review` 开放假设。首轮和第二轮 `synthesize` 分别固定调用槽位 0 与 1，第二轮使用独立提示/角色版本；模型成功后事实提交前和事实提交后 checkpoint 前均可跨父租约恢复。另外两种路由不新增综合调用或假设，父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 94 步继续保留图 v1～v8，新运行固定到 `dynamic-investigation-graph-v9`。图 v9 只在第二轮开放假设后执行独立审查，以 `follow_up_hypothesis_review` 保存绑定第二轮假设的 `recorded` 审查事实。首轮和第二轮 `review` 分别固定调用槽位 0 与 1，第二轮使用独立提示/角色版本；上下文清单与第二轮综合材料逐项同源并只追加当前假设。两个提交窗口均可跨父租约恢复，同槽位重复成功调用会在查询和恢复入口被拒绝。另外两种路由不新增第二轮审查，父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 95 步继续保留图 v1～v9，新运行固定到 `dynamic-investigation-graph-v10`。图 v10 从第二轮假设、同轮审查、冻结协调策略及两轮真实任务数重新编译并保存 `follow_up_review_route`。两条路由按业务轮次查询；第二轮 `follow_up` 只表达仍有容量的第三轮意图，不创建计划或任务。事实提交前和 checkpoint 前窗口均可跨父租约恢复，不增加模型、工具或预算调用；父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 96 步继续保留图 v1～v10，新运行固定到 `dynamic-investigation-graph-v11`。图 v11 只在第二轮路由为 `follow_up` 时，把完整建议确定性编译为绑定该路由的第三轮计划，并在有效父租约事务内原子创建第 3 轮和待执行任务；另外两种路由不创建第三轮。来源轮次门禁使图 v10 即使直接调用仓储也不能写入第三轮。两个计划提交窗口均可跨父租约恢复，不增加模型、工具或预算调用；父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 97 步继续保留图 v1～v11，新运行固定到 `dynamic-investigation-graph-v12`。图 v12 只从已持久化第三轮计划稳定分派任务，使用独立 `third_round_results` 隔离同角色跨轮结果，并显式核对 `expected_round_number=3`。第三轮复用冻结材料、任务尝试、共享预算、专业工具循环、结果和工作摘要；部分失败只重试失败任务，结果提交后 checkpoint 前中断不重调外部请求。第三轮结算为 `completed`，父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 98 步继续保留图 v1～v12，新运行固定到 `dynamic-investigation-graph-v13`。图 v13 只在第三轮任务全部完成后，使用第 3 轮已提交工作摘要和完整证据执行槽位 2 根级综合，以独立 `third_round_hypothesis` 保存绑定第三轮计划的 `pending_review` 开放假设。三轮综合分别使用槽位 0/1/2 和独立提示版本；模型成功后事实提交前与事实提交后 checkpoint 前均可跨父租约恢复。父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 99 步继续保留图 v1～v13，新运行固定到 `dynamic-investigation-graph-v14`。图 v14 在第三轮综合后只读取该综合的同源材料和当前开放假设，以审查槽位 2、独立提示和角色版本保存绑定第 3 轮的 `recorded` 审查事实。成功调用后事实提交前与事实提交后 checkpoint 前均可跨父租约恢复；完整三轮路径显式固定 13 次模型调用预算，既有默认 12 保持不变。

第 100 步继续保留图 v1～v14，新运行固定到 `dynamic-investigation-graph-v15`。图 v15 复用冻结的确定性路由规则，从第三轮假设、同轮审查、运行策略和真实任务数保存第三份路由；清洁充分支持进入 `candidate_evaluation`，有异议、支持不足或轮次用尽进入 `await_human`。最大轮次仍有完整建议时保留建议并记录 `round_limit_reached`，不能保存 `follow_up` 或创建第 4 轮。路由提交前后两个窗口均可跨父租约恢复，不增加模型、工具、来源或预算调用；父运行仍为 `running`，通用 Worker 继续跳过 v3。

第 101 步新增独立 `telemetry-mcp` 进程和唯一只读 `query_service_logs` 工具。新建现场 v2 固定 `task-evidence-live-mcp-log-v1`，日志任务通过固定 Streamable HTTP 地址查询本地 Loki；客户端和服务端双重限制服务、UTC 窗口、Schema、来源和结果，MCP 失败保存稳定账本错误且不回退。历史 `task-evidence-live-v1` 继续 Loki 直连恢复，指标、变更和 Git 路径未迁移。

第 102 步新增独立 `telemetry-metrics-mcp` 进程和唯一只读 `query_service_metrics` 工具。新建现场 v2 固定 `task-evidence-live-mcp-v1`，日志继续走第 101 步 MCP，指标通过固定 Streamable HTTP 地址查询本地 Prometheus；客户端与服务端限制固定服务、UTC 窗口、Schema、来源、值域和样本数。原日志 MCP 的身份、端点和唯一工具不变，`task-evidence-live-v1` 与 `task-evidence-live-mcp-log-v1` 均按原路径恢复，变更和 Git 路径未迁移。

第 103 步新增显式 `runbook-read-v1` 上下文策略和 `read_runbook` 专业工具。首个 Git 管理手册只在 `order-service` 变更任务具有事故开始前一小时内的已完成部署事实时进入目录；模型只能提交手册 ID、版本与内容散列。成功读取的完整正文作为配对工具结果保存，后续模型调用清单追加实际正文的稳定材料 ID、版本与散列。手册不属于现场证据，不能被专业报告引用，也不授予审批或执行权限。历史上下文策略保持原语义，v3 尚未接入手册。

第 104 步新增显式 `bearer` 模式，凭据仅由环境配置，API 的操作者和角色由服务端认证；8 个人工命令注入主体，13 个写入口按权限放行，未登记写入口拒绝。所有业务查询含 SSE 均认证；授权准入记录先持久化并返回 `X-Authorization-ID`，其含义不等于业务成功。默认 `legacy_local` 保留历史演练，历史审批不追认为可信。经验审核后续必须直接要求可信主体。

第 105 步从完整关闭复盘冻结报告、调查、证据、批准计划、成功执行及验证样本，按报告与策略唯一创建经验生成任务。三个新入口始终要求可信 Bearer；独立 Worker 按固定 `report-fact-draft-v1` 作确定性历史投影，最多三次租约尝试，迟到结果拒绝，候选与成功原子落库。来源、请求、尝试身份和候选不可变；查询共享锁避免跨语句混读。候选仅为生成时 `pending_review`，截至该步尚无审核、启用、检索或上下文消费；沿用现有主假设与单步骤复盘契约，不消费 v3 开放假设。

第 106 步增加可信工程师 confirm/reject 审核。候选保持不可变，审核投影单向进入版本 1；确认与独立经验版本同事务保存，拒绝不生成经验。候选行锁、完整规范命令幂等、首次来源重建与独立业务结果审计覆盖并发和409/404拒绝；原授权准入不等于业务成功。确认内容外层与原候选快照分离，当前状态读新 review 接口；三个新入口始终要求 Bearer。

第 107 步在历史 confirmed 正文之外投影 active/1 与 disabled/2，可信停用和业务结果原子保存，完整命令幂等、版本冲突及 404/409 均可追溯。三个新入口始终要求 Bearer；检索范围由持久运行决定，同服务/环境/来源模式且排除同事故与已停用版本，SQL 确定性排序最多三条。来源事故取冻结生成任务快照，共享锁后以新语句复核停用，历史详情保持可查；没有接入模型上下文。

第 108 步保存可信工程师选定的不可变直接后继，双侧精确版本/散列、有效状态、同范围和严格确认时间通过后，替代与业务结果原子提交。来源投影 replaced/2，数据库双向守卫保护停用/替代互斥；后继后来退休不影响旧关系和原成功重放。检索升级 experience-search-v2，先按排名最多选三条，再按 UUID 升序加共享锁并以新语句复核两类终态，避免反向排名锁死锁；经验仍未进入模型上下文。

第 109 步以独立运行策略启用首轮协调经验输入，完整有界内容和精确版本引用实际进入模型；首次预算预留原子冻结最多三条的选择，空选择同样冻结。技术重试、成功调用复用、计划及 checkpoint 恢复保持原材料，退休状态不改历史输入，每次调用继续累计预算。输入/清单查询复核冻结来源；graph-v15、专业、综合、审查和补查输入保持原边界。v15 首轮终端场景及恢复已通过；三轮扩展实验在最终来源递归重建超过 600 秒，不列入通过验收。

第 110 步在单次 v3 来源遍历中复用成功的计划、假设、审查及路由 DTO；命中前以有界行版本和完整集合重查动态来源，包括冻结经验审核授权，变化即拒绝。每次业务操作、会话、事务及 savepoint 都重新核验，不缓存父租约或授权决定。两种冻结三轮路径连同跨租约 checkpoint 恢复分别约 77/100 秒，保持 13 次模型及 6 次工具账本，通过 180 秒、25,000 条业务 SQL 上界；该记录补齐第 109 步同类实验的超时缺口，不修改历史试验结果。

尚未完成：更多负载性能验收；专业交互历史裁剪；其他角色经验上下文；更多场景手册与 v3 接入；可审计预算追加及补查意图实际分派；规划 Agent、多步骤处置、unknown人工收敛及验证失败回流；复杂场景工作台和完整工程版发布。人工等待已支持补充、取消、完整输入准备、独立模型重评意见及未分派后续意图，第127步已接入候选发布执行，第128步增加独立客观验证，第129步完成候选父运行收尾，第130步完成有时效的Incident结果收敛及成功分支活动槽位释放；第131步已有not_dispatched回流准入，held后续新证据处理及实际回流分派仍待交付；graph-v15 已到 END，历史业务审批不等于 LangGraph interrupt/resume。

第135步新增独立回流综合阶段：完整成功专业集合、原132材料、133分派、实际窗口、新证据和精确报告/调用引用进入冻结输入；未完成等待，专业失败或缺证据时阻断。独立尝试在原账本预留synthesize_reinvestigation，结果固定pending_review，成功调用复用及unknown占用延续，当前失效或迟到不能发布。新当前预算函数与完整原生来源闭包不改旧历史函数；原父/旧图、预算和审批保持。完整数据库与扫描过滤补验已通过，独立审查仍后置。

第136步新增独立回流审查：真实135成功任务/输入/调用/结果及尝试闭包绑定精确提案、输入和结果摘要，完整原材料、新专业证据、人工约束及未观测区间进入独立角色/提示的冻结上下文。review_reinvestigation在原账本预留，支持引用必须是原提案已引用的支持、反证须来自同轮新证据。结果单独recorded，135仍pending_review历史；成功调用复用、unknown占用和原期限保持，来源改变或失租不发布。双来源、原生保护及恢复验收通过；审查后的路由与新规划审批仍后置。

## 6. 下一步代码导航

| 入口 | 边界 |
| --- | --- |
| [reinvestigation_review/context.py](../src/opsswarm/investigation/reinvestigation_review/context.py)、[source.py](../src/opsswarm/investigation/reinvestigation_review/source.py)、[policy.py](../src/opsswarm/investigation/reinvestigation_review/policy.py)、[job_store.py](../src/opsswarm/investigation/reinvestigation_review/job_store.py)、[ledger.py](../src/opsswarm/investigation/reinvestigation_review/ledger.py)、[0091迁移](../alembic/versions/0091_reinvestigation_review.py) | 独立角色/租约审查、精确综合绑定及有限意见，原预算和旧父保持；已验收 |
| [reinvestigation_synthesis/source.py](../src/opsswarm/investigation/reinvestigation_synthesis/source.py)、[context.py](../src/opsswarm/investigation/reinvestigation_synthesis/context.py)、[job_store.py](../src/opsswarm/investigation/reinvestigation_synthesis/job_store.py)、[ledger.py](../src/opsswarm/investigation/reinvestigation_synthesis/ledger.py)、[0090迁移](../alembic/versions/0090_reinvestigation_synthesis.py) | 完整专业来源闭包、冻结综合输入、独立租约和原预算调用、待审查提案；已验收 |
| [reinvestigation/store.py](../src/opsswarm/investigation/reinvestigation/store.py)、[policy.py](../src/opsswarm/investigation/reinvestigation/policy.py)、[0086迁移](../alembic/versions/0086_reinvestigation_admission.py) | 明确验证失败的唯一可信回流准入、完整原预算绑定及原子业务审计；尚未分派，原父/槽位保持 |
| [convergence/store.py](../src/opsswarm/incidents/convergence/store.py)、[policy.py](../src/opsswarm/incidents/convergence/policy.py)、[queries.py](../src/opsswarm/incidents/convergence/queries.py)、[0085迁移](../alembic/versions/0085_candidate_convergence.py) | 完整来源、样本时效/告警判定、两跳与事件同事务，成功释放槽位；历史不依赖当前事故状态 |
| [settlement/store.py](../src/opsswarm/investigation/settlement/store.py)、[eligibility.py](../src/opsswarm/investigation/settlement/eligibility.py)、[parent_state.py](../src/opsswarm/investigation/settlement/parent_state.py)、[0084迁移](../alembic/versions/0084_candidate_settlement.py) | 唯一精确收尾、SQL资格过滤、进度事件/父终态同事务；兼容历史读取，不关闭Incident或释放槽位 |
| [candidate/store.py](../src/opsswarm/verification/candidate/store.py)、[worker.py](../src/opsswarm/verification/candidate/worker.py)、[probe.py](../src/opsswarm/verification/candidate/probe.py)、[queries.py](../src/opsswarm/verification/candidate/queries.py)、[0083迁移](../alembic/versions/0083_candidate_recovery.py) | 固定窗口与独立尝试、逐次订单业务样本及不可变判断；不结算父链 |
| [release_jobs/store.py](../src/opsswarm/execution/release_jobs/store.py)、[job_store.py](../src/opsswarm/execution/release_jobs/job_store.py)、[worker.py](../src/opsswarm/execution/release_jobs/worker.py)、[client.py](../src/opsswarm/execution/release_jobs/client.py)、[main.py](../src/opsswarm/execution/release_jobs/main.py) | 可信执行任务、当前凭据/来源重验、单次派发与仅GET恢复、不可变材料及结果；不验证健康或结算父链 |
| [fault_lab/release/dispatch_store.py](../src/opsswarm/fault_lab/release/dispatch_store.py)、[dispatch_queries.py](../src/opsswarm/fault_lab/release/dispatch_queries.py)、[dispatch_contracts.py](../src/opsswarm/fault_lab/release/dispatch_contracts.py)、[0081迁移](../alembic/versions/0081_lab_release_dispatch.py) | 远端不可变窗口、实际CAS/取消/回执及原生截止闭环；是隔离管理协议，127控制面Worker已接入 |
| [fault_lab/release/receipts.py](../src/opsswarm/fault_lab/release/receipts.py)、[queries.py](../src/opsswarm/fault_lab/release/queries.py)、[receipt_client.py](../src/opsswarm/execution/release/receipt_client.py)、[reconciliation.py](../src/opsswarm/execution/release/reconciliation.py) | 持久命令稳定键只读回执、严格来源及有界采集，完整CAS纯对账；未知不写，不授予派发权限 |
| [execution/release/store.py](../src/opsswarm/execution/release/store.py)、[queries.py](../src/opsswarm/execution/release/queries.py)、[observer.py](../src/opsswarm/execution/release/observer.py)、[policy.py](../src/opsswarm/execution/release/policy.py)、[contracts.py](../src/opsswarm/execution/release/contracts.py) | 当前核验批准与原步骤、只读演练版本/CAS的不可变prepared请求；独立操作ID、原预算/期限及可信审计；不派发 |
| [fault_lab/release/store.py](../src/opsswarm/fault_lab/release/store.py)、[policy.py](../src/opsswarm/fault_lab/release/policy.py)、[contracts.py](../src/opsswarm/fault_lab/release/contracts.py)、[tables.py](../src/opsswarm/fault_lab/release/tables.py)、[fault_lab/app.py](../src/opsswarm/fault_lab/app.py) | 显式启用的持久模拟版本、修订CAS和不可变命令；旧开关/订单/指标一致，独立于真实镜像身份；未接候选执行 |
| [approval/conditions/store.py](../src/opsswarm/approval/conditions/store.py)、[queries.py](../src/opsswarm/approval/conditions/queries.py)、[sources.py](../src/opsswarm/approval/conditions/sources.py)、[policy.py](../src/opsswarm/approval/conditions/policy.py)、[contracts.py](../src/opsswarm/approval/conditions/contracts.py) | 精确核验版本的独立可信请求/决定、原期限与当前性、负面收敛及不可变审计；批准不执行操作 |
| [execution/conditions/store.py](../src/opsswarm/execution/conditions/store.py)、[queries.py](../src/opsswarm/execution/conditions/queries.py)、[sources.py](../src/opsswarm/execution/conditions/sources.py)、[policy.py](../src/opsswarm/execution/conditions/policy.py)、[contracts.py](../src/opsswarm/execution/conditions/contracts.py) | 固定原文目录、可信完整核验版本及来源/前驱/时效，全部新依据须新审批，不授予执行资格 |
| [execution/candidate/store.py](../src/opsswarm/execution/candidate/store.py)、[observer.py](../src/opsswarm/execution/candidate/observer.py)、[policy.py](../src/opsswarm/execution/candidate/policy.py) | 固定本地GET只读预检和30秒观测，不执行操作，原未核验条件继续阻断 |
| [approval/decision/store.py](../src/opsswarm/approval/decision/store.py)、[queries.py](../src/opsswarm/approval/decision/queries.py)、[policy.py](../src/opsswarm/approval/decision/policy.py)、[tables.py](../src/opsswarm/approval/decision/tables.py) | 可信批准/拒绝/显式过期及不可变决定、事件与审计；独立决定阶段适用性，不授予执行资格或改变父图 |
| [approval/candidate/store.py](../src/opsswarm/approval/candidate/store.py)、[queries.py](../src/opsswarm/approval/candidate/queries.py)、[policy.py](../src/opsswarm/approval/candidate/policy.py)、[tables.py](../src/opsswarm/approval/candidate/tables.py) | 独立待审批请求、精确来源/事件/阶段与原期限，可信原子事务及历史失效；status固定pending，决定另片保存 |
| [planning/candidate/store.py](../src/opsswarm/planning/candidate/store.py)、[queries.py](../src/opsswarm/planning/candidate/queries.py)、[policy.py](../src/opsswarm/planning/candidate/policy.py)、[budget.py](../src/opsswarm/planning/candidate/budget.py)、[tables.py](../src/opsswarm/planning/candidate/tables.py) | 可信草案、精确候选/版本/当前性与原预算核验；独立表隔离旧审批执行，固定不可授权 |
| [candidate/store.py](../src/opsswarm/investigation/candidate/store.py)、[source.py](../src/opsswarm/investigation/candidate/source.py)、[queries.py](../src/opsswarm/investigation/candidate/queries.py)、[evaluation.py](../src/opsswarm/investigation/candidate/evaluation.py)、[job_store.py](../src/opsswarm/investigation/candidate/job_store.py)、[ledger.py](../src/opsswarm/investigation/candidate/ledger.py)、[main.py](../src/opsswarm/investigation/candidate/main.py) | 双来源显式候选命令、固定完整上下文、白名单评估、真实尝试与原预算；产物无执行资格 |
| [persistence/source_validation.py](../src/opsswarm/investigation/persistence/source_validation.py)、[source_revision.py](../src/opsswarm/investigation/persistence/source_revision.py) | 单次来源遍历复用、行修订门禁、ORM 新鲜度、异常/事务失效与有界结果；不跨操作缓存或替代租约 |
| [experience/context_store.py](../src/opsswarm/experience/context_store.py)、[context_contracts.py](../src/opsswarm/experience/context_contracts.py) | 首轮协调经验的临时选择、原子冻结与历史恢复；精确引用不可变确认工件，空选择保持冻结 |
| [experience/replacement_store.py](../src/opsswarm/experience/replacement_store.py)、[replacement_policy.py](../src/opsswarm/experience/replacement_policy.py)、[replacement_validation.py](../src/opsswarm/experience/replacement_validation.py)、[state_reader.py](../src/opsswarm/experience/state_reader.py) | 可信替代、单一前后继、精确双侧工件、历史重放和互斥终态读取；不递归跟随链；首轮消费由独立上下文服务组织 |
| [experience/lifecycle_store.py](../src/opsswarm/experience/lifecycle_store.py)、[lifecycle_validation.py](../src/opsswarm/experience/lifecycle_validation.py)、[search_store.py](../src/opsswarm/experience/search_store.py)、[search_policy.py](../src/opsswarm/experience/search_policy.py) | 当前有效状态、可信停用及业务审计，系统范围内最多三条的检索和并发锁复核；替代由独立仓储组织，消费由 context_store 组织 |
| [experience/review_store.py](../src/opsswarm/experience/review_store.py)、[review_policy.py](../src/opsswarm/experience/review_policy.py)、[review_validation.py](../src/opsswarm/experience/review_validation.py) | 可信审核、精确版本、幂等并发、业务结果及确认工件；不检索经验或注入上下文 |
| [worker/task_graph.py](../src/opsswarm/worker/task_graph.py) | 按固定运行策略装配新旧 Analyst，profile 必须匹配，SDK 重试为 0、n 为 1 |
| [loop/agent.py](../src/opsswarm/investigation/loop/agent.py)、[loop/middleware.py](../src/opsswarm/investigation/loop/middleware.py) | create_agent/ToolStrategy 原生循环及每次请求的预算、返回审计；每个专业尝试独立 middleware |
| [loop/contracts.py](../src/opsswarm/investigation/loop/contracts.py)、[loop/messages.py](../src/opsswarm/investigation/loop/messages.py) | 新输入版本、消息适配与配对，旧 ModelInput 未改变序列化 |
| [loop/context.py](../src/opsswarm/investigation/loop/context.py)、[loop/ledger.py](../src/opsswarm/investigation/loop/ledger.py) | 首轮目录、成功历史、报告读取资格、参数/来源/结果复核；历史重建抽至 loop/history.py 供摘要共用 |
| [identity/api.py](../src/opsswarm/identity/api.py)、[identity/authentication.py](../src/opsswarm/identity/authentication.py)、[identity/store.py](../src/opsswarm/identity/store.py) | 控制面统一认证、纯角色权限、服务端主体注入和独立授权准入审计；兼容模式不能作为可信身份 |
| [runbooks/registry.py](../src/opsswarm/investigation/runbooks/registry.py)、[runbooks/reader.py](../src/opsswarm/investigation/runbooks/reader.py) | Git 手册注册、确定性场景选择、正文编码/大小/散列校验与受限读取；不接受调用方路径 |
| [context/catalog.py](../src/opsswarm/investigation/context/catalog.py)、[context/evidence_reader.py](../src/opsswarm/investigation/context/evidence_reader.py) | 新旧入口共用目录投影；详情仍绑定真实保存的模型 call_id |
| [context/selection.py](../src/opsswarm/investigation/context/selection.py)、[context/reservation.py](../src/opsswarm/investigation/context/reservation.py) | 启动策略选择及预留事务内按版本重建输入，按策略选择旧报告或新摘要综合输入 |
| [budget/store.py](../src/opsswarm/investigation/budget/store.py)、[budget/runtime.py](../src/opsswarm/investigation/budget/runtime.py) | 原调查父租约、原子预留、共享额度、结算幂等与迟到阻断；人工重评由独立阶段入口使用同一账本，旧入口不接受新归属 |
| [working_summary/builder.py](../src/opsswarm/investigation/working_summary/builder.py)、[working_summary/persistence.py](../src/opsswarm/investigation/working_summary/persistence.py) | 摘要确定性投影、同事务保存与查询来源核对；不调用模型 |
| [tasks/runtime.py](../src/opsswarm/investigation/tasks/runtime.py) | 稳定任务、新尝试、快照复用及专业结果恢复；调用方必须显式提供期望轮次，避免跨轮执行 |
| [coordination/contracts.py](../src/opsswarm/investigation/coordination/contracts.py)、[coordination/policy.py](../src/opsswarm/investigation/coordination/policy.py) | 有限协调输入/输出、运行策略及系统范围注入；业务无效提案不能结算为成功 |
| [coordination/context.py](../src/opsswarm/investigation/coordination/context.py)、[coordination/planner.py](../src/opsswarm/investigation/coordination/planner.py) | 首轮最小上下文、根级共享预算调用、成功结果复用与计划提交恢复 |
| [coordination/persistence.py](../src/opsswarm/investigation/coordination/persistence.py)、[coordination/queries.py](../src/opsswarm/investigation/coordination/queries.py) | 策略、计划、业务轮次、父尝试和模型调用来源的原子写入及重建核对 |
| [workflow_versions.py](../src/opsswarm/investigation/workflow_versions.py)、[persistence/repository.py](../src/opsswarm/investigation/persistence/repository.py) | v3 路由、启动策略原子冻结、运行查询及 Worker 支持版本领取过滤 |
| [dynamic_graph.py](../src/opsswarm/investigation/dynamic_graph.py)、[dynamic_first_round_graph.py](../src/opsswarm/investigation/dynamic_first_round_graph.py)、[dynamic_synthesis_graph.py](../src/opsswarm/investigation/dynamic_synthesis_graph.py)、[dynamic_review_graph.py](../src/opsswarm/investigation/dynamic_review_graph.py)、[dynamic_routing_graph.py](../src/opsswarm/investigation/dynamic_routing_graph.py)、[dynamic_follow_up_graph.py](../src/opsswarm/investigation/dynamic_follow_up_graph.py)、[dynamic_second_round_graph.py](../src/opsswarm/investigation/dynamic_second_round_graph.py)、[dynamic_second_round_synthesis_graph.py](../src/opsswarm/investigation/dynamic_second_round_synthesis_graph.py)、[dynamic_second_round_review_graph.py](../src/opsswarm/investigation/dynamic_second_round_review_graph.py)、[dynamic_second_round_routing_graph.py](../src/opsswarm/investigation/dynamic_second_round_routing_graph.py)、[dynamic_third_round_planning_graph.py](../src/opsswarm/investigation/dynamic_third_round_planning_graph.py)、[dynamic_third_round_graph.py](../src/opsswarm/investigation/dynamic_third_round_graph.py)、[dynamic_third_round_synthesis_graph.py](../src/opsswarm/investigation/dynamic_third_round_synthesis_graph.py)、[dynamic_third_round_review_graph.py](../src/opsswarm/investigation/dynamic_third_round_review_graph.py)、[dynamic_third_round_routing_graph.py](../src/opsswarm/investigation/dynamic_third_round_routing_graph.py)、[worker/dynamic_graph.py](../src/opsswarm/worker/dynamic_graph.py) | 图 v1～v14 保留各阶段历史恢复语义；图 v15 保存第三轮停止路由；显式 Worker 持久交接，人工模型重评为独立阶段；后续路由由独立阶段保存，候选执行仍后置 |
| [synthesis/contracts.py](../src/opsswarm/investigation/synthesis/contracts.py)、[synthesis/persistence.py](../src/opsswarm/investigation/synthesis/persistence.py)、[synthesis/service.py](../src/opsswarm/investigation/synthesis/service.py) | 按轮开放假设契约、证据闭包、槽位隔离、成功调用复用、不可变写入和来源重建；不映射旧根因或处置资格 |
| [review/contracts.py](../src/opsswarm/investigation/review/contracts.py)、[review/persistence.py](../src/opsswarm/investigation/review/persistence.py)、[review/service.py](../src/opsswarm/investigation/review/service.py) | 按轮独立审查契约、同源材料、槽位隔离、成功调用复用和不可变事实；建议不创建任务或授予路由与执行资格 |
| [review_routing/contracts.py](../src/opsswarm/investigation/review_routing/contracts.py)、[review_routing/policy.py](../src/opsswarm/investigation/review_routing/policy.py)、[review_routing/persistence.py](../src/opsswarm/investigation/review_routing/persistence.py) | 按轮纯规则路由、有限原因码、冻结容量检查、不可变事实与来源重建；决定尚不执行对应路径 |
| [follow_up_planning/policy.py](../src/opsswarm/investigation/follow_up_planning/policy.py)、[follow_up_planning/service.py](../src/opsswarm/investigation/follow_up_planning/service.py)、[coordination/persistence.py](../src/opsswarm/investigation/coordination/persistence.py) | 完整补查建议的确定性下一轮编译、按来源轮次限制图能力、计划/轮次/任务原子提交与恢复；图 v11 已创建第三轮待执行任务 |
| [worker/processor.py](../src/opsswarm/worker/processor.py)、[worker/task_graph.py](../src/opsswarm/worker/task_graph.py)、[worker/dynamic_processor.py](../src/opsswarm/worker/dynamic_processor.py)、[worker/dynamic_factory.py](../src/opsswarm/worker/dynamic_factory.py) | 保留 v1/v2，显式分派当前冻结 v3、稳定演练快照与 checkpoint 恢复 |
| [handoff/persistence.py](../src/opsswarm/investigation/handoff/persistence.py)、[handoff/tables.py](../src/opsswarm/investigation/handoff/tables.py) | 重建最高轮终端来源、精确幂等交接、父尝试结算与租约释放；等待不授予执行资格 |
| [reinvestigation_execution](../src/opsswarm/investigation/reinvestigation_execution) | 独立执行/真实尝试、新窗口只读取证、完整上下文和原账本调用、专业报告/迟到隔离与可信读取；尚未回流综合审查 |
| [reinvestigation_dispatch](../src/opsswarm/investigation/reinvestigation_dispatch) | 独立业务轮次/pending任务、验证后新窗口、完整原预算与事件游标、短事务整轮分派和可信历史查询；真实专业执行及取证仍后置 |
| [reinvestigation_coordination](../src/opsswarm/investigation/reinvestigation_coordination) | 10类完整来源/上下文、独立阶段与真实租约尝试、原账本预留/结算、成功调用复用与可信历史查询；只保存not_dispatched建议，尚无新专业任务 |
| [human_review/store.py](../src/opsswarm/investigation/human_review/store.py)、[job_store.py](../src/opsswarm/investigation/human_review/job_store.py)、[ledger.py](../src/opsswarm/investigation/human_review/ledger.py)、[model.py](../src/opsswarm/investigation/human_review/model.py)、[queries.py](../src/opsswarm/investigation/human_review/queries.py)、[worker.py](../src/opsswarm/investigation/human_review/worker.py) | 显式重评命令、独立租约、原账本累计额度、完整准备输入消费与持久意见；取消/迟到隔离和成功调用复用；意见不进入候选或执行 |
| [human_routing/policy.py](../src/opsswarm/investigation/human_routing/policy.py)、[persistence.py](../src/opsswarm/investigation/human_routing/persistence.py)、[queries.py](../src/opsswarm/investigation/human_routing/queries.py)、[store.py](../src/opsswarm/investigation/human_routing/store.py) | 原规则、完整约束、原容量与预算确定后续意图；不可变事实、历史前缀、当前诊断及无模型有界补齐；全部尚未分派 |
| [human_prepare/store.py](../src/opsswarm/investigation/human_prepare/store.py)、[job_store.py](../src/opsswarm/investigation/human_prepare/job_store.py)、[source.py](../src/opsswarm/investigation/human_prepare/source.py)、[builder.py](../src/opsswarm/investigation/human_prepare/builder.py)、[queries.py](../src/opsswarm/investigation/human_prepare/queries.py)、[main.py](../src/opsswarm/investigation/human_prepare/main.py) | 可信准备命令、独立租约任务、最高轮来源、完整输入/清单和原预算诊断；CLI/Compose Worker 实际运行纯组装，不执行模型重评 |
| [human_wait/store.py](../src/opsswarm/investigation/human_wait/store.py)、[policy.py](../src/opsswarm/investigation/human_wait/policy.py)、[validation.py](../src/opsswarm/investigation/human_wait/validation.py)、[authorization.py](../src/opsswarm/investigation/human_wait/authorization.py) | 有界可信补充/取消、等待版本、幂等与业务结果审计；重建原始正文/授权/连续事件，取消后历史交接兼容；不消费上下文或创建恢复任务 |
| [telemetry_mcp](../src/opsswarm/telemetry_mcp)、[telemetry_metrics_mcp](../src/opsswarm/telemetry_metrics_mcp) | 日志与指标各自的单工具严格契约、独立 Streamable HTTP 服务、固定端点客户端与双重范围校验；原日志协议保持冻结 |
| [live.py](../src/opsswarm/investigation/live.py)、[live_window.py](../src/opsswarm/investigation/live_window.py) | 按三种持久取证策略选择直连、仅日志 MCP 或日志与指标 MCP；变更保持原适配器且不允许失败回退 |

第 79 步核对了锁定 LangChain 的 middleware、ToolStrategy 与输出确认契约。工具 schema 必须与框架实际绑定一致；升级依赖时通过真实 SDK 假传输测试核对，不能凭记忆改参数。

## 7. 保留的重要决定

LangGraph/LangChain 复用框架循环、调度与 checkpoint，Harness 专注权限、上下文、预算和审计；checkpoint 不替代业务事实。内部 Agent 不使用 A2A，MCP 只用于只读遥测，日志与指标均已迁移。经验从复盘生成候选，经可信人工确认后启用，不是当前事实或执行授权。新能力固定新策略，旧运行不静默换输入和恢复语义。

开发子智能体按独立边界与难度分配；主 Agent 核实关键结论、统一测试和提交。开发 Agent 的资源偏好不改变 OpsSwarm 业务角色的模型或预算。

## 8. 下一功能切片

第137步：**回流审查意见的确定性路由**。消费精确135/136结果及同源完整证据，保留人工约束、未解问题、未观测区间、原轮次/任务/累计预算与停止条件；将审查建议转为可审计的有限后续意图，不能直接当作执行资格。新规划/审批/处置另行绑定新依据，旧批准不能覆盖；任意秒MCP、held/unknown人工处理、终端取消、多步骤和前端继续分片。

第 81 步只让综合消费完成任务摘要，没有裁剪专业交互、动态分派或新增独立反证/未解问题字段。后续若需要历史压缩，应单独定义完整配对替换和必要材料保留规则，不以摘要已被消费推断 Token 已下降。

之后仍按第 64/65 篇推进动态协作 → 只读 MCP → 手册、可信身份与经验 → 人工介入/规划 → 多步骤处置与回流 → 复杂场景和工作台 → CI 与完整发布。

## 9. 验证与环境

使用仓库 `.venv/Scripts/`。第136步验收见[切片记录](progress/136-reinvestigation-review.md)：按同一用例最后结果去重2,624项通过，单元2,613、API4、迁移1及旧链路6。全量单元76.659秒、迁移32.857秒、旧链路57.774秒；人工来源负向断言修正后单独复验451.331秒通过，替代早期同名失败。双来源三专业的独立意见、并发准备单胜、原输入/结果/预算对照、真实30秒失租提交回滚、三次尝试保留unknown及成功调用复用、全面重算摘要后的篡改拒绝通过。Ruff、1,023文件格式、Mypy615源文件、唯一head0091_reflow_review与CLI帮助通过。仅隔离PG、假模型/固定HTTP，不升级用户Compose、调用真实模型或连接生产；无前端变更。

第135步历史验收见[切片记录](progress/135-reinvestigation-synthesis.md)：去重2,604项通过，单元2,592、API5、迁移1及旧链路6；SQL扫描过滤补验2项重叠不计数。完整双来源三专业输入/预算对照、真实30秒租约提交拒绝及三次尝试保留unknown/成功调用复用通过。Ruff、1,005文件格式、Mypy601源文件、唯一head0090_reflow_synthesis及CLI帮助通过。主数据库验收1,078.882秒，补验371.468秒，包含完整前置链，不作为生产性能结论。仅隔离PostgreSQL、假模型与固定HTTP/MCP，未升级用户Compose库、调用真实模型或连接生产。无前端变更。

第134步历史验收见[切片记录](progress/134-reinvestigation-execution.md)：去重2,585项有通过记录，单元2,571、API4、原生4及旧链路6；原生来源补验1项重叠不计数。真实三任务并发预留、SQL/Python预算/packet对照、实际30秒租约提交拒绝及保留unknown接管均通过。Ruff、987文件格式、Mypy587源文件、唯一head0089_reflow_execution、CLI帮助及文档/差异检查通过。仅隔离PostgreSQL17.6、假模型与固定HTTP/MCP传输；未升级用户Compose库、调用真实模型或连接生产。无前端变更。

```powershell
git status --short
git log -5 --oneline
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/ruff.exe check src tests alembic
.venv/Scripts/ruff.exe format --check src tests alembic
.venv/Scripts/mypy.exe src
git diff --check
```

第136步主要入口为[完整独立审查与原生保护](../tests/api/test_reinvestigation_review_api.py)、[迁移一致性及空升降级](../tests/integration/test_reinvestigation_review_migration.py)及[上下文/引用/预算/SDK](../tests/unit/investigation/test_reinvestigation_review.py)。最终报告在.pytest_cache/，output/未读取或提交。

第135步历史入口为[完整综合与原生闭包](../tests/api/test_reinvestigation_synthesis_api.py)、[迁移一致性与空升降级](../tests/integration/test_reinvestigation_synthesis_migration.py)和[输入/预算/SDK](../tests/unit/investigation/test_reinvestigation_synthesis.py)。报告在.pytest_cache/，output/未读取或提交。

第134步历史入口为[API专业执行与恢复](../tests/api/test_reinvestigation_execution_api.py)、[原生来源和迁移](../tests/integration/test_reinvestigation_execution_migration.py)、[并发预留及真实租约](../tests/integration/test_reinvestigation_execution_guards.py)、[材料/SDK/MCP](../tests/unit/investigation/test_reinvestigation_execution_materials.py)及[运行/预算](../tests/unit/investigation/test_reinvestigation_execution_runtime.py)。CLI仅显式启动，不新增默认Compose服务；output/未读取或提交，报告保存在.pytest_cache/。

动态池默认关闭。开启需要 frozen、function_calling 模型，建议处理 240 秒、租约 300 秒、停止宽限 5m；运行建议显式 max_model_calls=16、max_duration_seconds=900 及足够模型预算。旧默认 300 秒总时限不因 Worker 参数变化而延长；具体命令见 README，未升级用户 Compose 数据库。

## 10. 可直接用于继续开发的任务

> 请读AGENTS、项目状态、进度索引和136设计/验收，核对Git及0091_reflow_review。136只消费135成功综合的精确提案、完整原材料/新专业证据、实际窗口和未观测范围；独立角色/提示/真实尝试在原账本以review_reinvestigation和reflow_review_attempt_id预留，单独保存recorded意见，135原pending_review历史不改。接受支持必须来自原提案支持集合，反证限于同轮新证据，三个方向均只是建议；旧父settled/持槽和图保持，原预算与总截止跨全部角色/重试/unknown延续，成功调用可跨新尝试复用，迟到或新告警不能发布。SQL独立重建真实135输入/调用/结果及尝试闭包；等待项在有界扫描前过滤。下一片137接精确意见的确定性路由和有限意图，不直接进入执行或沿用旧审批，不改变旧131～136历史函数的恢复语义。
