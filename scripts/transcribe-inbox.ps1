# Task Scheduler entry point: transcribe any new audio in the OneDrive inbox.
# Suggested task: daily 7:00 AM + at logon, "run only when user is logged on".
#   powershell -ExecutionPolicy Bypass -File "<project>\scripts\transcribe-inbox.ps1"

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
$Log = Join-Path $Root "data\transcribe.log"

$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
"$stamp starting inbox scan" | Add-Content -Encoding utf8 $Log
& "$Root\.venv\Scripts\python.exe" -m lifecrm transcribe 2>&1 |
    ForEach-Object { "$stamp $_" } | Add-Content -Encoding utf8 $Log
