param(
    [int]$Port = 58767
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $root ".team-hub\hub.pid"

function Test-HubProcess {
    param([int]$ProcessId)
    $info = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $info) { return $false }
    $cmd = [string]$info.CommandLine
    if ($cmd -notmatch '(^|\s|["\\/])hub\.py(\s|"|$)') { return $false }
    if ($cmd -match '--port\s+(\d+)' -and [int]$Matches[1] -ne $Port) { return $false }
    return $true
}

$targetPids = New-Object System.Collections.Generic.HashSet[int]
if (Test-Path $pidFile) {
    $raw = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ($raw -match '^\d+$') {
        $candidate = [int]$raw
        if (Test-HubProcess -ProcessId $candidate) {
            [void]$targetPids.Add($candidate)
        } else {
            Write-Warning "Ignored stale or non-Hub PID $candidate in $pidFile."
        }
    }
}
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    $owner = [int]$conn.OwningProcess
    if (Test-HubProcess -ProcessId $owner) {
        [void]$targetPids.Add($owner)
    }
}

if ($targetPids.Count -eq 0) {
    Write-Host "No running Team Hub process found on port $Port."
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
