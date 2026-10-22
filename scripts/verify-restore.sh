#!/bin/bash
# An untested backup is not a backup. Restores the newest local snapshot into
# a scratch folder, runs doctor on it and compares it with the live database;
# never touches data/lifecrm.db. install-tasks.sh runs it Sundays at 12:00.
#   /bin/bash scripts/verify-restore.sh [snapshot.db]
#
# The checking lives in `lifecrm verify-backup`, shared with Windows, so both
# systems verify the same things; this only runs it and passes its exit code
# on, so launchd's "last exit code" shows a snapshot that failed.

# _common.sh moves to the repo root; a relative snapshot path means the
# folder this was started from, so it is made absolute against that first.
caller_pwd=$PWD
. "$(dirname "$0")/_common.sh" || exit 1
args=()
for a in "$@"; do
    case $a in
        /*|-*|[A-Za-z]:*) args+=("$a") ;;       # absolute, an option, C:\ (Git Bash)
        *) args+=("$caller_pwd/$a") ;;
    esac
done
"$LIFECRM_PY" -m lifecrm verify-backup "${args[@]}"
exit $?
