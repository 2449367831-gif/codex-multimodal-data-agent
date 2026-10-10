param(
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonwPath = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$supervisorPath = Join-Path $PSScriptRoot "launch_hidden.py"
$taskName = "RoyalTigerQueryAgent"

if (-not (Test-Path -LiteralPath $pythonwPath)) {
    throw "Missing .venv\Scripts\pythonw.exe. Run scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $supervisorPath)) {
    throw "Missing hidden service launcher: $supervisorPath"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$argumentLine = '"{0}" --supervise --no-browser' -f $supervisorPath

try {
    Import-Module ScheduledTasks -ErrorAction Stop
    $action = New-ScheduledTaskAction `
        -Execute $pythonwPath `
        -Argument $argumentLine `
        -WorkingDirectory $projectRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -DontStopOnIdleEnd `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal `
        -UserId $identity `
        -LogonType Interactive `
        -RunLevel Limited
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Keeps the local Royal Tiger query agent available without a console window." `
        -Force | Out-Null
} catch {
    # Keep the setup usable on Windows editions without the ScheduledTasks
    # PowerShell module. The supervisor still provides restart-on-failure.
    $schtasksPath = Join-Path $env:SystemRoot "System32\schtasks.exe"
    $taskRun = '"{0}" "{1}" --supervise --no-browser' -f $pythonwPath, $supervisorPath
    & $schtasksPath /Create /TN $taskName /SC ONLOGON /TR $taskRun /RL LIMITED /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to register the hidden service task."
    }
}

if ($StartNow) {
    $taskState = (Get-ScheduledTask -TaskName $taskName -ErrorAction Stop).State.ToString()
    if ($taskState -ne "Running") {
        try {
            Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
        } catch {
            $schtasksPath = Join-Path $env:SystemRoot "System32\schtasks.exe"
            & $schtasksPath /Run /TN $taskName | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "The hidden service task was registered but could not be started."
            }
        }
    }
}

Write-Host "Hidden service task registered: $taskName"
