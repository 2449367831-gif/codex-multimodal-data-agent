param(
    [string]$TaskName = "DorisQueryAgent-MetadataSync",
    [datetime]$At = (Get-Date).Date.AddHours(6),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$scanScript = Join-Path $projectRoot "scripts\scan_metadata.ps1"

if (-not (Test-Path -LiteralPath $scanScript)) {
    throw "Missing metadata scan script: $scanScript"
}

$action = New-ScheduledTaskAction `
    -Execute "PowerShell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scanScript`" -Mode live -FailOnDrift"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel LeastPrivilege

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    throw "Scheduled task '$TaskName' already exists. Use -Force to replace it."
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Read-only Doris metadata scan; writes candidate reports only." `
    -Force:$Force | Out-Null

Write-Host "Registered '$TaskName' at $At."
Write-Host "The task runs as the current interactive user. Configure persistent DORIS_* values or secret injection for that account; session-only variables are not sufficient."
