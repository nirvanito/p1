"""Configuration loading. All paths and tunables live in config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """config.toml is missing or wrong. The message is one line, naming the
    file and the fix — the CLI prints it as-is rather than a traceback."""

DEFAULT_CATEGORIES = [
    "food", "exercise", "work", "social", "health", "sleep", "idea", "media",
    "chores", "travel", "learning", "purchase", "event", "reflection", "other",
]
DEFAULT_KINDS = [
    "event", "observation", "thought", "idea", "decision", "task", "plan",
    "fact", "reflection",
]
TIME_OF_DAY = ["morning", "afternoon", "evening", "night"]
CONFIDENCE = ["high", "medium", "low"]
ENTITY_TYPES = [
    "person", "place", "food", "project", "activity", "media",
    "organization", "topic",
]
MENTION_ROLES = ["participant", "location", "item", "subject", "mention"]


@dataclass
class Config:
    root: Path
    data_dir: Path
    backups_dir: Path
    inbox_dir: Path | None
    inbox_text_dir: Path | None
    archive_dir: Path | None
    onedrive_backups_dir: Path | None
    timezone: str = "America/Chicago"
    day_rollover_hour: int = 5
    whisper: dict = field(default_factory=dict)
    ingest: dict = field(default_factory=dict)
    ask: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lifecrm.db"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"


def _resolve(root: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else root / p


def _optional_path(value: str | None) -> Path | None:
    """Unset or empty paths become None — Path('') is '.', which exists and
    would silently point inbox scans at the current directory. `~` expands,
    so a Mac config can say '~/Library/Mobile Documents/...'."""
    return Path(value).expanduser() if value else None


def load_config(root: Path | None = None) -> Config:
    root = root or PROJECT_ROOT
    cfg_file = root / "config.toml"
    if not cfg_file.exists():
        cfg_file = root / "config.example.toml"
    if not cfg_file.exists():
        raise ConfigError(
            f"no config.toml (or config.example.toml) in {root} — copy "
            f"config.example.toml to config.toml, or point LIFECRM_ROOT at a "
            f"project root that has one")
    # utf-8-sig: tolerate the BOM that Notepad and Windows PowerShell's
    # Out-File -Encoding utf8 prepend (tomllib rejects a raw BOM).
    try:
        raw = tomllib.loads(cfg_file.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{cfg_file} is not valid TOML: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read {cfg_file}: {e}") from e

    paths = raw.get("paths", {})
    time_cfg = raw.get("time", {})
    # Checked here, not at first use: a bad zone otherwise surfaces as a
    # traceback from deep inside whichever command happens to need a date.
    tz = time_cfg.get("timezone", "America/Chicago")
    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(
            f"{cfg_file}: [time] timezone '{tz}' is not a known IANA zone "
            f"(e.g. America/Chicago, Europe/London)") from e
    rollover = time_cfg.get("day_rollover_hour", 5)
    if isinstance(rollover, bool) or not isinstance(rollover, int) \
            or not 0 <= rollover <= 23:
        raise ConfigError(f"{cfg_file}: [time] day_rollover_hour must be a whole "
                          f"hour 0-23, got {rollover!r}")
    return Config(
        root=root,
        data_dir=_resolve(root, paths.get("data_dir", "data")),
        backups_dir=_resolve(root, paths.get("backups_dir", "backups")),
        inbox_dir=_optional_path(paths.get("inbox_dir")),
        inbox_text_dir=_optional_path(paths.get("inbox_text_dir")),
        archive_dir=_optional_path(paths.get("archive_dir")),
        onedrive_backups_dir=_optional_path(paths.get("onedrive_backups_dir")),
        timezone=str(tz),
        day_rollover_hour=rollover,
        whisper=raw.get("whisper", {}),
        ingest=raw.get("ingest", {}),
        ask=raw.get("ask", {}),
    )
