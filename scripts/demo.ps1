# One command to see the whole thing on Windows: build the demo instance
# (Windows laptop + Samsung phone, synthetic data only) and open the web UI.
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1            # android combo
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Combo iphone
#   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Rebuild
# Nothing here touches data\ — the demo lives in demo\<combo>\ (gitignored).

param(
    [ValidateSet("android", "iphone")] [string] $Combo = "android",
    [switch] $Rebuild,
    [switch] $NoServe,
    [int] $Port = 8765
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Error "no venv yet - run: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1"
}
$Demo = Join-Path $Root "demo\$Combo"

if ($Rebuild -or -not (Test-Path (Join-Path $Demo ".lifecrm-demo"))) {
    $build = @("-m", "lifecrm", "demo", "--combo", $Combo)
    if ($Rebuild) { $build += "--force" }
    & $Py @build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "demo already built at $Demo (use -Rebuild for a fresh one)"
}

if ($NoServe) { exit 0 }
Write-Host ""
Write-Host "opening the web UI at http://127.0.0.1:$Port/ (Ctrl+C stops it)"
Start-Process "http://127.0.0.1:$Port/"
& $Py -m lifecrm --root $Demo serve --port $Port
exit $LASTEXITCODE
