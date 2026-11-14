# 可信材料、用量与迁移基线整改

2026-11-14。通用模型输入、上下文清单和交互接口采用与专属接口相同的可信阶段边界。兼容模式仍可公开读取历史调查材料，人工重评、候选评估和回流材料由可信读取权限保护。Worker 按供应商原始字段累计已知 Token，输出字段缺失时标记用量不完整并保留未知费用。同一请求的多个候选按一份请求级用量累计。见[身份设计](../architecture/104-trusted-operator-identity.md)和[尝试审计](../architecture/43-investigation-attempt-audit.md)。

迁移基线从 Alembic 读取当前唯一 head。真实 PostgreSQL 的元数据一致性、空库往返、历史事实降级保护和 `0029` 带数据升级共四项通过。带数据用例在隔离容器构造事故、告警及时间线，核对升级后身份、正文和时间等字段。原业务卷未参与测试。

`python -m pytest tests/unit/identity tests/unit/investigation/test_worker_usage.py` 覆盖可信读取、历史兼容、未知用量、重复候选和迟到回调。迁移入口为 `python -m pytest tests/integration/test_migrations.py tests/integration/test_legacy_data_upgrade.py`。Ruff 和全源码 Mypy 通过。

面试表达：同一份可信材料可能有多个查询入口，权限由材料所属阶段决定。用量完整性由供应商报告决定，费用才具有可解释性。
