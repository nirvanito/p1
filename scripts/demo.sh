#!/bin/bash
# One command to see the whole thing on a Mac: build the demo instance
# (MacBook + iPhone, synthetic data only) and open the web UI.
#   /bin/bash scripts/demo.sh                 # iphone combo
#   /bin/bash scripts/demo.sh --combo android
#   /bin/bash scripts/demo.sh --rebuild
# Nothing here touches data/ — the demo lives in demo/<combo>/ (gitignored).

set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# From the repo root, wherever this was started: the venv does not install
# lifecrm as a package, so `python -m lifecrm` only finds it from here.
cd "$ROOT" || exit 1
export PYTHONUTF8=1

COMBO="iphone"
REBUILD=0
SERVE=1
PORT=8765
while [ $# -gt 0 ]; do
    case "$1" in
        --combo) COMBO="$2"; shift 2 ;;
        --rebuild) REBUILD=1; shift ;;
        --no-serve) SERVE=0; shift ;;
        --port) PORT="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

PY="${LIFECRM_PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
    elif [ -x "$ROOT/.venv/Scripts/python.exe" ]; then PY="$ROOT/.venv/Scripts/python.exe"
    else echo "no venv yet - run: /bin/bash scripts/setup.sh" >&2; exit 1
    fi
fi

DEMO="$ROOT/demo/$COMBO"
if [ "$REBUILD" = 1 ] || [ ! -f "$DEMO/.lifecrm-demo" ]; then
    ARGS=(-m lifecrm demo --combo "$COMBO")
    [ "$REBUILD" = 1 ] && ARGS+=(--force)
    "$PY" "${ARGS[@]}" || exit $?
else
    echo "demo already built at $DEMO (use --rebuild for a fresh one)"
fi

[ "$SERVE" = 1 ] || exit 0
echo ""
echo "opening the web UI at http://127.0.0.1:$PORT/ (Ctrl+C stops it)"
# --open: serve opens the browser once it is listening; a fixed sleep first
# loses the race whenever the snapshot takes longer than the sleep.
exec "$PY" -m lifecrm --root "$DEMO" serve --port "$PORT" --open
