# An untested backup is not a backup. Restore the newest LOCAL snapshot into a
# scratch directory, run doctor against it, compare row counts to the live
# database, report, clean up. Never touches data\lifecrm.db.
# Suggested Task Scheduler trigger: weekly.
#   powershell -ExecutionPolicy Bypass -File scripts\verify-restore.ps1

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
$Py = "$Root\.venv\Scripts\python.exe"
Set-Location $Root

$Snapshot = Get-ChildItem "$Root\backups\lifecrm-*.db" -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -Last 1
if (-not $Snapshot) {
    Write-Error "no local snapshots found - run scripts\backup.ps1 first"
}
Write-Host "verifying snapshot: $($Snapshot.Name)"

# live row counts (before switching roots)
$live = & $Py -m lifecrm sql "SELECT (SELECT COUNT(*) FROM entries) AS entries, (SELECT COUNT(*) FROM sources) AS sources, (SELECT COUNT(*) FROM entities) AS entities" --json | ConvertFrom-Json

$Scratch = Join-Path $env:TEMP ("lifecrm-verify-" + (Get-Date -Format "yyyyMMddHHmmss"))
New-Item -ItemType Directory -Force "$Scratch" | Out-Null
"[paths]`ndata_dir = `"data`"`nbackups_dir = `"backups`"`n[time]`ntimezone = `"America/Chicago`"`n" |
    Set-Content -Encoding utf8 "$Scratch\config.toml"

$env:LIFECRM_ROOT = $Scratch
try {
    & $Py -m lifecrm restore $Snapshot.FullName
    if ($LASTEXITCODE -ne 0) { throw "restore into scratch failed" }

    & $Py -m lifecrm doctor
    $doctorExit = $LASTEXITCODE

    $restored = & $Py -m lifecrm sql "SELECT (SELECT COUNT(*) FROM entries) AS entries, (SELECT COUNT(*) FROM sources) AS sources, (SELECT COUNT(*) FROM entities) AS entities" --json | ConvertFrom-Json
} finally {
    $env:LIFECRM_ROOT = $null
    Remove-Item -Recurse -Force "$Scratch" -ErrorAction SilentlyContinue
}

Write-Host ("row counts  live: entries=$($live.entries) sources=$($live.sources) entities=$($live.entities)")
Write-Host ("        restored: entries=$($restored.entries) sources=$($restored.sources) entities=$($restored.entities)")
if ($doctorExit -ne 0) {
    Write-Error "doctor found FAILURES in the restored snapshot"
}
if ($restored.entries -gt $live.entries -or $restored.sources -gt $live.sources) {
    Write-Error "restored snapshot has MORE rows than live - investigate"
}
if ($live.entries -ne $restored.entries) {
    Write-Host "note: live has entries newer than the snapshot (expected if the last backup predates recent logging)"
}
Write-Host "verify-restore OK: snapshot restores cleanly and passes doctor"
