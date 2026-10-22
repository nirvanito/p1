"""Cross-feature seams found while integrating: a relationship that cites an
observed record keeps that record from being deleted by source mirroring."""

from lifecrm.connectors import base
from lifecrm.domain import entities, entity_links
from lifecrm.domain import observations as obs
from lifecrm.doctor import run_checks


def _scan(ids):
    s = base.empty_scan("synthetic", account="me@example.com",
                        source_file="cards.vcf", source_hash="h-" + "-".join(ids))
    for i in ids:
        s["records"].append({
            "stream": "contact_card", "external_id": i, "journal_date": "2026-07-27",
            "started_at": "2026-07-27T15:00:00Z", "title": f"Card {i}",
            "body": "Works at Acme", "participants": []})
    s["present_ids"] = set(ids)
    s["mirror_streams"] = ["contact_card"]
    return s


def test_mirroring_retracts_an_observation_a_relationship_cites(conn, cfg):
    first = _scan(["a", "b"])
    obs.apply_plan(conn, first, obs.build_plan(conn, first))
    conn.commit()
    card = conn.execute("SELECT uuid FROM observations WHERE external_id='a'").fetchone()
    jake = entities.create(conn, "person", "Jake Morales")
    acme = entities.create(conn, "organization", "Acme")
    entity_links.add(conn, jake["id"], acme["id"], "works_at",
                     observation_uuid=card["uuid"])
    conn.commit()

    second = _scan(["b"])                         # card 'a' was deleted at the source
    result = obs.apply_plan(conn, second, obs.build_plan(conn, second))
    conn.commit()

    assert result.get("retracted_cited") == 1
    row = conn.execute("SELECT status FROM observations WHERE external_id='a'").fetchone()
    assert row["status"] == "retracted"           # kept: a confirmed fact cites it
    assert run_checks(conn, cfg)["failures"] == 0


def test_value_less_sleep_rows_are_not_a_second_recorder(conn, cfg):
    """An iPhone writes in-bed-only nights with no asleep minutes beside the
    Watch's night: nothing double-counts, so doctor must not say it does."""
    from lifecrm.doctor import _health_multi_origin
    now = "2026-10-01T00:00:00Z"
    conn.execute("INSERT INTO days (journal_date, created_at, updated_at) "
                 "VALUES ('2026-09-30', ?, ?)", (now, now))
    for i, (origin, value) in enumerate((("Watch", 420.0), ("iPhone", None))):
        conn.execute(
            "INSERT INTO health_records (uuid, metric, journal_date, started_at, "
            "ended_at, value, unit, value_json, origin_app, external_id, source_file, "
            "status, created_at, updated_at) VALUES (?, 'sleep_session', '2026-09-30', "
            "'2026-09-30T05:00:00Z', '2026-09-30T12:00:00Z', ?, 'min', ?, ?, ?, "
            "'export.zip', 'active', ?, ?)",
            (f"u{i}", value, '{"in_bed_min": 470}', origin, f"x{i}", now, now))
    seen = []
    _health_multi_origin(conn, lambda status, check, detail=None: seen.append(status))
    assert seen == ["ok"]


def test_every_config_path_is_relative_to_the_project_root(tmp_path, monkeypatch):
    """One rule for config.toml paths, whichever folder a command starts in:
    [paths] and [connectors.*] alike resolve a relative path under the root."""
    from lifecrm.config import load_config
    (tmp_path / "config.toml").write_text(
        "[paths]\ninbox_dir = 'VoiceJournal/inbox'\narchive_dir = 'VoiceJournal/archive'\n"
        "[connectors.photos]\npath = 'Pictures/Camera Roll'\n", encoding="utf-8")
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load_config(tmp_path)
    assert cfg.inbox_dir == tmp_path / "VoiceJournal" / "inbox"
    assert cfg.archive_dir == tmp_path / "VoiceJournal" / "archive"
    from lifecrm.connectors import photos
    assert photos.default_target(cfg) == tmp_path / "Pictures" / "Camera Roll"
