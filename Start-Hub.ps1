param(
    [int]$Port = 58767,
    [string]$Workspace = ".\fixtures",
    [string]$Database = ".team-hub\team.db"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) { $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $pythonCmd) { throw "Python 3.11+ not found in PATH. Please install Python 3.11+ or add it to PATH." }
$python = $pythonCmd.Source

$token = $env:TEAM_HUB_TOKEN
if (-not $token) {
    $token = [Environment]::GetEnvironmentVariable("TEAM_HUB_TOKEN", "User")
}
if (-not $token) {
    $token = & $python -c "import secrets; print(secrets.token_urlsafe(32))"
    [Environment]::SetEnvironmentVariable("TEAM_HUB_TOKEN", $token, "User")
    Write-Host "Generated new user-level TEAM_HUB_TOKEN. Restart clients so they inherit it."
}
$env:TEAM_HUB_TOKEN = $token

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            Write-Host "Team Hub is already running on http://127.0.0.1:$Port (PID $($existing.OwningProcess))."
            exit 0
        }
    } catch {}
    throw "Port $Port is already occupied by PID $($existing.OwningProcess)."
}

$dataDir = Join-Path $root ".team-hub"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$outLog = Join-Path $dataDir "hub.out.log"
$errLog = Join-Path $dataDir "hub.err.log"
$pidFile = Join-Path $dataDir "hub.pid"

$proc = Start-Process -FilePath $python `
    -ArgumentList @("hub.py", "--workspace", $Workspace, "--port", "$Port", "--database", $Database) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ascii

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 250
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            Write-Host "Team Hub started on http://127.0.0.1:$Port (PID $($proc.Id))."
            Write-Host "Logs: $outLog | $errLog"
            exit 0
        }
    } catch {}
    if ($proc.HasExited) {
        $errText = if (Test-Path $errLog) { Get-Content $errLog -Raw } else { "" }
        throw "Team Hub exited unexpectedly (code $($proc.ExitCode)): $errText"
    }
}
throw "Team Hub started (PID $($proc.Id)) but /health did not respond within 5s."
