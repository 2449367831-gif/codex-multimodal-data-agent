param(
    [ValidateSet("snapshot", "live")]
    [string]$Mode = "snapshot",
    [switch]$FailOnDrift
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Missing .venv. Run scripts\setup.ps1 first."
}

Set-Location -LiteralPath $projectRoot
$arguments = @("tools\metadata_sync.py", "--mode", $Mode, "--output-dir", "candidates")
if ($FailOnDrift) {
    $arguments += "--fail-on-drift"
}
& $pythonPath @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Metadata scan failed with exit code ${LASTEXITCODE}."
}
