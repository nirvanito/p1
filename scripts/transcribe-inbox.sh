#!/bin/bash
# launchd entry point: intake both inboxes — dictated text notes and audio
# (audio is transcribed with local whisper). The macOS port of
# transcribe-inbox.ps1; install-tasks.sh runs it daily at 07:00 and at login.
#   /bin/bash scripts/transcribe-inbox.sh
#
# Logs to data/transcribe.log and exits with intake's own exit code, so
# `launchctl print gui/$(id -u)/local.lifecrm.intake` shows "last exit code
# = 1" when any file failed or could not be archived.

. "$(dirname "$0")/_common.sh" || exit 1
run_logged transcribe.log "inbox intake" intake
exit $?
