# 可信人工重评输入准备与就绪诊断

## 目标与交付边界

第 112 步保存人工材料，但没有可供重评复核的完整输入。本片用可信 prepare 命令创建独立持久任务，由独立 Worker 实际重建最高终端轮次的原审查输入、原审查/路由及全部人工材料，生成完整 ModelInput、材料清单和原预算的就绪诊断。工程师可以核对约束是否被保留以及哪些预算限制阻止继续。

这是恢复的输入准备阶段，不是恢复执行。旧 graph-v15 已到 END，原 work/attempt、run 状态、固定策略、checkpoint、调用账本和 started_at/deadline 均不修改。现有预算事务强依赖原 running 父租约，调用归属和上下文也只接受旧角色与轮次；模型重评必须在后续为同一共享账本增加明确的新阶段/尝试归属，不能伪装旧父尝试或建立第二套额度。本片不发送外部模型请求，不预留或追加额度，不产生新审查结论、候选资格或执行授权。

本地入口为 `python -m opsswarm.investigation.human_prepare.main [--once]`，Compose 提供实际装配的 human-preparation-worker 服务，仅接收业务数据库配置，不持有模型密钥、checkpoint 配置或故障环境写权限。

## 准入、版本与持久任务

新增可信工程师 POST /api/v1/investigations/{run_id}/human-wait/preparations，命令绑定 handoff_id、source_sha256、expected_wait_version、idempotency_key 和 reason。至少有一次补充且当前尚未取消，才可创建。每运行每等待版本最多一个任务，因此每运行最多 15 个；相同主体、键与完整规范命令重放原任务，即使后来补充或取消也可查询历史。不同命令或同版本重复创建冲突，404/409 保存独立业务结果，授权原始正文与规范命令绑定。

任务固定 human-input-preparation-v1，queued/running/succeeded/failed；最多三次尝试，每次独立租约/尝试身份。Worker 只领取自身支持版本；过期尝试显式结算 lease_expired，新租约可重做纯组装，迟到结果拒绝。准备发现输入已变化或取消则失败，不能覆盖新事实；基础设施失败有限重试。原父工作项→准备任务是统一加锁顺序，短事务核验后释放锁，不持锁执行组装。

保存不可变来源快照、主体/授权/命令绑定、尝试事实、成功工件和逐请求结果。来源含完整原审查输入/清单及精确原审查、路由、交接和人工事件前缀；查询重建并比较，不把准备后的补充误判为历史损坏。原授权来源仍重新校验，不能假设可变授权表不可变。

## 输入与预算语义

新提示 human-input-review-preparation-v1 将原审查 user 材料作为有来源的历史调查材料，完整保留反证、失败查询、未解问题及原始证据。追加全部人工 statement/constraint/question，保留事件、主体、提交与声明观察时间；人工陈述不是工具证据，人工正文不能覆盖系统规则。复用现有 HypothesisReviewProposal 输出 Schema 作为待核对输入的明确用途，不修改旧审查提示或输入版本。

清单单独固定版本、来源引用/散列、完整输入散列、无工具及无裁剪。复用 allocate_input、require_capacity、require_model_capacity 和 remaining_time；诊断包含原账本快照、检查时刻、分配估算及 ready/blocked 原因。必要材料超窗或过大时明确 blocked，不截掉约束以得到 ready；不能构造合法 ModelInput 时保留完整来源与阻断原因。

ready 只表示当时诊断通过，不是预留或调用授权。GET /api/v1/investigations/{run_id}/human-wait/preparations/{job_id} 返回任务/尝试、历史工件及当前可用性；新补充、取消、deadline 或预算变化可使历史工件 stale/blocked。后续实际消费必须重新核对精确输入版本、阶段、租约与同运行全部累计预算。

## 失败与迁移

未知运行/任务 404；等待版本、来源、幂等或状态冲突 409；无可信身份 401、越权 403、非法正文 422、正文超过 64 KiB 为 413。等锁后及提交前复核凭据或租约，过期整体回滚。成功工件与成功任务/尝试原子提交；历史内容不可更新删除。新迁移 0069 增加准备任务、尝试、工件和命令审计，旧迁移及旧生命周期不修改；有准备事实时拒绝降级，空表可往返。
