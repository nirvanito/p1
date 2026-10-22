# Task Scheduler entry point: intake both inboxes — dictated text notes and
# audio (audio is transcribed with local whisper).
# Suggested task: daily 7:00 AM + at logon, "run only when user is logged on".
#   powershell -ExecutionPolicy Bypass -File "<project>\scripts\transcribe-inbox.ps1"
#
# Exits with intake's own exit code, so Task Scheduler's "Last Run Result"
# shows a failure (0x1) when any file failed or could not be archived.

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
# Task Scheduler starts in System32, where `python -m lifecrm` finds no package.
Set-Location $Root
# LIFECRM_ROOT (a restore target, a test run) wins, so its log stays with it.
$ProjectRoot = if ($env:LIFECRM_ROOT) { $env:LIFECRM_ROOT } else { $Root }
$LogDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "transcribe.log"

# Python writes UTF-8 (PYTHONUTF8); without this, PowerShell 5.1 decodes it
# with the console's OEM code page and every non-ASCII file name in the log
# is garbled. No console at all (some task contexts) is fine: skip it.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"$stamp starting inbox intake" | Add-Content -Encoding utf8 $Log
& "$Root\.venv\Scripts\python.exe" -m lifecrm intake 2>&1 |
    ForEach-Object { "$stamp $_" } | Add-Content -Encoding utf8 $Log

# PowerShell never passes a native command's exit code on by itself: without
# this line the task reports success whatever happened (measured in 1.5).
# $null means python never ran at all (no venv) — that is a failure too.
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
if ($code -ne 0) {
    "$stamp FAILED (exit $code) - see the lines above" | Add-Content -Encoding utf8 $Log
}
exit $code
