# Run doctor against the live database every day, so corruption and drift (an
# old backup, a dead cloud copy, a stuck inbox, an import gone quiet) surface
# without anyone remembering to look. verify-restore only ever doctors a
# scratch copy of last night's snapshot; this is the live one.
#
# Appends WARN/FAIL lines and the summary to data\doctor.log (the "ok" lines
# would bury them) and exits with doctor's own code, so Task Scheduler's
# "Last Run Result" shows a failure (0x1). Registered daily by
# scripts\install-tasks.ps1.
#   powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
# Task Scheduler starts in System32, where `python -m lifecrm` finds no package.
Set-Location $Root
# LIFECRM_ROOT (a restore target, a test run) wins, so its log stays with it.
$ProjectRoot = if ($env:LIFECRM_ROOT) { $env:LIFECRM_ROOT } else { $Root }
$LogDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "doctor.log"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"$stamp starting doctor" | Add-Content -Encoding utf8 $Log
& "$Root\.venv\Scripts\python.exe" -m lifecrm doctor 2>&1 |
    ForEach-Object { "$_" } | Where-Object { $_ -notmatch '^ok  ' } |
    ForEach-Object { "$stamp $_" } | Tee-Object -Variable lines |
    Add-Content -Encoding utf8 $Log

# PowerShell never passes a native command's exit code on by itself (1.5);
# $null means python never ran at all (no venv) - a failure too.
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
$lines | ForEach-Object { Write-Host $_ }
if ($code -ne 0) {
    "$stamp FAILED (exit $code) - see the lines above" | Add-Content -Encoding utf8 $Log
    Write-Host "$stamp FAILED (exit $code) - logged to $Log"
}
exit $code
