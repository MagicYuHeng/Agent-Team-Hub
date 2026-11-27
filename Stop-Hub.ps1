param(
    [ValidateRange(1, 65535)]
    [int]$Port = 58767
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$hubScript = Join-Path $root "hub.py"
$pidFile = Join-Path $root ".team-hub\hub-$Port.pid"
$legacyPidFile = Join-Path $root ".team-hub\hub.pid"
$legacyPid = $null
if (-not (Test-Path $pidFile) -and $Port -eq 58767 -and (Test-Path $legacyPidFile)) {
    $pidFile = $legacyPidFile
    $rawLegacy = (Get-Content -LiteralPath $legacyPidFile -Raw).Trim()
    if ($rawLegacy -match '^\d+$') { $legacyPid = [int]$rawLegacy }
}

$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1

function Test-HubProcess {
    param([int]$ProcessId)
    $info = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $info) { return $false }
    $cmd = [string]$info.CommandLine
    $cmdPort = 58767
    if ($cmd -match '(?:^|\s)--port(?:=|\s+)(\d+)(?:\s|$)') {
        $cmdPort = [int]$Matches[1]
    }
    if ($cmdPort -ne $Port) { return $false }
    if ($cmd.IndexOf($hubScript, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return $true
    }
    # Legacy upgrade compatibility: allow stopping relative 'hub.py' only when PID matches this checkout's legacy hub.pid AND owns port 58767.
    if ($null -ne $legacyPid -and $ProcessId -eq $legacyPid -and $conn -and [int]$conn.OwningProcess -eq $ProcessId) {
        return $cmd -match '(^|\s|["\\/])hub\.py(\s|"|$)'
    }
    return $false
}

$targetPids = New-Object System.Collections.Generic.HashSet[int]
if (Test-Path $pidFile) {
    $raw = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($raw -match '^\d+$') {
        $candidate = [int]$raw
        if (Test-HubProcess -ProcessId $candidate) {
            [void]$targetPids.Add($candidate)
        } else {
            Write-Warning "Ignored stale or non-matching PID $candidate in $pidFile."
        }
    }
}
if ($conn) {
    $owner = [int]$conn.OwningProcess
    if (Test-HubProcess -ProcessId $owner) {
        [void]$targetPids.Add($owner)
    }
}

if ($targetPids.Count -eq 0) {
    Write-Host "No running Team Hub process from this checkout found on port $Port."
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

foreach ($pidNum in $targetPids) {
    $p = Get-Process -Id $pidNum -ErrorAction SilentlyContinue
    if ($p) {
        Stop-Process -Id $pidNum -Force
        Write-Host "Stopped Team Hub PID $pidNum."
    }
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 300
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) {
    Write-Warning "Port $Port is still listening (PID $($still.OwningProcess))."
    exit 1
}
Write-Host "Team Hub stopped cleanly."
