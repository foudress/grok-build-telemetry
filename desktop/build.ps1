# Build the Windows shell and copy Token Telemetry.exe onto the desktop.
# The exe runs this repo's .venv. Dashboard and Python edits are picked up
# on the next launch, and while the app is open.
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$env:TT_REPO_ROOT = $repo
Push-Location (Join-Path $PSScriptRoot "src-tauri")
cargo build --release
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Pop-Location
$built = Join-Path $PSScriptRoot "src-tauri\target\release\token-telemetry.exe"
$desktop = [Environment]::GetFolderPath("Desktop")
Copy-Item -Force $built (Join-Path $desktop "Token Telemetry.exe")
Write-Output "desktop: $(Join-Path $desktop 'Token Telemetry.exe')"
