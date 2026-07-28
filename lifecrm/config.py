"""Configuration loading. All paths and tunables live in config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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
    p = Path(value)
    return p if p.is_absolute() else root / p


def _optional_path(value: str | None) -> Path | None:
    """Unset or empty paths become None — Path('') is '.', which exists and
    would silently point inbox scans at the current directory."""
    return Path(value) if value else None


def load_config(root: Path | None = None) -> Config:
    root = root or PROJECT_ROOT
    cfg_file = root / "config.toml"
    if not cfg_file.exists():
        cfg_file = root / "config.example.toml"
    # utf-8-sig: tolerate the BOM that Notepad and Windows PowerShell's
    # Out-File -Encoding utf8 prepend (tomllib rejects a raw BOM).
    raw = tomllib.loads(cfg_file.read_text(encoding="utf-8-sig"))

    paths = raw.get("paths", {})
    time_cfg = raw.get("time", {})
    return Config(
        root=root,
        data_dir=_resolve(root, paths.get("data_dir", "data")),
        backups_dir=_resolve(root, paths.get("backups_dir", "backups")),
        inbox_dir=_optional_path(paths.get("inbox_dir")),
        inbox_text_dir=_optional_path(paths.get("inbox_text_dir")),
        archive_dir=_optional_path(paths.get("archive_dir")),
        onedrive_backups_dir=_optional_path(paths.get("onedrive_backups_dir")),
        timezone=time_cfg.get("timezone", "America/Chicago"),
        day_rollover_hour=int(time_cfg.get("day_rollover_hour", 5)),
        whisper=raw.get("whisper", {}),
        ingest=raw.get("ingest", {}),
    )
