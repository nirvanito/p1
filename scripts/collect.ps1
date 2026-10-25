# Daily: run the imports [collect] sources names in config.toml - routine
# changes are applied, anything unusual (a source's first import, a large
# removal, records the reader cannot validate) is held and shows in /morning
# and doctor. With no [collect] sources it logs one line and exits 0.
# Registered daily 06:30 by scripts\install-tasks.ps1 - before intake, the
# reminders calendar and doctor, so all three see this morning's imports.
#   powershell -ExecutionPolicy Bypass -File scripts\collect.ps1

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
# Task Scheduler starts in System32, where `python -m lifecrm` finds no package.
Set-Location $Root
$ProjectRoot = if ($env:LIFECRM_ROOT) { $env:LIFECRM_ROOT } else { $Root }
$LogDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "collect.log"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"$stamp starting collect" | Add-Content -Encoding utf8 $Log
& "$Root\.venv\Scripts\python.exe" -m lifecrm collect --if-configured 2>&1 |
    ForEach-Object { "$stamp $_" } | Add-Content -Encoding utf8 $Log

# PowerShell never passes a native command's exit code on by itself (1.5).
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
if ($code -ne 0) {
    "$stamp FAILED (exit $code) - see the lines above" | Add-Content -Encoding utf8 $Log
}
exit $code
