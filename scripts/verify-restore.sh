#!/bin/bash
# An untested backup is not a backup. Restores the newest local snapshot into
# a scratch folder, runs doctor on it and compares it with the live database;
# never touches data/lifecrm.db. install-tasks.sh runs it Sundays at 12:00.
#   /bin/bash scripts/verify-restore.sh
#
# The checking lives in `lifecrm verify-backup`, shared with Windows, so both
# systems verify the same things; this only runs it and passes its exit code
# on, so launchd's "last exit code" shows a snapshot that failed.

. "$(dirname "$0")/_common.sh" || exit 1
"$LIFECRM_PY" -m lifecrm verify-backup "$@"
exit $?
