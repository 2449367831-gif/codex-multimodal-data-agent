param(
    [Parameter(Mandatory = $true, Position = 0)]
    [Alias("IP")]
    [string[]]$IPAddress,
    [switch]$Restart,
    [switch]$Remove,
    [switch]$ConfigureFirewall,
    [switch]$FirewallOnly,
    [string]$FirewallRemoteAddress = "192.168.51.0/24"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"
$backupPath = "$envPath.bak"

function Read-Dotenv {
    $values = @{}
    if (Test-Path -LiteralPath $envPath) {
        foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
                $values[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
            }
        }
    }
    return $values
}

function Test-PrivateAddress {
    param([System.Net.IPAddress]$address)
    if ($address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        $bytes = $address.GetAddressBytes()
        return (
            ($bytes[0] -eq 10) -or
            ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
            ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
        )
    }
    if ($address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) {
        return (($address.GetAddressBytes()[0] -band 0xfe) -eq 0xfc)
    }
    return $false
}

function Normalize-ClientIp {
    param([string]$value)
    $candidate = if ($null -eq $value) { "" } else { $value.Trim() }
    if (-not $candidate) { throw "IP address cannot be empty." }
    if ($candidate.Contains('/')) {
        throw "Pass one client IP, not a network range: $candidate"
    }
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($candidate, [ref]$parsed)) {
        throw "Invalid IP address: $candidate"
    }
    if (-not (Test-PrivateAddress $parsed)) {
        throw "Only private LAN IP addresses are allowed: $candidate"
    }
    return $parsed.ToString()
}

$values = Read-Dotenv
$existing = @()
if ($values.ContainsKey("AGENT_CHAT_ALLOWED_CIDRS") -and $values["AGENT_CHAT_ALLOWED_CIDRS"].Trim()) {
    $existing = @($values["AGENT_CHAT_ALLOWED_CIDRS"].Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$requestedIps = @($IPAddress | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() })
$normalized = @($requestedIps | ForEach-Object { Normalize-ClientIp $_ })
$current = @($existing)
if ($Remove) {
    $current = @($current | Where-Object { $normalized -notcontains ($_ -replace '/32$|/128$', '') })
} else {
    foreach ($ip in $normalized) {
        if ($current -notcontains "$ip/32" -and $current -notcontains "$ip/128" -and $current -notcontains $ip) {
            $suffix = if ($ip.Contains(':')) { "/128" } else { "/32" }
            $current += "$ip$suffix"
        }
    }
}

if (-not $FirewallOnly -and $current.Count -eq 0) {
    throw "The allowlist cannot be empty. Keep at least one tester IP or switch AGENT_CHAT_NETWORK_MODE back to loopback."
}

if (-not $FirewallOnly) {
    $values["AGENT_CHAT_NETWORK_MODE"] = "lan"
    $values["AGENT_CHAT_HOST"] = "0.0.0.0"
    $values["AGENT_CHAT_ALLOWED_CIDRS"] = ($current -join ',')

    $lines = @()
    if (Test-Path -LiteralPath $envPath) {
        $lines = @(Get-Content -LiteralPath $envPath -Encoding UTF8)
    }
    $managedKeys = @("AGENT_CHAT_NETWORK_MODE", "AGENT_CHAT_HOST", "AGENT_CHAT_ALLOWED_CIDRS")
    $seen = @{}
    $output = foreach ($line in $lines) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $key = $matches[1]
            if ($managedKeys -contains $key) {
                if (-not $seen.ContainsKey($key)) {
                    $seen[$key] = $true
                    "$key=$($values[$key])"
                }
                continue
            }
        }
        $line
    }
    foreach ($key in $managedKeys) {
        if (-not $seen.ContainsKey($key)) { $output += "$key=$($values[$key])" }
    }

    if (Test-Path -LiteralPath $envPath) { Copy-Item -LiteralPath $envPath -Destination $backupPath -Force }
    $tempPath = "$envPath.tmp"
    Set-Content -LiteralPath $tempPath -Value $output -Encoding UTF8
    Move-Item -LiteralPath $tempPath -Destination $envPath -Force

    Write-Host "LAN allowlist updated: $($current -join ', ')"
    Write-Host "Backup: $backupPath"
} elseif (-not $ConfigureFirewall) {
    throw "FirewallOnly requires -ConfigureFirewall."
}

if ($ConfigureFirewall) {
    $ruleName = "RoyalTigerQueryAgent-LAN-8021"
    try {
        $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
        if ($rule) {
            # The application decides whether an IP is approved. The firewall
            # must leave the request page reachable for unapproved LAN clients.
            $rule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $FirewallRemoteAddress
        } else {
            New-NetFirewallRule `
                -DisplayName $ruleName `
                -Direction Inbound `
                -Action Allow `
                -Protocol TCP `
                -LocalPort 8021 `
                -RemoteAddress $FirewallRemoteAddress `
                -Profile Domain,Private `
                -Description "Allow only approved private clients for the Royal Tiger query agent" | Out-Null
        }
        Write-Host "Windows Firewall rule updated: $ruleName"
    } catch {
        Write-Warning "Application allowlist updated, but Windows Firewall was not changed. Run this script from an Administrator PowerShell with -ConfigureFirewall."
    }
}

if ($Restart) {
    & (Join-Path $PSScriptRoot "restart_hidden.ps1") -StartNow
    Write-Host "Service restart requested. Check http://<server-ip>:8021/ after startup."
}
