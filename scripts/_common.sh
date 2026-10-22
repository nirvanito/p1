# Shared by the scripts in this folder: sourced, never run on its own.
# Where the project is, which python runs lifecrm, and the timestamped log
# runner the scheduled jobs use. Written for /bin/bash 3.2 (what macOS ships)
# and Git Bash on Windows, so no bash 4 features — and few forks, which are
# slow under Git Bash.

# Every script runs from the repo root (this file's folder's parent): the
# venv does not install lifecrm as a package, so `python -m lifecrm` finds
# the code because the working directory is first on sys.path — and launchd
# starts jobs in / unless told otherwise.
case ${BASH_SOURCE[0]} in
    */*) cd "${BASH_SOURCE[0]%/*}/.." || exit 1 ;;
    *) cd .. || exit 1 ;;
esac
LIFECRM_REPO=$PWD
export PYTHONUTF8=1

# The interpreter: LIFECRM_PYTHON wins (tests, an unusual install); else the
# project venv — bin/python on a Mac, Scripts/python.exe under Git Bash on
# Windows. With no venv at all a run fails with 127, which the log and the
# exit code then show.
if [ -n "${LIFECRM_PYTHON:-}" ]; then
    LIFECRM_PY=$LIFECRM_PYTHON
elif [ -x "$LIFECRM_REPO/.venv/bin/python" ]; then
    LIFECRM_PY="$LIFECRM_REPO/.venv/bin/python"
elif [ -x "$LIFECRM_REPO/.venv/Scripts/python.exe" ]; then
    LIFECRM_PY="$LIFECRM_REPO/.venv/Scripts/python.exe"
else
    LIFECRM_PY="$LIFECRM_REPO/.venv/bin/python"
fi

# run_logged LOGNAME WHAT LIFECRM-ARGS...
# The scheduled-job wrapper, ported line for line from transcribe-inbox.ps1:
# one timestamp taken at the start prefixes every line; python's stdout and
# stderr both go to data/LOGNAME under the project root (LIFECRM_ROOT when
# set, so a restore target or a test run keeps its own log); a non-zero exit
# is logged as FAILED and returned, so launchd's "last exit code" shows it.
run_logged() {
    local logname=$1 what=$2
    shift 2
    local logdir="${LIFECRM_ROOT:-$LIFECRM_REPO}/data"
    mkdir -p "$logdir"
    local log="$logdir/$logname"
    local stamp code
    stamp=$(date '+%Y-%m-%d %H:%M:%S')
    printf '%s starting %s\n' "$stamp" "$what" >> "$log"
    # Windows python ends lines with CRLF; the log stays LF on both systems.
    "$LIFECRM_PY" -m lifecrm "$@" 2>&1 | while IFS= read -r line || [ -n "$line" ]; do
        printf '%s %s\n' "$stamp" "${line%$'\r'}"
    done >> "$log"
    code=${PIPESTATUS[0]}
    if [ "$code" -ne 0 ]; then
        printf '%s FAILED (exit %s) - see the lines above\n' "$stamp" "$code" >> "$log"
    fi
    return "$code"
}
