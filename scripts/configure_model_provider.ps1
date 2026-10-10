param(
    [Security.SecureString]$ApiKey,
    [string]$CodexHome
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $CodexHome) {
    $CodexHome = Join-Path $projectRoot "var\codex-home"
}
$CodexHome = [IO.Path]::GetFullPath($CodexHome)

if (-not $ApiKey) {
    $ApiKey = Read-Host "Agent Gateway API key" -AsSecureString
}

function Set-PrivateAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [bool]$Directory
    )

    $item = Get-Item -LiteralPath $Path -Force
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($Directory) {
        $rules = @(
            "*$($currentSid):(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F"
        )
    }
    else {
        $rules = @(
            "*$($currentSid):F",
            "*S-1-5-18:F",
            "*S-1-5-32-544:F"
        )
    }
    & icacls.exe $item.FullName "/inheritance:r" "/grant:r" $rules | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply private ACL to $($item.FullName)"
    }

    $verified = Get-Acl -LiteralPath $item.FullName
    if (-not $verified.AreAccessRulesProtected) {
        throw "Failed to disable ACL inheritance for $($item.FullName)"
    }
    $allowedSids = @($currentSid, "S-1-5-18", "S-1-5-32-544")
    $unexpected = @($verified.Access | Where-Object {
        if ($_.AccessControlType -ne "Allow") {
            return $false
        }
        try {
            $sid = $_.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            return $true
        }
        return $sid -notin $allowedSids
    })
    if ($unexpected.Count -gt 0) {
        throw "Unexpected ACL entry remains on $($item.FullName)"
    }
}

$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ApiKey)
try {
    $plainText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($plainText) -or $plainText -match "\s") {
        throw "API key must be non-empty and contain no whitespace."
    }

    New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
    Set-PrivateAcl -Path $CodexHome -Directory $true
    $authPath = Join-Path $CodexHome "auth.json"
    if (Test-Path -LiteralPath $authPath) {
        Set-PrivateAcl -Path $authPath -Directory $false
    }

    $document = [ordered]@{
        auth_mode = "apikey"
        OPENAI_API_KEY = $plainText
    } | ConvertTo-Json
    [IO.File]::WriteAllText($authPath, $document, [Text.UTF8Encoding]::new($false))

    foreach ($file in @(Get-ChildItem -LiteralPath $CodexHome -File -Force)) {
        Set-PrivateAcl -Path $file.FullName -Directory $false
    }

    $parsed = Get-Content -LiteralPath $authPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($parsed.auth_mode -ne "apikey" -or -not $parsed.OPENAI_API_KEY) {
        throw "Generated auth.json failed validation."
    }
    Write-Host "Project model authentication configured: $authPath"
}
finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $plainText = $null
    $document = $null
    $parsed = $null
}
