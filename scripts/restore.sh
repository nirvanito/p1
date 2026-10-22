#!/bin/bash
# Restore the database from a snapshot. Without an argument, lists snapshots.
#   /bin/bash scripts/restore.sh [snapshot.db]
# Full instructions, including restoring on a machine that has never run this
# project, are in docs/restore.md.

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
exec "$LIFECRM_PY" -m lifecrm restore "${args[@]}"
