# 正式演示构建归属整改

2026-11-14。正式报告此前只有演示协议版本，不能确定实际运行镜像对应哪个提交。本批为运行与前端镜像增加 OCI 提交标签，正式构建要求受版本控制文件已提交；未跟踪用户文件不参与此判断。

预检读取限定项目运行容器的实际镜像 ID、指定提交标签和数据库迁移 head，并保存 Compose 文件及依赖锁文件摘要。标签缺失、旧镜像混用、工作区修改或迁移版本不一致时明确阻断，错误报告保留已经收集的身份。不读取或输出容器环境，旧报告保持原样。详情见[演示设计](../architecture/local-demo-reproduction.md)。

`python -m pytest tests/unit/test_local_demo.py tests/unit/test_local_demo_provenance.py -q` 通过 36 项，覆盖构建不一致、时钟阻断仍保存构建身份，以及恢复采样 Worker 使用正式发布镜像。相关 Ruff 与 Git 差异检查通过。

Docker Desktop 启动后，空载 Linux 35 秒探针观测到两次 UTC 倒退。随后使用正式入口构建并启动 `9a83d4b`，运行及前端镜像标签正确，迁移为 `0100_reflow_input_projection`，查询时演示库事故数为 0。正式 `Check` 返回 `blocked_environment / unstable_container_clock`，捕获两次 UTC 倒退和三次超过 100 毫秒的偏差，报告保存提交、29 个运行容器的镜像身份及迁移版本。报告在 `.pytest_cache/local-demo/remediation-20261114-9a83d4b/runs/2a031700-d4f2-49ca-99d6-4aacf1abe6b1/report.json`。

正式三场景、浏览器、保留卷原案例及其账本验收待稳定环境。恢复脚本的 PostgreSQL/ASGI 定向复测未触发预期的 CAS 后中断，尚未确定原因。执行层定向验证 `test_release_execution_jobs_api.py -k 'actual_126_asgi_cas or crash_after_phase_commit'` 三项通过，覆盖真实演练 CAS、响应丢失后查询原操作、租约到期后只读接手和迟到结果拒绝。

后续应在独立提交后构建镜像，再用稳定环境执行正式验收。面试表达：报告不仅记录业务成功，也记录实际接受验证的构建与数据库版本，避免用旧镜像结果证明新代码已发布。
