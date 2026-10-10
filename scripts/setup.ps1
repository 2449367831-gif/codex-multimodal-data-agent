param(
    [string]$PythonVersion = "3.13"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

uv sync --python $PythonVersion --extra dev --frozen
Write-Host "Environment ready: $projectRoot\.venv"
