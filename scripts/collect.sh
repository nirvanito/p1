#!/bin/bash
# Daily: run the imports [collect] sources names in config.toml — routine
# changes are applied, anything unusual (a source's first import, a large
# removal, records the reader cannot validate) is held and shows in /morning
# and doctor. With no [collect] sources it logs one line and exits 0.
# install-tasks.sh runs it daily at 06:30, before intake, the reminders
# calendar and doctor, so all three see this morning's imports.
#   /bin/bash scripts/collect.sh
#
# Logs to data/collect.log and exits with the command's code.

. "$(dirname "$0")/_common.sh" || exit 1
run_logged collect.log "collect" collect --if-configured
exit $?
