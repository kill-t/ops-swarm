[CmdletBinding()]
param(
    [string]$ControlBaseUrl = "http://127.0.0.1:8000",
    [string]$WebBaseUrl = "http://127.0.0.1:5173",
    [string]$OrderBaseUrl = "http://127.0.0.1:18001"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-LocalHttpBase {
    param(
        [Parameter(Mandatory)]
        [string]$Value,
        [Parameter(Mandatory)]
        [string]$Name
    )

    $uri = [System.Uri]$Value
    if ($uri.Scheme -ne "http" -or $uri.Host -notin @("127.0.0.1", "localhost")) {
        throw "$Name 只允许使用本机 http 地址"
    }
    if ($uri.AbsolutePath -ne "/" -or $uri.Query -or $uri.Fragment -or $uri.UserInfo) {
        throw "$Name 不能包含路径、查询参数、片段或用户信息"
    }
}

function Assert-Equal {
    param(
        [Parameter(Mandatory)]
        [AllowNull()]
        [object]$Actual,
        [Parameter(Mandatory)]
        [AllowNull()]
        [object]$Expected,
        [Parameter(Mandatory)]
        [string]$Message
    )

    if ($Actual -ne $Expected) {
        throw "$Message；期望=$Expected，实际=$Actual"
    }
}

function Invoke-JsonPut {
    param(
        [Parameter(Mandatory)]
        [string]$Uri,
        [Parameter(Mandatory)]
        [hashtable]$Body
    )

    Invoke-RestMethod `
        -Method Put `
        -Uri $Uri `
        -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json -Compress)
}

function Get-OrderResult {
    $response = Invoke-WebRequest `
        -Uri "$OrderBaseUrl/orders/release-acceptance" `
        -SkipHttpErrorCheck `
        -UseBasicParsing
    [pscustomobject]@{
        StatusCode = $response.StatusCode
        Body = $response.Content | ConvertFrom-Json
    }
}

function Restore-SafeDefaults {
    Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/faults/release-500" `
        -Body @{ enabled = $false } | Out-Null
    Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/configuration/database-pool" `
        -Body @{ value = 50 } | Out-Null
    Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/dependencies/redis" `
        -Body @{ available = $true } | Out-Null
}

Assert-LocalHttpBase -Value $ControlBaseUrl -Name "ControlBaseUrl"
Assert-LocalHttpBase -Value $WebBaseUrl -Name "WebBaseUrl"
Assert-LocalHttpBase -Value $OrderBaseUrl -Name "OrderBaseUrl"

$ControlBaseUrl = $ControlBaseUrl.TrimEnd("/")
$WebBaseUrl = $WebBaseUrl.TrimEnd("/")
$OrderBaseUrl = $OrderBaseUrl.TrimEnd("/")

try {
    Restore-SafeDefaults

    $controlHealth = Invoke-RestMethod -Uri "$ControlBaseUrl/health"
    Assert-Equal $controlHealth.status "healthy" "控制面健康检查失败"

    $webHome = Invoke-WebRequest -Uri "$WebBaseUrl/" -UseBasicParsing
    Assert-Equal $webHome.StatusCode 200 "Web 首页不可访问"
    $spaRoute = Invoke-WebRequest -Uri "$WebBaseUrl/follow-up" -UseBasicParsing
    Assert-Equal $spaRoute.StatusCode 200 "前端 history fallback 不可用"
    $proxiedApi = Invoke-WebRequest -Uri "$WebBaseUrl/api/v1/incidents" -UseBasicParsing
    Assert-Equal $proxiedApi.StatusCode 200 "Web 到控制面的同源代理不可用"

    $healthyOrder = Get-OrderResult
    Assert-Equal $healthyOrder.StatusCode 200 "安全默认状态下订单接口失败"
    Assert-Equal $healthyOrder.Body.status "confirmed" "安全默认订单状态错误"

    $releaseFault = Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/faults/release-500" `
        -Body @{ enabled = $true }
    Assert-Equal $releaseFault.enabled $true "版本故障没有启用"
    $releaseFailure = Get-OrderResult
    Assert-Equal $releaseFailure.StatusCode 500 "版本故障没有稳定返回 500"
    Assert-Equal $releaseFailure.Body.error_code "release_500" "版本故障错误码不正确"
    Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/faults/release-500" `
        -Body @{ enabled = $false } | Out-Null

    $poolFault = Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/configuration/database-pool" `
        -Body @{ value = 5; expected_value = 50 }
    Assert-Equal $poolFault.status "saturated" "连接池故障没有进入饱和状态"
    $poolFailure = Get-OrderResult
    Assert-Equal $poolFailure.StatusCode 500 "连接池故障没有稳定返回 500"
    Assert-Equal $poolFailure.Body.error_code "database_pool_timeout" "连接池故障错误码不正确"
    Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/configuration/database-pool" `
        -Body @{ value = 50; expected_value = 5 } | Out-Null

    $redisFault = Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/dependencies/redis" `
        -Body @{ available = $false; expected_available = $true }
    Assert-Equal $redisFault.status "unavailable" "Redis 故障没有进入不可用状态"
    $redisFailure = Get-OrderResult
    Assert-Equal $redisFailure.StatusCode 500 "Redis 故障没有稳定返回 500"
    Assert-Equal `
        $redisFailure.Body.error_code `
        "redis_connection_unavailable" `
        "Redis 故障错误码不正确"
    Invoke-JsonPut `
        -Uri "$OrderBaseUrl/__admin/dependencies/redis" `
        -Body @{ available = $true; expected_available = $false } | Out-Null

    $recoveredOrder = Get-OrderResult
    Assert-Equal $recoveredOrder.StatusCode 200 "故障恢复后订单接口仍然失败"
    Assert-Equal $recoveredOrder.Body.status "confirmed" "故障恢复后订单状态错误"

    Write-Output "MVP 运行态验收通过：控制面、Web 同源代理和三种确定性故障均可运行并恢复。"
}
finally {
    try {
        Restore-SafeDefaults
    }
    catch {
        Write-Warning "恢复安全默认值失败，请人工检查三个故障开关：$($_.Exception.Message)"
    }
}
