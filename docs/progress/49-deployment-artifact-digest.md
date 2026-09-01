# 49 - 部署构建产物摘要

## 目标

让新 CI/CD 部署事实不仅声明代码仓库和 Commit SHA，还必须声明不带 tag 的镜像仓库与 `sha256` digest。这样事故调查可以追溯不可变构建产物，同时保持第 48 步历史记录可读、不可伪造。

本切片只保存部署身份声明的 Commit—镜像映射，不独立验证镜像仓库、镜像签名或运行容器，因此不会把“已声明”误写成“已运行验证”。

## 设计与边界

- `source_contract_version=1` 表示本地事实或历史 CI/CD 事实，并禁止携带新构建产物字段；历史数据只回填版本，不修改详情和内容摘要。
- 新部署摄取入口由服务端固定写 v2；v2 必须同时包含代码引用和构建产物引用，客户端无权降级版本。
- 代码仓库和镜像仓库由两条服务端白名单独立限制；镜像仓库不接受 tag、协议、用户信息或越界端口，digest 只接受 64 位小写十六进制 SHA-256。
- Pydantic 校验外部输入，PostgreSQL CHECK 防止内部代码绕过应用契约，并拒绝构建产物额外字段；相同投递号改变 digest、契约版本或其他事实时返回 `409`。
- Evidence Board 和作战室明确区分 `declared`、`not_declared` 与 `invalid`，历史记录不会被补猜镜像。
- Agent 仍只有有界只读工具，不拥有摄取密钥、Docker Socket、镜像仓库写权限或任意 Shell。

## 实际改动

- 增加 `DeploymentArtifactReference`、镜像仓库和 OCI digest 精确类型，并把构建产物纳入部署内容哈希。
- 部署事件 v2 增加 `image_repository`、`image_digest`，并增加服务端镜像仓库白名单。
- `change_records` 增加来源契约版本，Alembic 迁移保留历史 v1，强制新 v2 事实包含合法代码与镜像引用。
- 仓储层保存并校验契约版本，精确重放继续返回原记录，版本或内容漂移保持冲突。
- 变更调查证据增加来源契约版本、构建产物状态、镜像仓库与 digest。
- 作战室增加不可变镜像来源展示，并在浏览器端防御性识别缺失或损坏字段。
- 架构、数据库、配置示例和路线图同步到第 49 步。

## 测试方法与结果

执行后端完整质量门禁：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

结果：347 个文件格式检查通过，Ruff 无问题，mypy 检查 142 个源码文件无问题，后端 `525 passed`。

执行前端完整质量门禁：

```powershell
cd web
npm run typecheck
npm test -- --maxWorkers=1
npm run build
```

结果：Vue/TypeScript 类型检查通过，9 个测试文件中的 28 个测试全部通过，Vite 生产构建成功。

执行 Compose、迁移与格式验收：

```powershell
docker compose config --quiet
git diff --check
```

结果：Compose 配置有效，`git diff --check` 无格式错误；本地演练库处于 `0026_deployment_artifact_digest`，迁移测试验证空库升级/降级、升级一致性检查以及存在 v2 事实时拒绝有损降级。

运行时冒烟采用从当前 Git HEAD 导出的干净源码快照构建订单服务镜像，再把该镜像的 descriptor digest 与同一 HEAD Commit 作为 v2 部署事件投递。结果为：首次写入 `201`、完全相同重放 `200`、同一投递号更换 digest 返回 `409`；只读变更工具同时读到历史 v1 的 `not_declared` 与新 v2 的 `declared` 镜像证据。验收库保留 2 条历史 v1 和 1 条 v2，v2 记录包含构建产物引用。

验收结束后重新关闭部署摄取入口，并确认：变更服务健康、订单接口返回 `200`、版本故障关闭、连接池为 `50`、Redis 可用、未启用摄取请求返回 `503`。临时源码快照目录已通过限定目标的 `git clean` 删除，没有修改或删除项目源码。

## 限制与下一步

- 没有访问 Registry 验证 digest 是否存在，也没有验证 Cosign/Notary 签名、SBOM 或构建证明。
- CI/CD 身份声明仍可能出错，HMAC 只证明持有共享密钥的身份发送了字段。
- 运行时验收使用从当前 Git HEAD 导出的源码快照构建订单服务镜像，证明 v2 事件、幂等冲突和 Evidence 映射可运行；旧运行镜像因本地 tag 重建后已无可查询的镜像对象，因此没有为它伪造 v2 映射。
- 第 50 步已增加受限只读运行身份，并以确定性代码比较最新部署声明与当前进程启动身份；Agent 和业务容器仍没有 Docker Socket。

## 面试表达

> 我没有用可变 tag 代表部署产物，而是把代码 Commit 和 OCI SHA-256 digest 一起存入不可变部署事实。为兼容已经保存的记录，我增加显式来源契约版本：历史事实是 v1，新入口只能写 v2，且数据库强制 v2 同时包含代码与镜像引用。应用层和数据库层都限制仓库格式、端口和 digest，同一投递号内容漂移会冲突。Agent 与前端只读并明确显示这是 CI 声明，不把它包装成运行实例验证；真正的运行匹配会由下一步确定性适配器完成。
