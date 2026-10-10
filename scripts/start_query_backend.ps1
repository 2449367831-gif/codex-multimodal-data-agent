param(
    [int]$Port = 8010,
    [string]$Database = "ywzt_ods",
    [string]$AllowedSchemas = "",
    [switch]$Background,
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
if ($Background -and $Foreground) {
    throw "Background and Foreground cannot be used together."
}
$runInBackground = $Background -or -not $Foreground
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Missing .venv. Run scripts\setup.ps1 first."
}

& (Join-Path $PSScriptRoot "harden_runtime_acl.ps1")

$required = @("DORIS_HOST", "DORIS_USER", "DORIS_PASSWORD")
$missing = @(
    $required | Where-Object {
        $null -eq [Environment]::GetEnvironmentVariable($_, "Process")
    }
)
if ($missing.Count -gt 0) {
    throw "Missing Doris environment variables: $($missing -join ', ')"
}

$env:DORIS_EXECUTION_MODE = "doris"
$env:DORIS_DATABASE = $Database
$env:AGENT_HOST = "127.0.0.1"
$env:AGENT_PORT = $Port.ToString()
$env:AGENT_SQL_ALLOWED_SCHEMAS = if ($AllowedSchemas) {
    $AllowedSchemas
} else {
    "$Database,dms_ods,information_schema"
}

Set-Location -LiteralPath $projectRoot
if ($runInBackground) {
    $pythonwPath = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonwPath)) {
        throw "Missing .venv\\Scripts\\pythonw.exe. Run scripts\\setup.ps1 first."
    }
    $logRoot = Join-Path $projectRoot "var\logs"
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    $stdoutLog = Join-Path $logRoot "query-backend-$Port.out.log"
    $stderrLog = Join-Path $logRoot "query-backend-$Port.err.log"
    $process = Start-Process `
        -FilePath $pythonwPath `
        -ArgumentList @("-m", "uvicorn", "app.query_backend:app", "--host", "127.0.0.1", "--port", $Port.ToString()) `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "Query backend started in background (PID $($process.Id)); logs: $logRoot"
    exit 0
}
& $pythonPath -m uvicorn app.query_backend:app --host 127.0.0.1 --port $Port
