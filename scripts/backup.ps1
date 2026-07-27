# Snapshot the database (SQLite backup API — never a raw copy of a live WAL DB)
# to backups\ and the OneDrive backups folder, keeping the newest 8 of each.
#   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
& "$Root\.venv\Scripts\python.exe" -m lifecrm backup
