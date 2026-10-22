#!/bin/bash
# Scheduled doctor run against the LIVE database — the weekly verify-restore
# audits a restored copy, which says nothing about drift in the real one (a
# stuck inbox, an old backup, notes still in iCloud). install-tasks.sh runs
# it daily at 08:00, after the 07:00 intake.
#   /bin/bash scripts/doctor.sh
#
# Logs to data/doctor.log. Exits 1 when doctor finds a failure (warnings
# alone exit 0), so launchd's "last exit code" shows it.

. "$(dirname "$0")/_common.sh" || exit 1
run_logged doctor.log doctor doctor
exit $?
