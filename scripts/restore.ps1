# Restore the database from a snapshot. Without an argument, lists snapshots.
#   powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 [snapshot.db]
# Full instructions, including restoring on a machine that has never run this
# project, are in docs\restore.md.

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
if ($args.Count -ge 1) {
    & "$Root\.venv\Scripts\python.exe" -m lifecrm restore $args[0]
} else {
    & "$Root\.venv\Scripts\python.exe" -m lifecrm restore
}
