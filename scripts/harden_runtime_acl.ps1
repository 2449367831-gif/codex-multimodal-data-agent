param(
    [string]$RuntimeRoot
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $projectRoot "var"
}
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)

# The chat process stores Codex transcripts, copied auth, session ownership,
# and audit metadata under these paths. Run this script as the same Windows
# service account that will run start_chat/start_query_backend.
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowedSids = @($currentSid, "S-1-5-18", "S-1-5-32-544")

function Protect-Path {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [Parameter(Mandatory = $true)] [bool]$Directory
    )

    $item = Get-Item -LiteralPath $Path -Force
    $acl = if ($Directory) {
        [Security.AccessControl.DirectorySecurity]::new()
    }
    else {
        [Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    else {
        [Security.AccessControl.InheritanceFlags]::None
    }
    foreach ($sidValue in $allowedSids) {
        $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    $item.SetAccessControl($acl)
    $acl = Get-Acl -LiteralPath $item.FullName
    if (-not $acl.AreAccessRulesProtected) {
        throw "ACL inheritance remains enabled on $($item.FullName)"
    }
    $unexpected = @($acl.Access | Where-Object {
        if ($_.AccessControlType -ne "Allow") { return $false }
        try {
            $sid = $_.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch { return $true }
        return $sid -notin $allowedSids
    })
    if ($unexpected.Count -gt 0) {
        throw "Unexpected ACL entry remains on $($item.FullName)"
    }
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
Protect-Path -Path $RuntimeRoot -Directory $true

$sensitiveDirectories = @(
    (Join-Path $RuntimeRoot "codex-home"),
    (Join-Path $RuntimeRoot "codex-contexts"),
    (Join-Path $RuntimeRoot "chat-ipc")
)
foreach ($directory in $sensitiveDirectories) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    Protect-Path -Path $directory -Directory $true
}

# Existing files/subdirectories are explicitly protected once; new children
# inherit the OI/CI ACL from their protected parent.
$sensitiveDirectories | ForEach-Object {
    $root = Get-Item -LiteralPath $_ -Force
    Get-ChildItem -LiteralPath $root.FullName -Force -Recurse -ErrorAction Stop |
        Where-Object {
            -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
        } |
        Sort-Object { $_.FullName.Length } -Descending |
        ForEach-Object {
            Protect-Path -Path $_.FullName -Directory $_.PSIsContainer
        }
}

$sensitiveFiles = @(
    "chat-sessions.sqlite3",
    "chat-sessions.sqlite3-wal",
    "chat-sessions.sqlite3-shm",
    "chat-sessions.sqlite3-journal",
    "installation-identity.json",
    "audit.sqlite3",
    "audit.sqlite3-wal",
    "audit.sqlite3-shm",
    "audit.sqlite3-journal",
    "chat-api.log",
    "chat-api.err.log"
)
foreach ($name in $sensitiveFiles) {
    $file = Join-Path $RuntimeRoot $name
    if (Test-Path -LiteralPath $file -PathType Leaf) {
        Protect-Path -Path $file -Directory $false
    }
}

Write-Host "Runtime ACL protected: $RuntimeRoot"
