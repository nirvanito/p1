# An untested backup is not a backup. Restore the newest local snapshot into a
# scratch folder, run doctor against it, and check it holds every row the live
# database had when it was taken - every table, not a sample. Never touches
# data\lifecrm.db.
#
# The checks live in `lifecrm verify-backup` so the Mac's verify-restore.sh
# runs exactly the same ones; this is the Task Scheduler wrapper. It appends
# to data\verify-restore.log and exits with verify-backup's own code, so
# "Last Run Result" shows a failure (0x1) when the backup does not hold up.
# Registered weekly by scripts\install-tasks.ps1.
#   powershell -ExecutionPolicy Bypass -File scripts\verify-restore.ps1 [snapshot.db]

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
# A relative snapshot path means the folder this was started from: make it
# absolute before leaving that folder.
$Passed = @($args | ForEach-Object {
    $a = [string]$_
    if ($a.StartsWith("-") -or [IO.Path]::IsPathRooted($a)) { $a }
    else { [IO.Path]::GetFullPath((Join-Path $PWD.ProviderPath $a)) }
})
# Task Scheduler starts in System32, where `python -m lifecrm` finds no package.
Set-Location $Root
# LIFECRM_ROOT (a restore target, a test run) wins, so its log stays with it.
$ProjectRoot = if ($env:LIFECRM_ROOT) { $env:LIFECRM_ROOT } else { $Root }
$LogDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "verify-restore.log"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"$stamp starting verify-backup" | Add-Content -Encoding utf8 $Log
$verify = @("-m", "lifecrm", "verify-backup") + $Passed
& "$Root\.venv\Scripts\python.exe" @verify 2>&1 |
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
