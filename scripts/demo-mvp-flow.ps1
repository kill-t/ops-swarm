[CmdletBinding()]
param(
    [switch]$AllowModelCall,
    [ValidateRange(30, 300)]
    [int]$InvestigationTimeoutSeconds = 120,
    [ValidateRange(90, 300)]
    [int]$VerificationTimeoutSeconds = 180,
    [string]$ControlBaseUrl = "http://127.0.0.1:8000",
    [string]$OrderBaseUrl = "http://127.0.0.1:18001"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $AllowModelCall) {
    throw "该演示会调用 .env 中配置的真实模型并产生费用；确认后请增加 -AllowModelCall"
}

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

function Invoke-JsonRequest {
    param(
        [Parameter(Mandatory)]
        [ValidateSet("Post", "Put")]
        [string]$Method,
        [Parameter(Mandatory)]
        [string]$Uri,
        [hashtable]$Body
    )

    if ($null -eq $Body) {
        return Invoke-RestMethod -Method $Method -Uri $Uri
    }
    Invoke-RestMethod `
        -Method $Method `
        -Uri $Uri `
        -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json -Depth 6 -Compress)
}

function Wait-ResourceStatus {
    param(
        [Parameter(Mandatory)]
        [string]$Uri,
        [Parameter(Mandatory)]
        [string[]]$TerminalStatuses,
        [Parameter(Mandatory)]
        [int]$TimeoutSeconds,
        [Parameter(Mandatory)]
        [string]$ResourceName
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $resource = Invoke-RestMethod -Uri $Uri
        if ($resource.status -in $TerminalStatuses) {
            return $resource
        }
        Start-Sleep -Seconds 2
    } while ([DateTimeOffset]::UtcNow -lt $deadline)

    throw "$ResourceName 在 $TimeoutSeconds 秒内没有进入终态"
}

function Set-ReleaseFault {
    param([bool]$Enabled)

    Invoke-JsonRequest `
        -Method Put `
        -Uri "$OrderBaseUrl/__admin/faults/release-500" `
        -Body @{ enabled = $Enabled } | Out-Null
}

Assert-LocalHttpBase -Value $ControlBaseUrl -Name "ControlBaseUrl"
Assert-LocalHttpBase -Value $OrderBaseUrl -Name "OrderBaseUrl"
$ControlBaseUrl = $ControlBaseUrl.TrimEnd("/")
$OrderBaseUrl = $OrderBaseUrl.TrimEnd("/")

$runToken = [Guid]::NewGuid().ToString("N")
$incidentId = $null

try {
    Set-ReleaseFault -Enabled $false
    Set-ReleaseFault -Enabled $true

    $alert = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/alerts" `
        -Body @{
            source = "mvp-release-demo"
            external_event_id = "release-$runToken"
            fingerprint = "order-service:http-500:$runToken"
            service_name = "order-service"
            severity = "p1"
            title = "订单接口在新版本发布后大量返回 500"
            payload = @{ status_code = 500; exercise = "mvp-release" }
            occurred_at = [DateTimeOffset]::UtcNow.ToString("o")
        }
    $incidentId = $alert.incident.id

    $started = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId/actions/start-investigation" `
        -Body @{
            expected_version = 1
            actor_id = "demo-engineer-001"
            reason = "MVP 发布验收：使用单 Agent 冻结证据基线"
            mode = "single_agent"
            data_source_mode = "frozen"
        }
    $investigationId = $started.investigation.id
    $investigation = Wait-ResourceStatus `
        -Uri "$ControlBaseUrl/api/v1/investigations/$investigationId" `
        -TerminalStatuses @("succeeded", "failed") `
        -TimeoutSeconds $InvestigationTimeoutSeconds `
        -ResourceName "调查运行"
    if ($investigation.status -ne "succeeded") {
        throw "调查运行失败，请在作战室查看尝试审计"
    }
    if ($investigation.result.conclusion.root_cause -ne "release_500_regression") {
        throw "调查没有识别出版本回归根因"
    }

    $plan = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/investigations/$investigationId/action-plans"
    $incident = Invoke-RestMethod -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId"
    $approval = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/action-plans/$($plan.action_plan.id)/approval-requests" `
        -Body @{
            expected_incident_version = $incident.version
            actor_id = "demo-engineer-001"
            reason = "确认回滚目标与恢复验证规则"
        }
    $incident = Invoke-RestMethod -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId"
    $decision = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/approval-requests/$($approval.approval_request.id)/actions/approve" `
        -Body @{
            expected_incident_version = $incident.version
            actor_id = "demo-engineer-002"
            reason = "复核证据后批准本地演练回滚"
        }

    $incident = Invoke-RestMethod -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId"
    $stepId = $plan.action_plan.steps[0].id
    $execution = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/action-steps/$stepId/executions" `
        -Body @{
            idempotency_key = "release-execution-$runToken"
            expected_incident_version = $incident.version
        }
    if ($execution.operation_execution.status -ne "succeeded") {
        throw "白名单回滚没有成功"
    }

    $incident = Invoke-RestMethod -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId"
    $verificationCreation = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/operation-executions/$($execution.operation_execution.id)/verification-runs" `
        -Body @{ expected_incident_version = $incident.version }
    $verificationId = $verificationCreation.verification_run.id
    $verification = Wait-ResourceStatus `
        -Uri "$ControlBaseUrl/api/v1/verification-runs/$verificationId" `
        -TerminalStatuses @("passed", "failed") `
        -TimeoutSeconds $VerificationTimeoutSeconds `
        -ResourceName "恢复验证"
    if ($verification.status -ne "passed") {
        throw "恢复验证没有通过"
    }

    $incident = Invoke-RestMethod -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId"
    $closed = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId/actions/close" `
        -Body @{
            expected_version = $incident.version
            actor_id = "demo-engineer-001"
            reason = "恢复窗口通过，完成 MVP 发布验收"
        }
    $report = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/incidents/$incidentId/reports" `
        -Body @{
            expected_incident_version = $closed.version
            actor_id = "demo-engineer-001"
        }
    $actionItem = Invoke-JsonRequest `
        -Method Post `
        -Uri "$ControlBaseUrl/api/v1/incident-reports/$($report.report.id)/action-items" `
        -Body @{
            idempotency_key = "release-follow-up-$runToken"
            title = "复核版本发布前的回归门禁"
            description = "补充订单核心接口的发布前冒烟与回滚演练记录"
            owner_id = "demo-sre-001"
            due_at = [DateTimeOffset]::UtcNow.AddDays(30).ToString("o")
            actor_id = "demo-engineer-001"
        }

    [pscustomobject]@{
        Result = "MVP 事故闭环演示通过"
        IncidentReference = $closed.reference
        IncidentStatus = $closed.status
        InvestigationStatus = $investigation.status
        RootCause = $investigation.result.conclusion.root_cause
        VerificationStatus = $verification.status
        ReportId = $report.report.id
        FollowUpStatus = $actionItem.action_item.status
        WorkroomUrl = "http://127.0.0.1:5173/incidents/$incidentId"
    } | Format-List
}
finally {
    try {
        Set-ReleaseFault -Enabled $false
    }
    catch {
        Write-Warning "恢复版本故障安全默认值失败：$($_.Exception.Message)"
    }
}
