param(
    [int]$Port = 58767
)

$ErrorActionPreference = 'Stop'
$teamHubToken = $env:TEAM_HUB_TOKEN
if (-not $teamHubToken) { $teamHubToken = [Environment]::GetEnvironmentVariable('TEAM_HUB_TOKEN', 'User') }
if (-not $teamHubToken) { throw 'TEAM_HUB_TOKEN is missing. Start Hub using Start-Hub.ps1 or README.md first.' }
$teamHubFragment = [Uri]::EscapeDataString($teamHubToken)
Start-Process "http://127.0.0.1:$Port/manage#token=$teamHubFragment"
