param(
    [int]$Port = 8020,
    [int]$BackendPort = 8010,
    [ValidateSet("loopback", "lan")]
    [string]$NetworkMode = "loopback",
    [ValidateSet("installation", "anonymous", "wecom_signed")]
    # Department-local installs use one random, persistent identity per machine.
    # No user login or business permission decision is performed in this mode.
    [string]$IdentityMode = "installation",
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

# Protect the persistent Codex context/auth directories before the service can
# create a new principal partition. This is a no-op only when the script is
# intentionally bypassed; direct uvicorn launches are not a production path.
& (Join-Path $PSScriptRoot "harden_runtime_acl.ps1")

$env:AGENT_CHAT_NETWORK_MODE = $NetworkMode
$env:AGENT_CHAT_HOST = if ($NetworkMode -eq "lan") { "0.0.0.0" } else { "127.0.0.1" }
$env:AGENT_CHAT_PORT = $Port.ToString()
$env:AGENT_CHAT_BACKEND_URL = "http://127.0.0.1:$BackendPort"
if ($IdentityMode) {
    $env:AGENT_CHAT_IDENTITY_MODE = $IdentityMode
}
if ($env:AGENT_CHAT_IDENTITY_MODE -eq "wecom_signed" -and
    [string]::IsNullOrWhiteSpace($env:AGENT_CHAT_IDENTITY_SECRET)) {
    throw "AGENT_CHAT_IDENTITY_SECRET is required when IdentityMode is wecom_signed."
}

if ($env:AGENT_CHAT_IDENTITY_MODE -eq "installation") {
    $installationPath = $env:AGENT_INSTALLATION_ID_PATH
    if ([string]::IsNullOrWhiteSpace($installationPath)) {
        $installationPath = Join-Path $projectRoot "var\installation-identity.json"
    }
    elseif (-not [IO.Path]::IsPathRooted($installationPath)) {
        $installationPath = Join-Path $projectRoot $installationPath
    }
    Write-Host "Installation identity: $([IO.Path]::GetFullPath($installationPath))"
}

Set-Location -LiteralPath $projectRoot
if ($runInBackground) {
    $pythonwPath = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonwPath)) {
        throw "Missing .venv\\Scripts\\pythonw.exe. Run scripts\\setup.ps1 first."
    }
    $logRoot = Join-Path $projectRoot "var\logs"
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    $stdoutLog = Join-Path $logRoot "chat-$Port.out.log"
    $stderrLog = Join-Path $logRoot "chat-$Port.err.log"
    $process = Start-Process `
        -FilePath $pythonwPath `
        -ArgumentList @("-m", "uvicorn", "app.chat_api:app", "--host", $env:AGENT_CHAT_HOST, "--port", $Port.ToString()) `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "Chat service started in background (PID $($process.Id)); logs: $logRoot"
    exit 0
}
& $pythonPath -m uvicorn app.chat_api:app --host $env:AGENT_CHAT_HOST --port $Port
exit $LASTEXITCODE
