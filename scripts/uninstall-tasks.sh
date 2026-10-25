#!/bin/bash
# Remove the launchd jobs install-tasks.sh registered: unload each one and
# delete its plist. Logs and data are left alone.
#   /bin/bash scripts/uninstall-tasks.sh             unload + delete
#   /bin/bash scripts/uninstall-tasks.sh --no-load   delete the plists only
# LIFECRM_LAUNCHAGENTS_DIR replaces ~/Library/LaunchAgents (tests use it).

. "$(dirname "$0")/_common.sh" || exit 1

unload=1
for arg in "$@"; do
    case "$arg" in
        --no-load) unload="" ;;
        *) echo "usage: uninstall-tasks.sh [--no-load]" >&2; exit 2 ;;
    esac
done
if [ -n "$unload" ] && ! command -v launchctl >/dev/null 2>&1; then
    echo "error: launchctl not found — this script is for macOS (or pass --no-load)" >&2
    exit 1
fi

AGENTS="${LIFECRM_LAUNCHAGENTS_DIR:-$HOME/Library/LaunchAgents}"
uid=""
if [ -n "$unload" ]; then
    uid=$(id -u)
fi
for job in intake backup doctor verify-restore review reminders; do
    label="local.lifecrm.$job"
    file="$AGENTS/$label.plist"
    if [ -n "$unload" ]; then
        launchctl bootout "gui/$uid/$label" 2>/dev/null
    fi
    if [ -f "$file" ]; then
        rm -f "$file" && echo "removed: $label"
    else
        echo "not installed: $label"
    fi
done
