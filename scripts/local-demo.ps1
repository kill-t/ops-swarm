param(
    [ValidateSet("Prepare", "Start", "Check", "Run", "Status", "Stop")]
    [string]$Action = "Check",
    [ValidateSet("handoff", "recovery", "reflow")]
    [string]$Scenario = "handoff",
    [string]$DemoDirectory = ".pytest_cache/local-demo",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $root ".venv/bin/python" }
if (-not (Test-Path -LiteralPath $python)) { throw "请先运行 uv sync --locked" }
Push-Location $root
try {
    $demoArgs = @("-m", "scripts.local_demo", "--directory", $DemoDirectory)
    $envFile = & $python @demoArgs path
    if ($LASTEXITCODE -ne 0) { throw "演示目录无效" }
    if ($Action -in @("Prepare", "Start")) {
        & $python @demoArgs prepare
        if ($LASTEXITCODE -ne 0) { throw "演示身份配置不可用" }
    }
    if ($Action -in @("Start", "Status", "Stop")) {
        & (Join-Path $PSScriptRoot "local-release.ps1") -EnvFile $envFile -Action $Action -ScriptedDemo -SkipBuild:$SkipBuild
    } elseif ($Action -eq "Check") {
        & $python @demoArgs check
        if ($LASTEXITCODE -ne 0) { throw "环境预检未通过，报告已保存" }
    } elseif ($Action -eq "Run") {
        & $python @demoArgs run --scenario $Scenario
        if ($LASTEXITCODE -ne 0) { throw "演示未通过，持久事实与报告已保留" }
    }
} finally {
    Pop-Location
}
