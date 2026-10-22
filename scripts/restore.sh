#!/bin/bash
# Restore the database from a snapshot. Without an argument, lists snapshots.
#   /bin/bash scripts/restore.sh [snapshot.db]
# Full instructions, including restoring on a machine that has never run this
# project, are in docs/restore.md.

. "$(dirname "$0")/_common.sh" || exit 1
exec "$LIFECRM_PY" -m lifecrm restore "$@"
