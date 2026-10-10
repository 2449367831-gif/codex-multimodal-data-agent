param(
    [switch]$StartNow,
    [switch]$InspectOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$taskName = "RoyalTigerQueryAgent"

function Get-AgentRuntimeKind($process) {
    if ($process.Name -notmatch '^pythonw?\.exe$' -or -not $process.CommandLine) {
        return $null
    }
    $interpreter = [regex]::Escape((Join-Path $projectRoot '.venv\Scripts\pythonw.exe'))
    $launcher = [regex]::Escape((Join-Path $projectRoot 'scripts\launch_hidden.py'))
    $prefix = '^"?' + $interpreter + '"?\s+'
    if ($process.CommandLine -match ($prefix + '"?' + $launcher + '"?\s+--supervise\s+--no-browser\s*$')) {
        return 'supervisor'
    }
    if ($process.CommandLine -match ($prefix + '-m\s+uvicorn\s+app\.(chat_api|query_backend):app\s+--host\s+[^\s]+\s+--port\s+\d{4,5}\s*$')) {
        return 'service'
    }
    return $null
}

if ($InspectOnly) {
    Get-CimInstance Win32_Process | ForEach-Object {
        $kind = Get-AgentRuntimeKind $_
        if ($kind) {
            [pscustomobject]@{ ProcessId = $_.ProcessId; Kind = $kind }
        }
    }
    return
}

# Stop the supervisor before its detached children, otherwise it may relaunch
# them during the restart. An unrelated listener is never ours to terminate.
Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
Start-Sleep -Seconds 1
$processes = @(Get-CimInstance Win32_Process | Where-Object { Get-AgentRuntimeKind $_ })

foreach ($process in $processes) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)"
    if ($current -and $current.CreationDate -eq $process.CreationDate -and
        $current.CommandLine -eq $process.CommandLine -and (Get-AgentRuntimeKind $current)) {
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
    }
}

Start-Sleep -Seconds 1
if ($StartNow) {
    Start-ScheduledTask -TaskName $taskName
}

Write-Host "Hidden runtime restart requested: $taskName; verify readiness before accepting traffic."
