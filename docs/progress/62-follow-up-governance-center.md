# 62 - 跨事故改进行动项治理中心

## 目标

为负责人和研发主管提供跨事故未完成行动项入口，按服务器时间识别逾期任务并按负责人精确筛选，同时保持完成动作只能在具体事故复盘中提交。

## 设计与边界

- 全局 API 固定只返回 `open` 行动项，不提供批量写操作。
- 逾期由服务器 UTC 时钟判定，响应回显 `as_of`。
- 聚合读取行动项、冻结报告和 Incident 关系，不解析报告 JSONB。
- 新增两个仅覆盖 `open` 的部分索引，分别服务全局截止时间和负责人查询。
- 前端核对服务端筛选回显，并用请求代次隔离迟到响应。
- 治理卡片只提供“返回事故复盘”，不能直接完成或重新分配。

## 实际改动

- 新增跨事故治理领域投影和仓储查询。
- 新增 `GET /api/v1/report-action-items` 及响应模型。
- 新增第 29 个 Alembic 迁移，为全局 open 查询建立部分索引。
- 新增治理 TypeScript 契约、API 客户端和独立 Pinia store。
- 新增“改进治理”主导航、筛选页面和事故来源卡片。
- 更新数据库、API、前端架构和项目入口文档。

## 测试方法与结果

```powershell
uv run ruff check src/opsswarm/follow_up src/opsswarm/control_plane/schemas.py src/opsswarm/control_plane/routes.py alembic/versions/20260904_0029_follow_up_governance_idx.py tests/api/test_control_plane_api.py tests/api/test_control_plane_openapi.py
uv run mypy src/opsswarm/follow_up src/opsswarm/control_plane/schemas.py src/opsswarm/control_plane/routes.py
uv run pytest tests/api/test_control_plane_api.py -k "governance" -q
uv run pytest tests/api/test_control_plane_openapi.py -q
uv run pytest tests/integration/test_migrations.py -q

cd web
npm run typecheck
npm test -- --run src/services/api.test.ts src/stores/follow-up-governance-store.test.ts src/components/FollowUpGovernanceList.test.ts
npm run build
```

后端 Ruff、mypy、OpenAPI 和真实 PostgreSQL 治理接口测试通过，迁移升降级测试 `3 passed`；前端类型检查与生产构建通过，`3` 个定向文件共 `22 passed`。覆盖两个事故的稳定排序、服务器逾期判定、负责人筛选、空白负责人拒绝、URL 查询参数、筛选回显校验、迟到响应隔离、事故返回入口和只读空状态。

项目常驻 PostgreSQL 已无损升级到 `0029_follow_up_governance_idx`，并核对两个部分索引均已建立，数据库健康检查保持通过。

本切片只运行相关后端、迁移和前端测试，不重复执行完整测试集。

## 限制与下一步

- 当前只读列表最多返回 100 项，尚无复合游标分页。
- 没有认证人员目录、团队权限和负责人自动补全。
- 没有逾期提醒、SLA 分级或外部工单同步。
- 下一步建议做一次 MVP 发布验收：运行完整测试、启动 Compose 主链路并整理项目演示脚本，而不是继续扩展业务功能。

## 面试表达

> 跨事故查询改变了数据库访问路径，所以我不仅加了页面，还补了与查询一致的 open 部分索引。服务端用 UTC 观察时间计算逾期并回显筛选，前端对响应条件做二次核对。治理入口保持只读，完成动作必须回到具体事故使用当前版本提交，从而兼顾管理视角和审计安全。
