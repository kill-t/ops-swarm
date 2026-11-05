# 第二批证据体积分层评测

固定 [RCAEval RE2](https://github.com/phamquiluan/RCAEval) 同一数据修订中的六个新案例，覆盖 CPU、内存、磁盘、网络延迟、丢包和套接字故障。沿用 Online Boutique，案例未参与首批调试，结果用于同一系统内的留出验证。案例与原始文件见[批次清单](batch-02.json)，读取日志和指标。

准备验证通过。12 份原始文件合计 2,923,871 字节，均通过大小与 SHA-256 检查。紧凑版证据为 3,839～4,025 字节，扩展版为 19,359～20,140 字节，均在预设阈值的对应侧。

每个案例生成两份证据。紧凑版沿用首批的日志和指标摘要。扩展版保留相同摘要，为每个指标加入至多 24 个按全观测窗口等宽切分的均值，避免使用注入时间作分割。按需目录继续使用紧凑摘要里的变化线索，工具返回该组证据版本。两组使用同一份遥测和标准答案。

用同一个 DeepSeek Flash 配对比较全量与按需。每个案例在两个证据版本下各跑两组，共 24 次调查，每组一次，先跑紧凑版再跑扩展版，各版本内交替组别先后。输出记录完整证据 JSON 的 UTF-8 字节数、整次调查 Token、时延、请求、完成状态与故障服务定位。

预先固定一个候选分流规则。完整证据少于 8,000 字节走全量，否则走按需。按各案例的配对结果直接计算该规则的成绩，复用对应运行。8,000 字节是试点阈值，是否优于固定策略由结果判断。扩展版增加了时间信息，因此分层差异同时反映材料体积与内容，主要看各层内部的配对差异。

从项目根目录运行，输出文件须不存在：

```powershell
.venv/Scripts/python.exe -u evaluations/rcaeval-re2/run_batch.py .pytest_cache/rcaeval-re2/batch-02-compact.jsonl --manifest evaluations/rcaeval-re2/batch-02.json --profile compact
.venv/Scripts/python.exe -u evaluations/rcaeval-re2/run_batch.py .pytest_cache/rcaeval-re2/batch-02-expanded.jsonl --manifest evaluations/rcaeval-re2/batch-02.json --profile expanded
```
