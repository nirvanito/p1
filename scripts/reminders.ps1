# Daily: republish the reminders calendar (due tasks and plans, birthdays) so
# an overdue item is re-dated to this morning and the phone's calendar sees a
# new revision. The same publication runs after every CLI command that
# changes the database; this job covers the mornings nothing was written.
# With no [assistant] reminders_dir in config.toml it logs one line and exits 0.
# Registered daily 07:15 by scripts\install-tasks.ps1 - after the 05:00
# rollover and after intake, before the day starts.
#   powershell -ExecutionPolicy Bypass -File scripts\reminders.ps1

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
# Task Scheduler starts in System32, where `python -m lifecrm` finds no package.
Set-Location $Root
$ProjectRoot = if ($env:LIFECRM_ROOT) { $env:LIFECRM_ROOT } else { $Root }
$LogDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "reminders.log"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"$stamp starting reminders calendar" | Add-Content -Encoding utf8 $Log
& "$Root\.venv\Scripts\python.exe" -m lifecrm reminders publish --if-configured 2>&1 |
    ForEach-Object { "$stamp $_" } | Add-Content -Encoding utf8 $Log

# PowerShell never passes a native command's exit code on by itself (1.5).
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
if ($code -ne 0) {
    "$stamp FAILED (exit $code) - see the lines above" | Add-Content -Encoding utf8 $Log
}
exit $code
