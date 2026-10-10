$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Missing .venv. Run scripts\setup.ps1 first."
}

Set-Location -LiteralPath $projectRoot

function Invoke-CheckedPython {
    & $pythonPath @args
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code ${LASTEXITCODE}: $($args -join ' ')"
    }
}

# Check the imported source snapshot when the original project is available.
# The optional path keeps packaged builds usable without that source tree.
$sourceProjectRoot = if ($env:SOURCE_PROJECT_ROOT) {
    $env:SOURCE_PROJECT_ROOT
} else {
    "D:\数仓取数规则"
}
if (Test-Path -LiteralPath $sourceProjectRoot -PathType Container) {
    Invoke-CheckedPython -m tools.sync_source_project --source-root $sourceProjectRoot
}

Invoke-CheckedPython "tools\validate_knowledge_base.py"
Invoke-CheckedPython "tools\run_golden_questions.py"
Invoke-CheckedPython -m unittest discover -s app/tests -v
Invoke-CheckedPython -m unittest discover -s tests -p "test_*.py" -v
# unittest discovery does not run pytest function-based browser contracts.
Invoke-CheckedPython -m pytest app/tests/test_frontend_table_parity.py app/tests/test_frontend_result_presentation.py -q
Invoke-CheckedPython -m pytest app/tests/test_direct_sales_ranking.py app/tests/test_request_syntax.py app/tests/test_sales_ranking_integrity.py -q
Invoke-CheckedPython -m app.smoke

$wheelDirectory = Join-Path $projectRoot "var\wheel-check"
uv build --wheel --out-dir $wheelDirectory --clear --no-build-logs
if ($LASTEXITCODE -ne 0) {
    throw "Wheel build failed with exit code ${LASTEXITCODE}."
}
$wheelPath = Get-ChildItem -LiteralPath $wheelDirectory -Filter "*.whl" |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $wheelPath) {
    throw "Wheel build completed without a wheel artifact."
}
Invoke-CheckedPython "tools\verify_wheel.py" $wheelPath.FullName
