param(
    [ValidateRange(1, 65535)]
    [int]$Port = 58767
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$hubScript = Join-Path $root "hub.py"
$pidFile = Join-Path $root ".team-hub\hub-$Port.pid"
$legacyPidFile = Join-Path $root ".team-hub\hub.pid"
if (-not (Test-Path $pidFile) -and $Port -eq 58767 -and (Test-Path $legacyPidFile)) {
    $pidFile = $legacyPidFile
}
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1

if (-not $conn) {
    Write-Host "Status: STOPPED (port $Port is not listening)"
    exit 1
}

$procInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue
$healthStatus = "unreachable"
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    $healthStatus = $health.status
} catch {}

$ownerTag = "other process/checkout"
if ($procInfo -and ([string]$procInfo.CommandLine).IndexOf($hubScript, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
    $ownerTag = "this checkout"
}

Write-Host "Status: RUNNING ($ownerTag)"
Write-Host "Endpoint: http://127.0.0.1:$Port (health=$healthStatus)"
Write-Host "PID: $($conn.OwningProcess)"
if ($procInfo) { Write-Host "Command: $($procInfo.CommandLine)" }
if (Test-Path $pidFile) { Write-Host "PID file: $((Get-Content -LiteralPath $pidFile -Raw).Trim())" }
