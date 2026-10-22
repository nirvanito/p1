#!/bin/bash
# One-time setup on a Mac: Python 3.12 venv, dependencies, config.toml for
# this machine, database, git. The macOS counterpart of setup.ps1.
# Run from anywhere:  /bin/bash scripts/setup.sh
#
# Python 3.12 exactly: faster-whisper's ctranslate2 lags new CPython releases,
# and the /usr/bin/python3 macOS ships (3.9) has no tomllib.

set -e
. "$(dirname "$0")/_common.sh" || exit 1
ROOT="$LIFECRM_REPO"

# --- where the repo lives ---
# With iCloud "Desktop & Documents Folders" on, a repo under ~/Documents or
# ~/Desktop is synced — including data/lifecrm.db and its live WAL, which
# iCloud can upload mid-write or evict to a stub. Keep it somewhere iCloud
# does not touch, like ~/LifeCRM.
case "$ROOT/" in
    "$HOME/Documents/"* | "$HOME/Desktop/"*)
        echo "WARNING: this repo is under ~/Documents or ~/Desktop. If iCloud's" >&2
        echo "  'Desktop & Documents Folders' sync is on, the live database will be" >&2
        echo "  synced and can be evicted. Move the folder (e.g. to ~/LifeCRM) and" >&2
        echo "  re-run this script." >&2
        ;;
    "$HOME/Library/Mobile Documents/"* | "$HOME/Library/CloudStorage/"*)
        echo "WARNING: this repo is inside iCloud Drive or a cloud-storage folder;" >&2
        echo "  the live database would be synced and can be evicted. Move it (e.g." >&2
        echo "  to ~/LifeCRM) and re-run this script. Only the VoiceJournal folders" >&2
        echo "  belong in the cloud." >&2
        ;;
esac

# --- locate Python 3.12 ---
is_312() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null
}
PY312=""
for candidate in "$(command -v python3.12 || true)" \
                 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    if is_312 "$candidate"; then
        PY312=$candidate
        break
    fi
done
if [ -z "$PY312" ] && command -v uv >/dev/null 2>&1; then
    candidate=$(uv python find 3.12 2>/dev/null || true)
    if is_312 "$candidate"; then
        PY312=$candidate
    fi
fi
if [ -z "$PY312" ]; then
    echo "error: Python 3.12 not found. Install it with:  brew install python@3.12" >&2
    echo "  then re-run this script (no Homebrew? see https://brew.sh)" >&2
    exit 1
fi
echo "Using $PY312"

# --- venv + dependencies ---
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    "$PY312" -m venv "$ROOT/.venv"
fi
PY="$ROOT/.venv/bin/python"
"$PY" -m pip install -q -U pip
"$PY" -m pip install -q -r "$ROOT/requirements.txt"

# --- config + database ---
if [ ! -f "$ROOT/config.toml" ]; then
    "$PY" -m lifecrm configure
fi
"$PY" -m lifecrm init

# --- git (code only; data/ and backups/ are gitignored) ---
if [ ! -d "$ROOT/.git" ] && [ ! -f "$ROOT/.git" ]; then
    git init -q && git add -A && git commit -q -m "Initial commit"
fi

echo ""
echo "Setup complete. Next:"
echo "  .venv/bin/python -m lifecrm where          # what this Mac has to import"
echo "  /bin/bash scripts/install-tasks.sh         # daily intake, backup, doctor"
echo "The guide: docs/mac.md"
