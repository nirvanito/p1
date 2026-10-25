#!/bin/bash
# Daily: republish the reminders calendar (due tasks and plans, birthdays) so
# an overdue item is re-dated to this morning and the phone's calendar sees a
# new revision. The same publication runs after every CLI command that
# changes the database; this job covers the mornings nothing was written.
# With no [assistant] reminders_dir in config.toml it logs one line and exits 0.
# install-tasks.sh runs it daily at 07:15, after the 05:00 rollover and after
# intake.
#   /bin/bash scripts/reminders.sh
#
# Logs to data/reminders.log and exits with the command's code.

. "$(dirname "$0")/_common.sh" || exit 1
run_logged reminders.log "reminders calendar" reminders publish --if-configured
exit $?
