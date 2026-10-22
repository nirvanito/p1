#!/bin/bash
# Snapshot the database (SQLite backup API — never a raw copy of a live WAL
# DB) to backups/ and the cloud backups folder, with tiered retention.
# The macOS port of backup.ps1; install-tasks.sh runs it daily at 21:30.
#   /bin/bash scripts/backup.sh

. "$(dirname "$0")/_common.sh" || exit 1
"$LIFECRM_PY" -m lifecrm backup
# Passed on explicitly so launchd's "last exit code" is the backup's own.
exit $?
