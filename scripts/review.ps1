# Weekly: build last week's review pack (every lens) so it is waiting in
# data\staging\ when you ask Claude for /review-week or /advise. Reads only -
# the pack is a file; nothing reaches the database without the review gate.
# Registered Mondays 07:30 by scripts\install-tasks.ps1 - after the rollover,
# so "last week" is the week that has just ended.
#   powershell -ExecutionPolicy Bypass -File scripts\review.ps1

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
# Task Scheduler starts in System32, where `python -m lifecrm` finds no package.
Set-Location $Root
$ProjectRoot = if ($env:LIFECRM_ROOT) { $env:LIFECRM_ROOT } else { $Root }
$LogDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "review.log"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"$stamp starting weekly review pack" | Add-Content -Encoding utf8 $Log
& "$Root\.venv\Scripts\python.exe" -m lifecrm review --lens all --write-only 2>&1 |
    ForEach-Object { "$stamp $_" } | Add-Content -Encoding utf8 $Log

# PowerShell never passes a native command's exit code on by itself (1.5).
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
if ($code -ne 0) {
    "$stamp FAILED (exit $code) - see the lines above" | Add-Content -Encoding utf8 $Log
}
exit $code
