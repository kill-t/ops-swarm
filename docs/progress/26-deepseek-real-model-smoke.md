# 26 - DeepSeek 真实模型兼容与首次对照

## 目标

在已经具备预算、幂等和失败持久化的真实模型入口上，完成第一次 `deepseek-v4-flash` 单 Agent 与多 Agent 冒烟对照；遇到供应商兼容差异时保留失败事实，通过显式配置修复，而不是把特殊判断写入领域逻辑。

## 设计与边界

- `.env` 使用 DeepSeek 官方 OpenAI 兼容地址，密钥不读取到终端、不写入文档并继续由 Git 忽略。
- 价格采用高峰期缓存未命中的保守上限：输入 3 元、输出 9 元/百万 tokens。按 2026-08-28 人民币汇率中间价换算后向上取整为 0.45 与 1.35 美元，仅用于本地预算估算。
- 模型接口兼容由 `LLM_THINKING_MODE` 和 `LLM_STRUCTURED_OUTPUT_METHOD` 显式配置，不依据模型名称自动猜测。
- DeepSeek 配置关闭默认思考模式，避免强制 `tool_choice` 被拒绝；多 Agent 使用 `json_mode`，提示中包含 Pydantic JSON Schema，返回值仍需通过 Pydantic 与 Evidence ID 校验。
- 第一次失败尝试作为审计事实保留；修复后使用新的实验标识和幂等键，不覆盖、不重放旧尝试。
- 真实模型仍只能访问冻结的日志、指标和变更只读工具，不能执行 Shell、SQL、回滚或其他处置动作。

## 实际改动

- 模型配置新增思考模式和结构化输出方式，并在两个 ChatOpenAI 工厂中统一传递思考参数。
- LangChain 专业分析与综合适配器支持显式选择 `json_schema`、`json_mode` 或 `function_calling`。
- `json_mode` 会把对应 Pydantic JSON Schema 注入请求，避免只要求“输出结构化结果”却没有告诉模型字段契约。
- Worker 与真实评测运行器使用同一兼容配置，防止离线冒烟成功但持久 Worker 行为不同。
- 兼容配置进入模型参数哈希，保证实验身份可比较。
- `.env.example`、README、技术栈和真实模型架构文档同步说明配置边界。

## 测试方法与结果

首次请求成功到达 DeepSeek，但接口拒绝了默认参数：单 Agent 返回“思考模式不支持该 `tool_choice`”，多 Agent 返回“当前不支持该 `response_format` 类型”。两条尝试均以 `model_request_error` 落库，没有被伪造成评测零分，也没有覆盖后续结果。

修复后再次运行同一冻结场景，两种模式均成功：

| 指标 | 单 Agent | 多 Agent |
| --- | ---: | ---: |
| 根因 Top-1 | 1.0 | 1.0 |
| 证据召回 | 1.0 | 1.0 |
| 建议准确率 | 1.0 | 1.0 |
| 安全违规率 | 0.0 | 0.0 |
| 工具调用数 | 3 | 3 |
| 模型请求数 | 2 | 4 |
| 总 Tokens | 3144 | 3712 |
| 估算费用（美元） | 0.001914 | 0.002300 |
| 调查耗时 | 约 9.26 秒 | 约 33.21 秒 |

两种模式都判断根因为 `release_500_regression`，建议 `rollback_release`，引用三条真实返回证据，并把处置标记为必须人工审批。当前简单场景中，多 Agent 没有提升准确率，却增加了两次模型请求、568 tokens、约 0.000386 美元和约 23.95 秒耗时，这正是项目坚持先做单 Agent 基线的价值。

已执行定向验证：

```powershell
uv run ruff check src tests alembic
uv run mypy
uv run pytest tests/unit/investigation/test_settings.py tests/unit/investigation/test_parallel_graph.py tests/unit/evaluation/test_real_model.py tests/unit/evaluation/test_real_main.py -q
uv run pytest -q
uv run alembic check
docker compose config --quiet
git diff --check
```

Ruff 通过，mypy 检查 105 个源码文件无错误，定向测试与完整后端测试全部通过。Alembic 未检测到 ORM 与迁移漂移，Compose 配置和 Git 空白错误检查通过。上述自动化测试使用假模型，没有再次调用外部 API。

## 限制与下一步

- 只有一个简单场景的一次运行，不能据此宣称多 Agent 整体优于或劣于单 Agent。
- 美元费用由人民币公开价和当日中间价换算，是保守估算，不等于 DeepSeek 最终账单。
- JSON Output 官方说明存在偶发空内容风险；当前会把此类结果归入结构化输出失败，后续多次重复评测才能量化可靠性。
- 下一步应在三个冻结故障场景上进行多次交替顺序运行，汇总准确率、成功率、耗时和成本的均值与方差。

## 面试表达

第一次真实模型调用没有被我当成“改个参数再试”，而是完整保留为失败尝试。问题来自供应商兼容差异：DeepSeek V4 默认思考模式与强制工具选择不兼容，Chat Completions 只支持普通 JSON Output。我的解决方式是把思考模式和结构化输出策略做成显式基础设施配置，并纳入实验哈希；`json_mode` 只改变协议适配，Pydantic、证据引用校验和人工审批护栏仍然有效。修复后的真实对照还显示，在简单场景中多 Agent 准确率没有提升但成本和耗时更高，因此系统是否升级为多 Agent 应由评测数据决定。
