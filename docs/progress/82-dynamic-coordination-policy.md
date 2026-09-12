# 82 - 动态调查计划与确定性分派校验

## 目标

在模型规划进入任务表之前建立框架无关、可单测的确定性策略，防止模型扩大服务、角色、时间窗、重试和任务额度。起点为 main `e744dd2`，工作区干净。

## 设计与边界

见 [专项设计](../architecture/82-dynamic-coordination-policy.md)。模型提案与系统编译结果使用独立 Pydantic 契约；任务执行仍只接受既有 `RoundAssignment`。本片没有模型调用、迁移、API 或图路由变化。

## 实际改动

- 新增版本化协调策略、有限提案任务、协调提案和编译计划契约。
- 编译器注入事故服务、UTC 截止时间、快照身份和尝试上限，并检查轮次、角色、窗口及运行总任务额度。
- 首版限制每轮每个专业最多一个任务，任务键固定为角色值；不开放通用 Agent 创建或任意工具权限。

## 测试方法与结果

验收日期：2026-09-12。12 项定向单元测试通过；Ruff 全仓检查和 607 个文件格式检查通过，mypy 检查 245 个源码文件无问题。7 篇更新文档的 UTF-8、代码围栏和 296 个本地链接检查通过，`git diff --check` 通过。该切片没有数据库或 API 变化，因此不启动 PostgreSQL，也不虚报集成验证。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit/investigation/test_coordination_policy.py --tb=short
.venv/Scripts/ruff.exe check .
.venv/Scripts/ruff.exe format --check .
.venv/Scripts/mypy.exe src
git diff --check
```

## 限制与下一步

提案尚未持久化，也没有来自模型。下一片固定运行级协调策略，并把提案与编译结果作为不可变轮次来源保存和查询。

## 面试表达

> 我没有让协调模型直接写任务表，而是把它的选择缩成有限提案。确定性编译器注入服务、时间、快照和重试上限，同时检查轮次与总任务额度；只有编译后的既有任务契约才能进入持久化。这让动态路径可控，也保留了模型决定调查重点的空间。
