# Restore the database from a snapshot. Without an argument, lists snapshots.
#   powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 [snapshot.db]
# Full instructions, including restoring on a machine that has never run this
# project, are in docs\restore.md.

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
# A relative snapshot path means the folder this was started from: make it
# absolute before moving to the repo root, where `python -m lifecrm` has to
# run (the venv does not install the package).
$Snapshot = $null
if ($args.Count -ge 1) {
    $Snapshot = [string]$args[0]
    if (-not [IO.Path]::IsPathRooted($Snapshot)) {
        $Snapshot = [IO.Path]::GetFullPath((Join-Path $PWD.ProviderPath $Snapshot))
    }
}
Set-Location $Root
if ($Snapshot) {
    & "$Root\.venv\Scripts\python.exe" -m lifecrm restore $Snapshot
} else {
    & "$Root\.venv\Scripts\python.exe" -m lifecrm restore
}
# "Stop" does not catch a native command's exit code (1.5).
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
exit $code
