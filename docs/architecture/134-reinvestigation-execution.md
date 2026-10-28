# 独立回流专业执行与原账本调用

基线fcd230b。本片消费133的真实pending任务，建立独立执行投影/真实尝试/租约，在新窗口只读取证并保存完整快照，组装包含132原材料/人工约束的专业上下文，在原账本预留后发起受限的结构化SpecialistReport调用，原子保存独立专业结果。没有新run、旧图重开、综合审查或处置，133分派及原队列继续保持不可变pending历史，134查询表达实际进度。

## 版本、窗口与取证

执行固定reinvestigation-task-v1，取证固定reflow-live-mcp-v1。原133请求窗口带微秒且可能不足整分钟，旧MCP v1/ObservationWindow契约保持：requested_start向上取整秒，requested_end向下取整秒；log/metric再按完整分钟向内收缩start，至少60秒；change只取整秒正窗。记录requested/effective及调整原因，实际窗口必须包含于原分派窗口并处于失败验证之后；不足则blocked、零调用，不外扩、不补旧证据。内收未观测区间必须进入模型输入，不能声称覆盖完整原请求范围；精确任意秒MCP协议是后续能力。

日志、指标分别复用固定本地MCP异步客户端，change复用本地只读窗口适配器并通过ReadOnlyQueryExecutor执行，网络不在数据库事务中。并发最多3个同根执行任务，阻塞查询超时后底层仍占槽。空结果可作为如实缺证据快照，来源失败必须失败关闭，不回退冻结来源。快照复用TaskEvidenceSnapshot live契约，限定20条、128KiB、精确角色/服务/来源/window，禁止模型引用未采集证据。旧恢复探针只是上下文历史，新证据保留实际采集时间及所属真实任务尝试。

## 持久化与恢复

新增investigation_reflow_task_work、attempts、evidence、packets、results五表。work按133 task.id唯一创建，冻结dispatch/assignment摘要、版本、effective_window；queued/running/succeeded/blocked/failed，最多原max_attempts(<=3)，租约默认180秒/最多300秒。尝试有新UUID/token，稳定任务身份不变。原队列不修改；未来执行资格通过独立投影而非133最低容量availability判断。

成功取证及快照同事务保存，完整ModelInput/分配/manifest另存不可变packet；快照与成功模型调用可以跨新尝试复用，外部调用不因结果保存前崩溃重复。失联预留记unknown且保留分配，租约失效/原截止/事故或告警变化后的迟到结果不发布。模型只生成专业发现/证据引用，不强制根因枚举，没有工具写权限或授权；一次预取证+结构化调用不重复实现框架工具循环。

## 原账本与身份

账本新增collect_reinvestigation(tool)、analyze_reinvestigation(model)，以及reflow_task_id/reflow_task_attempt_id，精确绑定新work/真实尝试及run。旧task_id/task_attempt_id为空，原investigation_attempt_id仅引用原132的父来源，不代表旧租约有效。旧通用预留/结算/收尾不能操作新owner；handoff仅按真实133/134归属窄JOIN允许当前在途历史读取，不递归完整来源。

所有角色、重试及历史调用共享原policy/model_budget/started_at/deadline。预留前父work锁串行重算并预留；并行预留全部占预算，不对自己的已有预留再次要求一整次容量。每任务工具计数使用旧task_id或新reflow_task_id的逻辑任务身份，不能把不同新专业合并成None。未知usage保持None并保守占用，超预留实际usage只能保留审计，不能发布成功报告。新调用严格晚于133捕获账本时刻，原历史前缀不改；新当前SQL预算函数认识新操作/归属，旧131/132历史函数保持。

## 当前资格与事务

复用133完整历史重建及132当前事故/告警/父settled持槽/原截止规则，但不使用133 availability作执行中门槛（它会对自身reserved及全部任务最低容量自我阻断）。独立阶段按真实单次增量及累计预算判定。锁顺序父work→134 work→132 job/原candidate→Incident；领取SKIP LOCKED，有界扫描，原始material/manifest/packet/result完整闭包原生重建。成功事实、当前尝试及work终态同事务，时间/租约提交前实际检查；任何失败整体回滚。历史读不要求当前资格仍有效。

CLI显式--once/--continuous，模型配置须匹配原冻结profile，不默认加入Compose。可信GET /api/v1/investigations/{run_id}/reinvestigation-execution读取至多3个任务的真实进度、快照/输入/结果；旧调用三材料接口支持新manifest/模型结果且始终Bearer/read，legacy_local拒绝公开。GET不执行采集/模型。未连接生产或真实供应商作为CI前提。

## 验收

真实隔离PostgreSQL及假模型/固定HTTP/MCP传输验证双来源、三角色窗口/引用、并发预留/原累计限额/每任务工具上限、当前与历史分离、成功取证/模型复用、失租/超时/新告警/原截止和迟到隔离、伪造owner/packet/引用/结果、同事务与有数据禁降级。必要上下文超预算明确停止；未观测原窗口区间保留。通过测试、静态、文档与独立提交后才验收。下一步消费专业结果进行有界综合与独立审查；新动作重新规划审批。
