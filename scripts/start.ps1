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
& (Join-Path $PSScriptRoot "start_query_backend.ps1") `
    -Port $Port `
    -Database $Database `
    -AllowedSchemas $AllowedSchemas `
    -Background:$runInBackground `
    -Foreground:$Foreground
exit $LASTEXITCODE
