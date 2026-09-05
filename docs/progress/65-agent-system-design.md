# 65 - Agent 系统最终设计与事实核对约定

## 目标

将最终多 Agent 方案落入仓库，补全上一版总路线缺少的记忆、上下文、工具、Harness 和协议设计，使后续开发能从版本化文件恢复目标、约束与实际进展。

## 设计与边界

- 本次为独立文档切片，按用户确认的最终计划先保存决策，再进入轮次与专业任务持久化。没有修改运行代码、数据库、API、依赖、Compose 或业务数据，没有进行付费模型调用。
- 第 64 篇保留项目总路线，第 65 篇保存完整专项设计；`AGENTS.md` 只保存长期规则，进度索引保存当前推进位置，旧进度记录保留当时事实。
- 多 Agent 是既定方向；固定采用 LangGraph、LangChain 与内部轻量 Harness，不重复实现框架能力。内部通信不采用 A2A，本地日志/指标只读 MCP 纳入完整版本必须交付的能力。
- 工作记忆、事故事实、人工确认的经验、Git 手册与 checkpoint 分工明确；上下文按任务隔离、按需读取、受预算约束并保留材料与版本清单。
- 单一职责继续约束代码文件、类、函数和前端组件。专项设计列出运行层组件职责，按实现切片提取边界，不提前创建目录或顺带全仓重构。
- 已决定、开发中和已验收分别标记。文档完成不代表动态协作、上下文、经验库、MCP 或图内人工恢复已实现。

## 实际改动

- 新增 [Agent 系统最终设计](../architecture/65-agent-system-design.md)，保存六个角色、自主性边界、未知假设、分层记忆、经验生命周期、ContextBuilder、八类能力工具、MCP、Harness、预算、持久化恢复、接口演进和完整验收。
- 更新 [总路线](../architecture/64-full-multi-agent-roadmap.md)，用最终十一项交付顺序替代最初六阶段安排，将 MCP 从可选改为计划内必选，并在经验审核前安排可信身份。
- 更新根目录协作约定，固定框架与协议、记忆和上下文边界、工具能力范围、单一职责及每次继续开发的事实核对流程。
- 同步项目章程、系统架构和技术栈，区分当前 MVP 路径与目标 v2 工作流；README 增加最终设计和进度入口。
- 更新本目录索引与当前推进位置，下一切片明确为轮次、专业任务及尝试的最小持久化，不宣布整版功能完成。
- 核对现有并行图的固定分派、代码取证和确定性质疑，保留第 63 步验收的历史归属。本次不把旧测试数量作为新执行结果。
- 查阅专项设计中的四项官方参考，保存链接、2026-09-05 查阅日期与采用边界，未把参考项目能力写成 OpsSwarm 已有功能。

## 测试方法与结果

本次只修改 Markdown，验证范围为九份文档的 UTF-8 编码、本地链接、标题/代码块/表格结构、两个路线表的交付顺序、现状与规划一致性、完整差异和 Git 空白检查。没有运行未受影响的应用测试，也没有重新执行 MVP 发布或现场演示。

在仓库根目录运行以下文档检查，Python 脚本只读取文件：

```powershell
@'
from pathlib import Path
import re

root = Path.cwd().resolve()
names = (
    'AGENTS.md', 'README.md',
    'docs/architecture/01-project-charter.md',
    'docs/architecture/02-system-architecture.md',
    'docs/architecture/03-technology-stack.md',
    'docs/architecture/64-full-multi-agent-roadmap.md',
    'docs/architecture/65-agent-system-design.md',
    'docs/progress/README.md',
    'docs/progress/65-agent-system-design.md',
)
links = 0
texts = {}
for name in names:
    path = root / name
    content = path.read_text(encoding='utf-8')
    assert '\ufffd' not in content, name
    visible = []
    in_fence = False
    width = None
    for line in content.splitlines():
        if line.startswith(chr(96) * 3):
            in_fence = not in_fence
            width = None
            continue
        if in_fence:
            continue
        visible.append(line)
        if line.startswith('|'):
            columns = len(re.split(r'(?<!\\)\|', line)) - 2
            if width is not None:
                assert columns == width, (name, line)
            width = columns
        else:
            width = None
    assert not in_fence, name
    text = '\n'.join(visible)
    assert len(re.findall(r'^# ', text, re.M)) == 1, name
    texts[name] = text
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
        if '://' in target or target.startswith('#'):
            continue
        destination = (path.parent / target.split('#', 1)[0]).resolve()
        assert destination.is_relative_to(root), (name, target)
        assert destination.is_file(), (name, target)
        links += 1
milestones = []
for name in names[5:7]:
    rows = re.findall(r'^\| (\d+) \| ([^|]+) \|', texts[name], re.M)
    assert [int(number) for number, _ in rows] == list(range(1, 12)), name
    milestones.append([(number, title.strip()) for number, title in rows])
assert milestones[0] == milestones[1], milestones
print(f'PASS: {len(names)} Markdown files, {links} local links, UTF-8, headings, fences and tables.')
print('PASS: all 11 roadmap milestones match the detailed design.')
'@ | .\.venv\Scripts\python.exe -
if ($LASTEXITCODE -ne 0) { throw 'Markdown validation failed' }

git diff --check
if ($LASTEXITCODE -ne 0) { throw 'Working tree whitespace check failed' }
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw 'Staged whitespace check failed' }
```

实际结果：九份 Markdown 文档、176 个本地链接通过检查；UTF-8、单个一级标题、代码块闭合和表格列数检查通过；两篇架构文档的十一项交付顺序完全一致。`git diff --check` 与 `git diff --cached --check` 无错误。

差异审查确认本次只有九份 Markdown 文档改动。人工核对多 Agent 既定方向、六个角色、八类工具、MCP 必选范围、不采用 A2A、可信身份先于经验审核、预算与默认上限、v1/v2 兼容边界及当前能力标记，没有把待开发功能记为已实现。本切片验收对象为文档。

检查器只验证本地链接文件存在和基本 Markdown 结构，不验证标题锚点或 Mermaid 渲染。专项设计的外部参考已通过网页读取核对，链接检查器本身不访问网络。

## 限制与下一步

- 本次不交付新增运行能力，现有业务审批不能表述为 LangGraph 图内暂停恢复，既有复盘报告与改进行动项不能表述为已启用的经验记忆。
- 后续依赖版本和具体框架 API 在实现切片重新核对；本次没有预建物理表、协议路由或空壳服务。
- 下一切片先明确轮次、专业任务及尝试的状态、稳定身份、租约、幂等与失败路径，更新数据库/API 设计，再实现最小迁移、必要查询与真实 PostgreSQL 测试。
- 后续固定图接入、Harness、上下文、动态协作、MCP 和经验等逐片交付，按实际验证更新当前推进位置。

## 面试表达

> 我在 MVP 之后明确了完整多 Agent 系统的运行边界：LangGraph 组织协作，LangChain 承担专业工具循环，轻量 Harness 统一上下文、预算与结果检查。计划把任务事实、经验和 checkpoint 分开管理，通过角色工具权限与独立审批约束执行。这次提交形成可追踪的架构决定，后续用持久化、恢复与现场协作测试逐项验证；尚未实现的能力不作为已完成成果描述。
