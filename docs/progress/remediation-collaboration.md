# 协作台失效核验续接与演示预算基线

本批修复准备过期、核验审批拒绝后页面没有合法后续操作的问题。使用现有 ConditionState.availability 和不可变版本接口，不新增业务 API、不改后端执行资格。原草案批准与候选仍有效、未创建执行任务时，允许绑定精确前驱摘要重新核验，再申请新审批；八版本上限、原依据失效及计划需修改时展示明确接管原因。已有执行任务即使旧依据后来过期也继续展示原任务和客观恢复，不产生新派发或重核验按钮。

演示调用预算的成功路径推导、重试和工具循环边界见[预算基线](../architecture/local-demo-budget-baseline.md)。保留正式入口20次模型、18次工具、1800秒总期限及所有历史快照，未用理论最大值乘法拒绝小预算运行。推导不是正式Compose实测，三场景账本对照仍待发布验收。

`npm --prefix web test -- src/services/collaboration.test.ts src/components/TrustedCollaborationPanel.test.ts` 通过 8 项，覆盖精确前驱、拒绝、额度上限、原批准过期和已执行事实保留。`npm --prefix web run typecheck` 通过。隔离 PostgreSQL 的 `test_revisions_supersede_approval_and_block_non_ready_requests` 通过，核验更新后旧审批失效。完整浏览器场景待验收。

面试表达：把审批和核验当作不可变版本事实，页面只组织后端已支持的合法续接，不靠延长有效期或重用旧批准消除操作死路。
