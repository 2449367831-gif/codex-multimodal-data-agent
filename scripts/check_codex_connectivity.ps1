param(
    [ValidateRange(1, 60)]
    [int]$TimeoutSeconds = 20,
    [string]$Model = "gpt-6",
    [string]$BaseUrl = "http://192.168.110.9:8088",
    [string]$CodexHome
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $CodexHome) {
    $CodexHome = Join-Path $projectRoot "var\codex-home"
}
$authPath = Join-Path ([IO.Path]::GetFullPath($CodexHome)) "auth.json"
if (-not (Test-Path -LiteralPath $authPath)) {
    throw "Project model authentication is missing. Run scripts\configure_model_provider.ps1."
}

$auth = Get-Content -LiteralPath $authPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($auth.auth_mode -ne "apikey" -or -not $auth.OPENAI_API_KEY) {
    throw "Project auth.json must contain auth_mode=apikey and OPENAI_API_KEY."
}

$endpoint = $BaseUrl.TrimEnd("/")
$headers = @{
    Authorization = "Bearer $($auth.OPENAI_API_KEY)"
    "x-openai-actor-authorization" = "local-image-extension"
}
$watch = [Diagnostics.Stopwatch]::StartNew()
try {
    $response = Invoke-WebRequest `
        -Uri "$endpoint/v1/models" `
        -Headers $headers `
        -Method Get `
        -TimeoutSec $TimeoutSeconds `
        -SkipHttpErrorCheck
    $watch.Stop()
    $statusCode = [int]$response.StatusCode
    $modelAvailable = $false
    if ($statusCode -eq 200) {
        try {
            $modelDocument = $response.Content | ConvertFrom-Json
            $modelAvailable = @($modelDocument.data | Where-Object { $_.id -eq $Model }).Count -gt 0
        }
        catch {
            $modelAvailable = $null
        }
    }
    [pscustomobject]@{
        auth_source = "project"
        endpoint = $endpoint
        model = $Model
        reachable = $true
        status_code = $statusCode
        model_available = $modelAvailable
        elapsed_ms = $watch.ElapsedMilliseconds
        diagnostic = if ($statusCode -eq 200) { "ready" } else { "gateway rejected the request" }
    } | ConvertTo-Json
    if ($statusCode -ne 200) {
        exit 1
    }
}
catch {
    $watch.Stop()
    [pscustomobject]@{
        auth_source = "project"
        endpoint = $endpoint
        model = $Model
        reachable = $false
        status_code = $null
        model_available = $null
        elapsed_ms = $watch.ElapsedMilliseconds
        diagnostic = $_.Exception.GetType().Name
    } | ConvertTo-Json
    exit 1
}
finally {
    $headers.Clear()
    $auth = $null
}
