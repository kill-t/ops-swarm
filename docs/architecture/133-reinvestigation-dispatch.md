# 回流协调建议的确定性任务编译与持久分派

基线5572e70。第132步只交付not_dispatched建议；本片自动消费成功协调结果，在原运行范围创建一次完整业务轮次及待执行专业任务，不调用模型、遥测或处置，不重开原图。

## 历史兼容与持久化

原handoff按原表最高轮核验，132按原表实时max/count重建冻结来源。向原表追加会使历史来源失效，因此新增investigation_reflow_dispatches及investigation_reflow_task_queue，复用RoundAssignment/TaskAssignment契约与原run外键，明确逻辑下一轮号、原累计任务基数和原剩余容量。不是新run或新额度；原轮次、任务、父settled/持槽、图/checkpoint及128～132保持。独立执行策略reinvestigation-task-v1作为未来Worker能力边界，本片只有pending业务任务，不伪造执行尝试或借用已结束父租约。

每根运行一个分派事实，queued或blocked，正文/hash不可变。queued含稳定round_id及完整RoundAssignment，任务稳定ID使用sha256(run UUID文本+冒号+版本化名称)前128位；名称分别为reflow-round-v1和reflow-task-v1:{task_key}。任务精确绑定run/round/来源，初始pending、完整assignment/hash/created_at；整轮与全部任务同事务保存，同一SAVEPOINT成套创建也支持；不能缺失、追加、篡改或跨事务补齐。有数据禁止降级。blocked无轮次或任务，保存首个确定性原因，避免失效来源反复占队首。

## 确定性编译与预算

重验132完整结果/成功调用、冻结source/packet及131→130来源，原轮次/任务容量；服务由Incident注入，模型不能指定权限/服务。任务键沿用132合法自定义键，不调用旧首轮编译器要求key等于role的规则。每角色最多一个，log/metric回看最多900秒、change最多21600秒；越界明确blocked，不裁剪用户目标。

取证下界为失败恢复结果created_at，上界为分派created_at；各任务window_start=max(失败结果时刻,分派时刻-lookback)，严格正窗。snapshot为None，证据策略post-verification-read-only-v1要求后续只读重新采集，不复用旧冻结样本、不把旧材料当新证据。本片没有采集现场证据。

分派冻结当时Incident事件游标basis_event_cursor，历史依据按该游标重建状态/版本及130之后新告警，不按当前事件重写历史；当前资格另行检查。

在途调用仍可结算，不能将reserved状态永久冻结进历史前缀。发现任何reserved时暂不消费并有界扫描其他来源，待结算后再处理；数据库也拒绝持久化含reserved的快照。

分派冻结完整当前原账本快照，原策略/started_at/deadline保持，未知用量继续占用。最低容量按每任务一次完整单次上限模型和一次工具检查，原总/每任务工具上限、次数、输入/输出及费用都保留；这是最低就绪诊断，不是预留或整个调查的预算保证。后续实际调用必须使用独立真实尝试在原账本预留。本片不追加或消耗预算。

## 事务与接口

短事务自动发现132成功且未消费的来源。父work锁使用SKIP LOCKED，再锁132job、原candidate和Incident，重建来源后确定queued/blocked。并发只创建一次；事务失败整体回滚，重试生成相同身份。提交前检查真实原截止与当前事故/告警；失效不得保留queued任务。

新增可信Bearer/read的GET /api/v1/investigations/{run_id}/reinvestigation-dispatch，只读不可变record/tasks及当前pending/current/stale/expired/blocked诊断，GET不创建任务。历史按捕获账本前缀重建；当前变更不重写历史。显式CLI --once/--continuous运行纯事务消费者，默认Compose不启用。原生约束独立重建完整source、预算、编译、真实来源归属、成套任务及同事务写入；不修改旧0087与历史函数。

## 验收与后续

单元验证新窗口、角色上限、原轮次/任务容量、预算、未知用量和稳定身份；真实PostgreSQL验证双来源、并发/重试、完整旧历史可读、零调用/预算不变、来源失效/超期/损坏、伪造或缺失任务、跨事务补齐、提交时失效和有数据禁降级。完成静态检查、进度文档与独立提交后标记验收。下一片建立真实专业执行尝试/新证据采集与原账本预留，仍不沿用原批准执行新动作。
