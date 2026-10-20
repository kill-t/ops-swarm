# 107 - 有效经验状态、停用与有界确定性检索

## 状态与边界

第 106 步的不可变确认版本，是有效性投影 active/state_version=1 的持久依据。唯一停用事实把当前投影单向推进为 disabled/state_version=2；候选、审核和确认正文均不改写。原详情的 confirmed 表示历史人工确认，不等于当前 active。首版不支持重新启用、版本替代或模型上下文消费。

## 停用与审计

- `POST /api/v1/experience-entries/{entry_id}/actions/disable`：严格命令包含 idempotency_key、expected_entry_version、expected_entry_content_hash、expected_state_version、reason。幂等键沿用 8～100 位 ASCII 字母数字及 `._:-`，原因 1～1000 字符。只有可信工程师使用 manage_report 可执行，服务端注入主体与授权；legacy 拒绝。
- `GET /api/v1/experience-entries/{entry_id}/state`：返回当前 active/disabled、有效性版本、精确正文身份及可选停用事实；原确认详情仍可读取。

首次停用 201，同主体同键同规范化完整命令重放 200，不替换首次主体或授权。同键异参、错误正文版本/hash、错误预期状态版本及其他重复停用请求均 409；未知条目 404。失败请求不预占业务键。停用不重新审查现场来源，必须能撤销已经失去适用性的历史材料；仍复核冻结工件、可信确认和不可变引用。

以 entry 行排他锁串行化停用，共享锁读取状态。权限检查先于幂等；停用事实及成功业务结果同事务保存，业务校验失败使用保存点回滚半成品后提交错误结果，数据库异常整体回滚。404/409 业务结果独立保存，授权准入不等于业务成功。

迁移 `0064_experience_lifecycle` 新增不可变 `experience_disablements` 和 `experience_lifecycle_commands`。停用事实以 entry 唯一、精确正文/来源复合外键绑定版本，保存 state_version=2、experience-lifecycle-v1、可信主体、首次授权、幂等键、完整命令 hash、原因及 UTC 时间。命令结果保存授权、目标（允许不存在）、规范命令/hash、主体、applied/replayed/error、有限错误码、HTTP 状态、可选停用引用。两表禁止 UPDATE/DELETE，有新事实拒绝降级；原 entry 只增加复合唯一约束和检索范围表达式索引，不回填正文。

## 有界检索

`GET /api/v1/investigations/{run_id}/experience-entries` 始终要求可信 Bearer 和 read 权限。服务由 run 关联 Incident 决定，环境固定 local_fault_lab，调查 frozen/live 取持久 run；调用方不能覆盖范围，未知查询字段拒绝。可选 preferred_fault_tag 是已有 RootCause 枚举的排序偏好，不是当前事故根因或执行资格。

固定 experience-search-v1：只查询同服务/环境/来源模式的确认版本，排除同一事故和已停用记录；偏好标签命中优先，随后确认时间倒序、entry UUID 升序。来源事故始终取不可变生成任务的 source 快照，不沿用后来可能变化的报告关联。数据库先过滤和 LIMIT 3，再复核完整冻结候选/审核/确认工件和实际范围，不先加载全库正文后裁剪。没有结构化依赖字段，首版不宣称完成依赖匹配。

检索持有选中 entry 的共享锁，拿锁后再以新语句复查停用事实，避免反连接查询等待锁后仍沿用旧子表快照。并发停用导致候选被剔除时不无界补查，结果可少于三条；坏工件失败关闭，不回退混入不匹配或冻结材料。返回检索时间、策略、系统范围，以及最多三条摘要、来源事故、正文版本/hash、来源 hash、状态版本和反证提示；原始细节按不可变版本读取。摘要去掉生成时待审尾句，单独标明人工确认，不改写原工件。

结果只表示此次查询复核时的有效集合；停用不会抹去历史引用，但后续检索必须排除。尚未持久化模型上下文材料清单，也没有工具或图接入，不能声称同一 run 的后续查询固定或模型已经消费经验。

## 验收

真实 PostgreSQL 验证精确版本、主体与 legacy、幂等/并发停用、业务和数据库故障原子性、不可变事实及降级。检索覆盖排序与上限、跨事故/服务/来源隔离、未确认/拒绝排除、停用后排除、历史详情可追溯，以及查询等待停用锁后的重新复核。假调查结果与固定探针不作为现场证据，不调用外部模型。
