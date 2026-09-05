# 64 - 完整多 Agent 工程版目标、基础约束与开发路线

## 目标

把下一阶段建设完整多 Agent 项目的决定落入仓库，明确区别于 RAG 项目的技术定位、六阶段开发顺序和基础约束，并把代码文件的单一职责作为后续切片的检查要求。

## 设计与边界

- `AGENTS.md` 保存长期方向、协作规则和职责约束；架构文档保存详细路线与验收；本记录只保存本次文档决策和验证结果。
- 多 Agent 是既定架构方向，质量评测继续保留，但不再以单、多 Agent 收益对比决定是否开发或保留多 Agent。
- 保留 MVP 成果、单 Agent 路径及历史评测，本次不实现动态协作、不调整 API 或数据库、不调用模型。
- 完整工程版继续限定本地隔离演练环境，保持模块化单体与独立 Worker、确定性授权和白名单执行边界。
- 文件按内聚职责拆分；路由、领域规则、应用服务、仓储、模型与工具适配、图装配和前端状态分别承担明确责任，不采用机械行数或一文件一函数规则。

## 实际改动

- 新增完整工程版架构规划，描述当前能力、六个逻辑角色、目标流程、基础约束、六阶段验收与下一批切片。
- 在 `AGENTS.md` 固定多 Agent 方向、文件单一职责、协作预算、证据、人工等待及恢复兼容要求。
- 更新项目章程，替换旧的多 Agent 收益准入要求，增加完整工程版目标和协作质量指标。
- 系统架构明确当前固定图、业务审批与待开发动态循环、人工中断的区别，并标记网关为规划组件。
- 更新 README 和进度索引，明确第 64 步为文档决策，新增功能尚未实现。

## 测试方法与结果

本次只修改 Markdown，验收范围为本地链接、UTF-8 编码、代码块闭合、方向与现状一致性、文件差异及 Git 空白检查；不运行未受影响的应用测试，不产生模型费用。

在项目根目录通过 PowerShell 执行以下文档静态检查：

```powershell
@'
from pathlib import Path
import re

root = Path.cwd().resolve()
paths = [Path(name) for name in (
    'AGENTS.md', 'README.md',
    'docs/architecture/01-project-charter.md',
    'docs/architecture/02-system-architecture.md',
    'docs/architecture/64-full-multi-agent-roadmap.md',
    'docs/progress/README.md',
    'docs/progress/64-full-multi-agent-roadmap.md',
)]
checked = 0
for relative in paths:
    path = root / relative
    content = path.read_text(encoding='utf-8')
    assert '\ufffd' not in content, relative
    assert content.count(chr(96) * 3) % 2 == 0, relative
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', content):
        if '://' in target or target.startswith('#'):
            continue
        destination = (path.parent / target.split('#', 1)[0]).resolve()
        assert destination.is_relative_to(root), (relative, target)
        assert destination.exists(), (relative, target)
        checked += 1
print(f'PASS: {len(paths)} Markdown files, {checked} local links, UTF-8 and code fences.')
'@ | .\.venv\Scripts\python.exe -
git diff --check
```

实际结果：7 个 Markdown 文件、146 个本地链接通过检查，UTF-8 解码、替换字符与代码块闭合检查通过，`git diff --check` 无错误。检查完整差异确认只有文档改动；人工核对六阶段路线、角色数量、单一职责规则及现状/规划标记，确认章程和协作约定不再要求以单、多 Agent 收益对比作为开发前提。链接检查只验证本地目标文件，不访问外部网站或验证 Mermaid 渲染。

## 限制与下一步

- 本文档完成不代表完整多 Agent 工程版已实现。
- 轮次/任务的物理字段、公开接口与状态迁移，在下一切片明确并先更新数据库/API 设计；本次只锁定概念与约束。
- 首版动态调查规划默认最多三轮、三个专业任务并发，其他资源数值随对应切片验证并冻结到运行配置。
- 下一步先设计并实现调查轮次与专业任务的最小持久化切片，再把现有固定并行图接入任务记录和事件。

## 面试表达

> MVP 交付后，我把后续方向明确为完整多 Agent 平台，并先定义角色、协作任务、证据时效、预算、人工介入与恢复语义。规划继续使用已有持久任务和安全执行底座，按文件职责分离协议、业务、持久化与 Agent 编排，再逐个切片实现动态调查。当前提交是可追踪的架构决策，新增协作能力只有经过实际验收后才写成项目成果。
