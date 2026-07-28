import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lifecrm import db as db_mod  # noqa: E402
from lifecrm.config import Config  # noqa: E402
from lifecrm.domain.util import now_utc  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path) -> Config:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    inbox = tmp_path / "onedrive" / "inbox"
    inbox_text = tmp_path / "onedrive" / "inbox-text"
    archive = tmp_path / "onedrive" / "archive"
    obk = tmp_path / "onedrive" / "backups"
    for d in (data, data / "staging", data / "transcripts", backups, inbox,
              inbox_text, archive, obk):
        d.mkdir(parents=True, exist_ok=True)
    return Config(root=tmp_path, data_dir=data, backups_dir=backups,
                  inbox_dir=inbox, inbox_text_dir=inbox_text, archive_dir=archive,
                  onedrive_backups_dir=obk,
                  timezone="America/Chicago", day_rollover_hour=5,
                  whisper={"model": "small"}, ingest={})


@pytest.fixture
def conn(cfg):
    db_mod.init_db(cfg.db_path, now_utc())
    c = db_mod.connect(cfg.db_path)
    yield c
    c.close()


def sample_text() -> str:
    return (FIXTURES / "sample_day.txt").read_text(encoding="utf-8").strip()


def golden_draft() -> dict:
    draft = json.loads((FIXTURES / "sample_day.json").read_text(encoding="utf-8"))
    draft["source"]["text"] = sample_text()
    return draft


def cli_project(tmp_path, monkeypatch) -> Path:
    """Write a config.toml under tmp_path and point LIFECRM_ROOT at it."""
    onedrive = tmp_path / "onedrive"
    for d in ("inbox", "inbox-text", "archive", "backups"):
        (onedrive / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.toml").write_text(
        "[paths]\n"
        'data_dir = "data"\n'
        'backups_dir = "backups"\n'
        f"inbox_dir = '{onedrive / 'inbox'}'\n"
        f"inbox_text_dir = '{onedrive / 'inbox-text'}'\n"
        f"archive_dir = '{onedrive / 'archive'}'\n"
        f"onedrive_backups_dir = '{onedrive / 'backups'}'\n"
        "[time]\n"
        'timezone = "America/Chicago"\n'
        "day_rollover_hour = 5\n"
        "[whisper]\n"
        'model = "small"\n',
        encoding="utf-8")
    monkeypatch.setenv("LIFECRM_ROOT", str(tmp_path))
    return tmp_path
