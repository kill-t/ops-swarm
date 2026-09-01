# 50 - 运行实例构建产物核对

## 目标

为订单服务增加默认关闭的只读运行身份端点，并把最新部署声明的镜像仓库与 digest 和当前进程启动身份做确定性比较。比较结果持久化进入 Evidence Board，并在作战室展示；LLM 不参与比较，也不获得容器控制权限。

## 设计与边界

- 运行身份必须由部署系统在进程启动时完整注入，未启用时端点明确返回 `503`，不读取主机状态或猜测 tag。
- 只比较调查窗口内最新部署；更早部署标记为 `historical`，避免把正常版本演进误报为漂移。
- `matched` 要求镜像仓库和完整 digest 同时相等；任一不同即为 `mismatched`。
- 运行端点禁用为 `unavailable`；响应损坏、超时、过大、过期或 URL 越界会让调查失败关闭。
- 前端再次校验运行字段并重新计算匹配关系，不能只信任状态字符串。
- 结论只代表部署环境声明的一致性，不代表 Docker Engine、Registry 或签名系统的独立证明。

## 实际改动

- 新增运行身份设置、精确响应模型和 `GET /api/v1/runtime-metadata`。
- Compose 为订单服务增加四个显式运行身份变量，为现场调查 Worker 增加固定只读地址。
- 变更调查适配器增加运行响应白名单、新鲜度、大小和契约校验。
- Evidence 新增部署位置、运行匹配状态、实例 ID、运行仓库、运行 digest、启动时间和身份来源。
- 作战室新增当前运行核对结果及实际运行身份字段，并防御性识别矛盾数据。
- 补充后端 API、调查适配器与前端组件测试。

## 测试方法与结果

执行后端完整质量门禁：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

结果：350 个文件格式检查通过，Ruff 无问题，mypy 检查 143 个源码文件无问题，后端 `541 passed`。

执行前端完整质量门禁：

```powershell
cd web
npm run typecheck
npm test -- --maxWorkers=1
npm run build
```

结果：Vue/TypeScript 类型检查通过，9 个测试文件中的 31 个测试全部通过，Vite 生产构建成功。首次把 Vitest 与后端 Testcontainers 并行运行时，Windows 上最后一个 fork Worker 启动超时；已在后端结束后用同一完整命令串行重跑通过，没有忽略测试或放宽断言。

执行配置与格式验收：

```powershell
docker compose config --quiet
git diff --check
```

结果：Compose 配置有效，Git 差异无空白错误。

真实 Compose 验收中先构建当前 `ops-swarm-runtime:local`，由宿主 Docker CLI 读取该构建的 descriptor digest，再以随机实例 UUID、固定仓库和 digest 创建订单容器。容器实际引用的 image config ID 与刚构建镜像一致；只读端点返回的实例 ID、仓库和 digest 与注入值完全一致。应用容器没有 Docker Socket。

Docker Engine 刚启动时 Redis 与代理尚未运行，因此订单接口按设计短暂返回 `500`；启动既有依赖容器后确认 Redis 可用、版本故障关闭、连接池为 `50`，订单接口恢复 `200`，没有修改或删除数据卷。

最后使用真实变更服务和运行端点执行调查适配器：三条旧部署事实分别标记为历史且未声明 artifact，最新 v2 部署标记为 `current + mismatched`，并携带当前实例 UUID、仓库、digest、启动时间和身份来源。当前构建与上一条部署记录本就不同，因此该不一致是预期的真实漂移检测，不是伪造匹配。本步没有调用模型 API。

## 限制与下一步

- 当前只支持单实例，不处理滚动发布期间多个 digest 并存。
- 身份由部署环境注入，没有签名运行证明；有环境变量写权限的主体仍可伪造。
- 旧 Evidence 快照不会被改写，只有新调查包含运行核对字段。
- 下一步可增加多实例运行清单和滚动发布判定，或由最小权限部署控制器生成签名运行证明。

## 面试表达

> 我没有把 Docker Socket 暴露给 Agent，而是让订单进程只读报告部署系统在启动时注入的不可变镜像身份。调查适配器只比较最新部署，确定性产生 matched、mismatched、unavailable 或 historical；异常响应失败关闭。页面还会重新校验字段和结论是否一致。同时我明确区分部署平台内部一致性与真正的供应链签名证明，避免夸大安全能力。
