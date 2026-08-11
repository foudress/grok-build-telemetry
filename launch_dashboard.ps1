# Launch the near-real-time token/cost companion (browser UI).
# Usage: .\launch_dashboard.ps1  [-Port 8765]  [-SessionId <uuid>]

param(
    [int]$Port = 8765,
    [string]$SessionId = ""
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Root "scripts\live_dashboard.py"

$argsList = @($Script, "--port", "$Port")
if ($SessionId) {
    $argsList += @("--session-id", $SessionId)
}

Write-Host "Starting Grok Token Telemetry dashboard on http://127.0.0.1:$Port/"
python @argsList
