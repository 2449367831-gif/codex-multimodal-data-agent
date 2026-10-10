param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Question,
    [string]$Model = "gpt-6",
    [ValidateSet("none", "minimal", "low", "medium", "high", "xhigh")]
    [string]$ReasoningEffort = "low",
    [string]$ModelProvider = "agent_gateway",
    [string]$ModelBaseUrl = "http://192.168.110.9:8088",
    [string]$BackendUrl = "http://127.0.0.1:8010"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Missing .venv. Run scripts\setup.ps1 first."
}
$codexHome = Join-Path $projectRoot "var\codex-home"
$authPath = Join-Path $codexHome "auth.json"
if (-not (Test-Path -LiteralPath $authPath)) {
    throw "Missing project model authentication. Run scripts\configure_model_provider.ps1 first."
}

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $pythonPath
$startInfo.WorkingDirectory = $projectRoot
$startInfo.UseShellExecute = $false
$startInfo.ArgumentList.Add("-m")
$startInfo.ArgumentList.Add("app.codex_agent")
$startInfo.ArgumentList.Add($Question)
$startInfo.ArgumentList.Add("--model")
$startInfo.ArgumentList.Add($Model)
$startInfo.ArgumentList.Add("--reasoning-effort")
$startInfo.ArgumentList.Add($ReasoningEffort)
$startInfo.ArgumentList.Add("--model-provider")
$startInfo.ArgumentList.Add($ModelProvider)
$startInfo.ArgumentList.Add("--model-base-url")
$startInfo.ArgumentList.Add($ModelBaseUrl)
$startInfo.ArgumentList.Add("--codex-home")
$startInfo.ArgumentList.Add($codexHome)
$startInfo.ArgumentList.Add("--backend-url")
$startInfo.ArgumentList.Add($BackendUrl)

$sensitiveKeys = @(
    "DORIS_HOST",
    "DORIS_PORT",
    "DORIS_USER",
    "DORIS_PASSWORD",
    "DORIS_DATABASE",
    "AGENT_TRUSTED_RELEASE_SHA256",
    "AGENT_ALLOW_UNAPPROVED_CONTRACTS",
    "AGENT_CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY"
)
foreach ($key in $sensitiveKeys) {
    $startInfo.Environment.Remove($key) | Out-Null
}
$startInfo.Environment["AGENT_QUERY_BACKEND_URL"] = $BackendUrl
$startInfo.Environment["AGENT_CODEX_HOME"] = $codexHome
$startInfo.Environment["AGENT_CODEX_BASE_URL"] = $ModelBaseUrl
$startInfo.Environment["AGENT_CODEX_MODEL_PROVIDER"] = $ModelProvider
$startInfo.Environment["AGENT_CODEX_MODEL"] = $Model
$startInfo.Environment["AGENT_CODEX_REASONING_EFFORT"] = $ReasoningEffort

$process = [System.Diagnostics.Process]::Start($startInfo)
$process.WaitForExit()
exit $process.ExitCode
