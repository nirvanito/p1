#!/bin/bash
# Register the launchd jobs (the Mac's Task Scheduler) so nothing depends on
# remembering — the macOS counterpart of install-tasks.ps1:
#   local.lifecrm.intake          daily 07:00 + at login (text + audio inboxes)
#   local.lifecrm.backup          daily 21:30 (tiered retention)
#   local.lifecrm.doctor          daily 08:00 (invariants on the live database)
#   local.lifecrm.verify-restore  Sunday 12:00 (an untested backup is not a backup)
#   local.lifecrm.review          Sunday 19:00 (last week's review pack, waiting for /review-week)
# Run once, as yourself (no sudo); re-running replaces the jobs:
#   /bin/bash scripts/install-tasks.sh             write + load the jobs
#   /bin/bash scripts/install-tasks.sh --print     show the plists, change nothing
#   /bin/bash scripts/install-tasks.sh --no-load   write the plists, load nothing
# LIFECRM_LAUNCHAGENTS_DIR replaces ~/Library/LaunchAgents (tests use it).
#
# launchd semantics worth knowing: a job missed while the Mac slept runs when
# it wakes; one missed while it was shut down does not (RunAtLoad covers
# intake at the next login). There is no run-time limit like Task
# Scheduler's two hours. Undo with scripts/uninstall-tasks.sh.

. "$(dirname "$0")/_common.sh" || exit 1

mode=install
for arg in "$@"; do
    case "$arg" in
        --print) mode=print ;;
        --no-load) mode=write ;;
        *) echo "usage: install-tasks.sh [--print | --no-load]" >&2; exit 2 ;;
    esac
done

AGENTS="${LIFECRM_LAUNCHAGENTS_DIR:-$HOME/Library/LaunchAgents}"
LOGDIR="$LIFECRM_REPO/data"
JOBS="intake backup doctor verify-restore review"

# The plists are built from bash builtins only — no $(...) or cat per job.
# Every fork is slow under Git Bash, where the tests run this on Windows.
# The replacements are quoted variables: bash 5.2+ (Homebrew's, Git Bash)
# reads an unquoted '&' in a replacement as "the matched text", which turns
# '<' into '<lt;'. Quoted, every bash back to the Mac's 3.2 inserts it as is.
XML_AMP='&amp;' XML_LT='&lt;' XML_GT='&gt;'
xml() {    # XML-escape $1 into REPLY
    REPLY=${1//&/"$XML_AMP"}
    REPLY=${REPLY//</"$XML_LT"}
    REPLY=${REPLY//>/"$XML_GT"}
}
xml "$LIFECRM_REPO"; ROOT_XML=$REPLY
xml "$LOGDIR"; LOGDIR_XML=$REPLY

# One job's plist. StartCalendarInterval is in the Mac's local time;
# Weekday 0 is Sunday.
plist_for() {
    local job=$1 script hour minute weekday=""
    case $job in
        intake) script=transcribe-inbox.sh; hour=7; minute=0 ;;
        backup) script=backup.sh; hour=21; minute=30 ;;
        doctor) script=doctor.sh; hour=8; minute=0 ;;
        verify-restore) script=verify-restore.sh; hour=12; minute=0; weekday=0 ;;
        review) script=review.sh; hour=19; minute=0; weekday=0 ;;
    esac
    printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' \
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
        '<plist version="1.0">' '<dict>'
    printf '\t<key>Label</key>\n\t<string>local.lifecrm.%s</string>\n' "$job"
    printf '\t<key>ProgramArguments</key>\n\t<array>\n'
    printf '\t\t<string>/bin/bash</string>\n\t\t<string>%s/scripts/%s</string>\n' \
        "$ROOT_XML" "$script"
    printf '\t</array>\n'
    printf '\t<key>WorkingDirectory</key>\n\t<string>%s</string>\n' "$ROOT_XML"
    printf '\t<key>EnvironmentVariables</key>\n\t<dict>\n'
    printf '\t\t<key>PYTHONUTF8</key>\n\t\t<string>1</string>\n\t</dict>\n'
    printf '\t<key>StandardOutPath</key>\n\t<string>%s/launchd-%s.log</string>\n' \
        "$LOGDIR_XML" "$job"
    printf '\t<key>StandardErrorPath</key>\n\t<string>%s/launchd-%s.log</string>\n' \
        "$LOGDIR_XML" "$job"
    printf '\t<key>StartCalendarInterval</key>\n\t<dict>\n'
    if [ -n "$weekday" ]; then
        printf '\t\t<key>Weekday</key>\n\t\t<integer>%s</integer>\n' "$weekday"
    fi
    printf '\t\t<key>Hour</key>\n\t\t<integer>%s</integer>\n' "$hour"
    printf '\t\t<key>Minute</key>\n\t\t<integer>%s</integer>\n\t</dict>\n' "$minute"
    if [ "$job" = intake ]; then       # the "at login" run
        printf '\t<key>RunAtLoad</key>\n\t<true/>\n'
    fi
    printf '%s\n' '</dict>' '</plist>'
}

if [ "$mode" = print ]; then
    for job in $JOBS; do
        echo "# $AGENTS/local.lifecrm.$job.plist"
        plist_for "$job"
    done
    exit 0
fi

if [ "$mode" = install ] && ! command -v launchctl >/dev/null 2>&1; then
    echo "error: launchctl not found — this script is for macOS; on Windows run" \
         "scripts/install-tasks.ps1 (or pass --no-load to only write the plists)" >&2
    exit 1
fi
if [ -z "${LIFECRM_PYTHON:-}" ] && [ ! -x "$LIFECRM_REPO/.venv/bin/python" ]; then
    echo "warning: no .venv/bin/python yet — run scripts/setup.sh first, or every" \
         "job fails (exit 127) until you do" >&2
fi

mkdir -p "$AGENTS" "$LOGDIR" || exit 1
uid=""
if [ "$mode" = install ]; then
    uid=$(id -u)
fi
status=0
for job in $JOBS; do
    label="local.lifecrm.$job"
    file="$AGENTS/$label.plist"
    plist_for "$job" > "$file" || { status=1; continue; }
    if command -v plutil >/dev/null 2>&1 && ! plutil -lint -s "$file"; then
        echo "error: $file is not a valid plist" >&2
        status=1
        continue
    fi
    if [ "$mode" = write ]; then
        echo "wrote: $file (not loaded)"
        continue
    fi
    # bootout of a job that is not loaded fails; that is just the first install.
    launchctl bootout "gui/$uid/$label" 2>/dev/null
    # bootout returns before launchd has finished tearing the old job down,
    # and a bootstrap straight after can fail with "Input/output error".
    loaded=""
    for attempt in 1 2 3; do
        if launchctl bootstrap "gui/$uid" "$file" 2>/dev/null; then
            loaded=1
            break
        fi
        sleep 1
    done
    if [ -z "$loaded" ] && ! launchctl bootstrap "gui/$uid" "$file"; then
        echo "error: launchctl could not load $file" >&2
        status=1
        continue
    fi
    echo "registered: $label"
done

if [ "$mode" = install ]; then
    echo "done - inspect with:  launchctl print gui/$uid/local.lifecrm.intake"
    echo "  ('last exit code' is the last run's result; 1 means look at the log)"
    echo "  run a job now:  launchctl kickstart gui/$uid/local.lifecrm.backup"
fi
echo "logs: data/transcribe.log, data/doctor.log, data/launchd-<job>.log"
echo "a log saying 'Operation not permitted' means macOS privacy settings are" \
     "blocking the job - see Full Disk Access in docs/mac.md"
exit $status
