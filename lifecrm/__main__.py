"""`python -m lifecrm` — the entry point the scheduled tasks run.

Errors an operator can act on (a locked or unopenable database, a broken
config.toml) end as one `error:` line and exit 1, whichever command they
surface in. Anything else stays a full traceback: that is a bug, and a bug
should be loud.
"""

import os
import sqlite3
import sys
from pathlib import Path

from .cli import app, db_error_line
from .config import ConfigError, load_config


def _db_path() -> Path:
    root = os.environ.get("LIFECRM_ROOT")
    try:
        return load_config(Path(root) if root else None).db_path
    except ConfigError:
        return Path("lifecrm.db")


def main() -> None:
    try:
        app()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.OperationalError as e:
        if not any(k in str(e) for k in ("locked", "busy", "unable to open")):
            raise
        print(f"error: {db_error_line(_db_path(), e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
