# Snapshot the database (SQLite backup API — never a raw copy of a live WAL DB)
# to backups\ and the cloud backups folder, with tiered retention.
#   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
& "$Root\.venv\Scripts\python.exe" -m lifecrm backup
# "Stop" does not catch a native command's exit code; without this the task
# reports success even when the backup failed (measured in 1.5).
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
exit $code
