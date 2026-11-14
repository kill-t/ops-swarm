param(
    [Parameter(Mandatory)]
    [string]$EnvFile,
    [ValidateSet("Start", "Status", "Stop")]
    [string]$Action = "Start",
    [switch]$ScriptedDemo,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$releaseConfig = (Resolve-Path -LiteralPath $EnvFile).Path
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectName = if ($ScriptedDemo) { "opsswarm-local-demo" } else { "opsswarm-local-release" }
$compose = @(
    "compose", "--project-directory", $root, "-p", $projectName,
    "--env-file", $releaseConfig, "--profile", "human-reassessment",
    "--profile", "candidate-evaluation",
    "-f", (Join-Path $root "compose.yaml"),
    "-f", (Join-Path $root "compose.local-release.yaml")
)
if ($ScriptedDemo) {
    $compose += @("-f", (Join-Path $root "compose.local-demo.yaml"))
}
$services = @(
    "web", "change-service", "alloy", "investigation-worker-live",
    "human-preparation-worker", "human-review-worker", "experience-worker",
    "candidate-evaluation-worker", "release-execution-worker", "recovery-sampling-worker",
    "run-settlement-worker", "incident-convergence-worker", "reflow-coordination-worker",
    "reflow-dispatch-worker", "reflow-execution-worker", "reflow-synthesis-worker",
    "reflow-review-worker", "reflow-routing-worker"
)
if ($ScriptedDemo) {
    $services += "investigation-worker-frozen"
}

$oldRuntimeImage = $env:OPSSWARM_RUNTIME_IMAGE
$oldWebImage = $env:OPSSWARM_WEB_IMAGE
try {
    $env:OPSSWARM_RUNTIME_IMAGE = "ops-swarm-runtime:local-release"
    $env:OPSSWARM_WEB_IMAGE = "ops-swarm-web:local-release"
    switch ($Action) {
        "Start" {
            if (-not $SkipBuild) {
                $revision = git -C $root rev-parse HEAD
                if ($LASTEXITCODE -ne 0) { throw "无法读取构建提交" }
                $trackedChanges = git -C $root status --porcelain --untracked-files=no
                if ($LASTEXITCODE -ne 0 -or $trackedChanges) { throw "发布构建要求受版本控制文件已提交" }
                docker @compose build --build-arg "OPSSWARM_BUILD_REVISION=$revision" control-migrate web
                if ($LASTEXITCODE -ne 0) { throw "无法构建本地发布镜像" }
            }
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
