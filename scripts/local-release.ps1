param(
    [Parameter(Mandatory)]
    [string]$EnvFile,
    [ValidateSet("Start", "Status", "Stop")]
    [string]$Action = "Start"
)

$ErrorActionPreference = "Stop"
$releaseConfig = (Resolve-Path -LiteralPath $EnvFile).Path
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$compose = @(
    "compose", "--project-directory", $root, "-p", "opsswarm-local-release",
    "--env-file", $releaseConfig, "--profile", "human-reassessment",
    "--profile", "candidate-evaluation",
    "-f", (Join-Path $root "compose.yaml"),
    "-f", (Join-Path $root "compose.local-release.yaml")
)
$services = @(
    "web", "change-service", "alloy", "investigation-worker-live",
    "human-preparation-worker", "human-review-worker", "experience-worker",
    "candidate-evaluation-worker", "release-execution-worker", "recovery-sampling-worker",
    "run-settlement-worker", "incident-convergence-worker", "reflow-coordination-worker",
    "reflow-dispatch-worker", "reflow-execution-worker", "reflow-synthesis-worker",
    "reflow-review-worker", "reflow-routing-worker"
)

$oldRuntimeImage = $env:OPSSWARM_RUNTIME_IMAGE
$oldWebImage = $env:OPSSWARM_WEB_IMAGE
try {
    $env:OPSSWARM_RUNTIME_IMAGE = "ops-swarm-runtime:local-release"
    $env:OPSSWARM_WEB_IMAGE = "ops-swarm-web:local-release"
    switch ($Action) {
        "Start" {
            docker @compose build control-migrate web
            if ($LASTEXITCODE -ne 0) { throw "无法构建本地发布镜像" }
            docker @compose up -d --no-build --wait --wait-timeout 240 @services
            if ($LASTEXITCODE -ne 0) { throw "本地发布服务未就绪，请查看 Compose 日志" }
        }
        "Status" {
            docker @compose ps -a
            if ($LASTEXITCODE -ne 0) { throw "无法读取本地发布状态" }
        }
        "Stop" {
            docker @compose stop
            if ($LASTEXITCODE -ne 0) { throw "无法停止本地发布服务" }
        }
    }
} finally {
    $env:OPSSWARM_RUNTIME_IMAGE = $oldRuntimeImage
    $env:OPSSWARM_WEB_IMAGE = $oldWebImage
}
