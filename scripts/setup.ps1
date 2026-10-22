# One-time setup: Python 3.12 venv, dependencies, database, git.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --- locate or install Python 3.12 (faster-whisper's ctranslate2 lags new CPython) ---
$py312 = $null
$candidates = @(
    "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "C:\Python312\python.exe"
)
foreach ($c in $candidates) { if (Test-Path $c) { $py312 = $c; break } }
if (-not $py312) {
    $pymgr = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WindowsApps" -Recurse -Filter "pymanager.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pymgr) {
        Write-Host "Installing Python 3.12 via Python Install Manager..."
        & $pymgr.FullName install 3.12
        $py312 = "$env:LOCALAPPDATA\Python\pythoncore-3.12-64\python.exe"
    }
}
if (-not $py312 -or -not (Test-Path $py312)) {
    Write-Error "Python 3.12 not found. Install it with:  winget install Python.Python.3.12  then re-run this script."
}
Write-Host "Using $py312"

# --- venv + dependencies ---
if (-not (Test-Path "$Root\.venv")) { & $py312 -m venv "$Root\.venv" }
& "$Root\.venv\Scripts\python.exe" -m pip install -q -U pip
& "$Root\.venv\Scripts\python.exe" -m pip install -q -r "$Root\requirements.txt"

# --- config + database ---
# configure proposes this PC's OneDrive VoiceJournal folders and timezone,
# creates the folders, and prints every value it chose; it never replaces an
# existing config.toml.
if (-not (Test-Path "$Root\config.toml")) {
    & "$Root\.venv\Scripts\python.exe" -m lifecrm configure
    if ($LASTEXITCODE -ne 0) { Write-Error "lifecrm configure failed - see the line above" }
}
& "$Root\.venv\Scripts\python.exe" -m lifecrm init

# --- git (code only; data\ and backups\ are gitignored) ---
if (-not (Test-Path "$Root\.git")) { git init | Out-Null; git add -A; git commit -m "Initial commit" | Out-Null }

Write-Host ""
Write-Host "Setup complete. Try:  .venv\Scripts\python.exe -m lifecrm where"
Write-Host "(lists the journal folders and everything else this PC can import)"
