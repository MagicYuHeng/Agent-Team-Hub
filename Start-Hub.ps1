param(
    [ValidateRange(1, 65535)]
    [int]$Port = 58767,
    [string]$Workspace = ".\fixtures",
    [string]$Database = ".team-hub\team.db"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Quote-Arg {
    param([string]$Value)
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }
    return $Value
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) { $pythonCmd = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $pythonCmd) { throw "Python 3.11+ not found in PATH. Please install Python 3.11+ or add it to PATH." }
$python = $pythonCmd.Source
$hubScript = Join-Path $root "hub.py"

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
    $ownerInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($existing.OwningProcess)" -ErrorAction SilentlyContinue
    $ownerCmd = if ($ownerInfo) { [string]$ownerInfo.CommandLine } else { "" }
    $isThisCheckout = $ownerCmd.IndexOf($hubScript, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($health.status -eq "ok" -and $isThisCheckout) {
            Write-Host "Team Hub from this checkout is already running on http://127.0.0.1:$Port (PID $($existing.OwningProcess))."
            exit 0
        }
        if ($health.status -eq "ok") {
            throw "Port $Port is already serving Team Hub from another checkout or process (PID $($existing.OwningProcess))."
        }
    } catch {
        if ($_.Exception.Message -like "Port $Port is already serving*") { throw }
    }
    throw "Port $Port is already occupied by PID $($existing.OwningProcess)."
}

$dataDir = Join-Path $root ".team-hub"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$outLog = Join-Path $dataDir "hub-$Port.out.log"
$errLog = Join-Path $dataDir "hub-$Port.err.log"
$pidFile = Join-Path $dataDir "hub-$Port.pid"

$argString = (@(
    (Quote-Arg $hubScript),
    "--workspace", (Quote-Arg $Workspace),
    "--port", "$Port",
    "--database", (Quote-Arg $Database)
) -join " ")

$proc = Start-Process -FilePath $python `
    -ArgumentList $argString `
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
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        $errText = if (Test-Path $errLog) { Get-Content $errLog -Raw } else { "" }
        throw "Team Hub exited unexpectedly (code $($proc.ExitCode)): $errText"
    }
}
throw "Team Hub started (PID $($proc.Id)) but /health did not respond within 5s."
