# Build the distributable Windows x64 setup executable.
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$tauri = Join-Path $PSScriptRoot "src-tauri"
$venv = Join-Path $repo ".venv/Scripts/python.exe"
$version = (Get-Content -Raw (Join-Path $tauri "tauri.conf.json") | ConvertFrom-Json).version
$basePython = & $venv -c "import sys, sysconfig; assert sys.version_info[:2] == (3, 12) and sysconfig.get_platform() == 'win-amd64'; print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0) { throw "Install the project's Windows x64 Python 3.12 environment first." }

$payloadRelative = "target/installer/$([Guid]::NewGuid())/runtime"
$payload = Join-Path $tauri $payloadRelative
$python = Join-Path $payload "python"
New-Item -ItemType Directory -Path $python -Force | Out-Null

# Only ship source assets known to Git. Never traverse local telemetry files.
$files = git -C $repo ls-files -- LICENSE pyproject.toml launch_dashboard.py dashboard token_telemetry vendor
if ($LASTEXITCODE -ne 0) { throw "Could not enumerate the application files." }
foreach ($file in $files) {
    $destination = Join-Path $payload $file
    New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $repo $file) -Destination $destination
}

foreach ($file in @("python.exe", "python3.dll", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")) {
    Copy-Item -LiteralPath (Join-Path $basePython $file) -Destination $python
}
foreach ($folder in @("Lib", "DLLs")) {
    robocopy (Join-Path $basePython $folder) (Join-Path $python $folder) /E /XD __pycache__ test tests site-packages /XF *.pyc /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "Could not copy Python's $folder directory." }
}

# Isolate the bundled Python from user-installed packages and PYTHONPATH.
@("Lib", "DLLs", "Lib/site-packages", "..") |
    Set-Content -Encoding Ascii (Join-Path $python "python312._pth")
uv pip install --python $venv --target (Join-Path $python "Lib/site-packages") --require-hashes --only-binary :all: -r (Join-Path $repo "requirements-windows-py312.lock")
if ($LASTEXITCODE -ne 0) { throw "Could not stage the locked Python dependencies." }

$bundledPython = Join-Path $python "python.exe"
$verifyRuntime = @'
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import zstandard
from token_telemetry.tokenizer import tokenizer_info

ZoneInfo('America/Los_Angeles')
os.environ['GROK_TOKENIZER'] = 'grok2'
os.environ.pop('GROK_TOKENIZER_PATH', None)
os.environ.pop('GROK2_TOKENIZER_PATH', None)
info = tokenizer_info()
assert info['mode'] == 'grok2' and Path(info['source']) == Path(info['vendor_dir'])
'@
& $bundledPython -c $verifyRuntime
if ($LASTEXITCODE -ne 0) { throw "The staged Python runtime failed its import checks." }

$config = Join-Path (Split-Path $payload -Parent) "tauri.installer.json"
$configJson = @{ bundle = @{ resources = @{ "$payloadRelative/" = "runtime/" } } } |
    ConvertTo-Json -Depth 4
[IO.File]::WriteAllText($config, $configJson)

$target = "x86_64-pc-windows-msvc"
$previousRepoRoot = $env:TT_REPO_ROOT
Push-Location $PSScriptRoot
try {
    $env:TT_REPO_ROOT = $null
    npm exec --yes --cache (Join-Path $repo ".grok/npm-cache") --package=@tauri-apps/cli@2.12.6 -- tauri build --config $config --target $target --bundles nsis -- --locked
    if ($LASTEXITCODE -ne 0) { throw "Could not build the Tauri setup executable." }
}
finally {
    $env:TT_REPO_ROOT = $previousRepoRoot
    Pop-Location
}

$setup = Join-Path $tauri "target/$target/release/bundle/nsis/Token Telemetry_${version}_x64-setup.exe"
$dist = Join-Path $repo "dist"
New-Item -ItemType Directory -Path $dist -Force | Out-Null
$output = Join-Path $dist "Token-Telemetry-v$version-windows-x64-setup.exe"
Copy-Item -LiteralPath $setup -Destination $output -Force
Write-Output $output
