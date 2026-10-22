#!/bin/bash
# Weekly: build last week's review pack (every lens) so it is waiting in
# data/staging/ when you ask Claude for /review-week or /advise. Reads only —
# the pack is a file; nothing reaches the database without the review gate.
# install-tasks.sh runs it Sundays at 19:00.
#   /bin/bash scripts/review.sh
#
# Logs to data/review.log and exits with the command's code.

. "$(dirname "$0")/_common.sh" || exit 1
run_logged review.log "weekly review pack" review --lens all --write-only
exit $?
