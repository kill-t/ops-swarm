# 106 - 可信工程师经验审核、确认与拒绝

## 目标与状态

从第 105 步的不可变候选建立可信人工确认边界。候选正文、来源、版本及生成时 `pending_review` 状态保持原样；独立审核投影从待审版本 0 单向进入版本 1 的 confirmed 或 rejected。只有可信工程师可提交，复用目前仅工程师拥有的 `manage_report` 权限；处置审批者不因拥有 `decide_approval` 而获得经验审核权。

确认与不可变经验版本 1 在同一事务提交，拒绝不产生经验版本。经验正文使用独立 `confirmed-experience-v1` 外层契约，明确区分原候选快照与当前人工确认事实；原快照中“尚未经过经验审核”描述的是生成当时，不能作为当前审核状态。确认不证明根因、不改变调查来源模式，也不继承历史审批或执行授权。

本片只提供确认、拒绝及精确身份查询，不提供修订、启停、替代、列表检索或模型上下文消费。来源仍为现有主假设及单步骤复盘契约。

## 接口与幂等

- `POST /api/v1/experience-candidates/{candidate_id}/reviews`：严格字段 `idempotency_key`、`expected_candidate_version`、`expected_candidate_content_hash`、`expected_review_version`、`decision`（confirm/reject）与 `reason`。幂等键 8～100 字符，原因 1～1000 字符；身份、正文、策略及结果由服务端生成。首次 201，原请求重放 200。
- `GET /api/v1/experience-candidates/{candidate_id}/review`：返回候选身份/散列、当前审核版本和 pending_review/confirmed/rejected、可选决定及经验 ID。不存在候选为 404，尚未审核是正常待审结果。
- `GET /api/v1/experience-entries/{entry_id}`：读取精确已确认版本、人工确认及来源引用。

三个入口始终要求 Bearer，legacy 不放行；读接口要求 read。认证与授权准入先于业务和幂等读取。幂等键按候选及主体作用域解释，成功决定绑定完整规范化命令；同主体同键同命令返回原决定，不覆盖原审核人或原授权 ID。同键异参为幂等冲突，其他已决定请求为重复审核冲突。未成功的请求不预占业务幂等键。未知候选、错误候选版本/散列、错误预期审核版本均拒绝；终态不得反转。

## 来源、事务与审计

事务锁定候选，复核成功生成任务、不可变候选/来源散列和确定性投影。首次决定还重建精确源记录，漂移即拒绝；幂等重放只复核已冻结工件及原决定，不重新批准或改写历史。锁内读取当前决定，比较命令，确认时原子创建决定及经验；唯一约束兜底单候选单决定。查询也以候选共享锁读取一致审核投影。

迁移 `0063_trusted_experience_review` 增加三表：

1. `experience_reviews`：不可变唯一决定，绑定候选 ID/版本/内容 hash/来源 hash、review_version=1、experience-review-v1、confirm/reject、可信主体、首次授权、幂等键、规范命令 hash、原因及 UTC 时间。复合外键绑定精确候选版本。
2. `experience_entries`：不可变版本 1、confirmed、精确候选及确认引用、独立确认内容 JSON/hash 与时间；复合外键只允许绑定 confirm 决定。
3. `experience_review_commands`：每个授权准入关联一次不可变业务结果，保存目标候选（允许不存在）、规范命令及 hash、可信主体、applied/replayed/error、有限错误码、HTTP 状态、可选决定与 UTC 时间。409/404 来源及状态拒绝也提交结果后再由 HTTP 返回错误。

授权准入不是业务成功。认证失败和结构无效请求不进入审核业务结果；未分类数据库故障整体回滚，原授权准入仍在，但不能宣称已保存业务失败。决定、经验及命令结果禁止 UPDATE/DELETE；有任何新事实时拒绝降级。迁移只给原候选增加精确版本复合唯一键，不回填或改写旧正文。

## 验收

使用假调查结果与固定探针的完整复盘及真实 PostgreSQL，验证角色/legacy/冒名拒绝，精确版本与散列，重复及异参，确认/拒绝竞争，主体隔离，来源漂移，原子事务失败，不可变事实、复合外键及降级保护。并发冲突检查独立业务结果审计，不把授权准入当作结果。静态检查、当前切片测试和差异检查通过后才更新推进位置并独立提交。
