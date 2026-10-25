"""The feature switches: [features] in config.toml, checked and unchecked by
`lifecrm features`.

The load-bearing claims: every feature is on until config.toml says
otherwise; a key or value the table cannot hold is a one-line error; `save`
rewrites only the [features] lines (comments, other tables, a BOM and CRLF
line ends survive) and refuses rather than touch anything else; check /
uncheck / --all / numbers / pick all land in the file; the web status
carries the list read-only.
"""

from __future__ import annotations

import tomllib

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import features
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.domain.util import DomainError

runner = CliRunner()


def test_every_feature_is_on_until_the_table_says_otherwise(cfg):
    assert features.settings(cfg) == {k: True for k in features.KEYS}
    assert len(features.KEYS) == 14
    cfg.extra["features"] = {"subscriptions": False, "places": "off", "search": "yes"}
    s = features.settings(cfg)
    assert s["subscriptions"] is False and s["places"] is False and s["search"] is True
    assert features.enabled(cfg, "on_this_day") is True
    with pytest.raises(KeyError):
        features.enabled(cfg, "no_such_feature")


def test_a_bad_key_or_value_is_one_line(cfg):
    cfg.extra["features"] = {"subscriptons": False}
    with pytest.raises(DomainError) as e:
        features.settings(cfg)
    assert "unknown feature(s) subscriptons" in str(e.value) and "\n" not in str(e.value)
    cfg.extra["features"] = {"places": "maybe"}
    with pytest.raises(DomainError, match="places must be true or false"):
        features.settings(cfg)
    cfg.extra["features"] = "on"
    with pytest.raises(DomainError, match="must be a table"):
        features.settings(cfg)
    # the list still renders, with the error, so the table can be fixed
    cfg.extra["features"] = {"subscriptons": False}
    data = features.listing(cfg)
    assert data["error"] and data["on"] == 14


def test_names_numbers_and_dashes_resolve(cfg):
    assert features.resolve(["1", "meeting-prep", "PLACES", "1"]) == \
        ["on_this_day", "meeting_prep", "places"]
    with pytest.raises(DomainError, match="no feature 'nope'"):
        features.resolve(["nope"])
    with pytest.raises(DomainError):
        features.resolve(["15"])


def test_save_rewrites_only_the_features_lines(cfg):
    path = cfg.root / "config.toml"
    original = ("# my config\r\n[paths]\r\ndata_dir = \"data\"  # keep\r\n\r\n"
                "[features]\r\nsubscriptions = true   # the one I like\r\n\r\n"
                "[assistant]\r\ndue_days = 5\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
    features.save(cfg, {"subscriptions": False, "places": False})
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert "subscriptions = false   # the one I like" in text
    assert "# my config" in text and 'data_dir = "data"  # keep' in text
    parsed = tomllib.loads(text)
    assert parsed["features"] == {"subscriptions": False, "places": False}
    assert parsed["assistant"] == {"due_days": 5} and parsed["paths"] == {"data_dir": "data"}
    # the new key went inside the table, before [assistant]
    assert text.index("places = false") < text.index("[assistant]")


def test_a_new_key_goes_after_the_table_s_last_setting(cfg):
    path = cfg.root / "config.toml"
    path.write_text("[features]\n# my switches\nplaces = false\n\n# [assistant]\n"
                    "# due_days = 7\n\n[review]\nquiet_weeks = 3\n", encoding="utf-8")
    features.save(cfg, {"highlights": False})
    text = path.read_text(encoding="utf-8")
    assert "places = false\nhighlights = false\n\n# [assistant]" in text
    path.write_text("[features]\n# nothing yet\n\n[review]\nquiet_weeks = 3\n",
                    encoding="utf-8")
    features.save(cfg, {"search": False})
    assert path.read_text(encoding="utf-8").startswith("[features]\n# nothing yet\nsearch = false\n")


def test_save_appends_a_missing_table_and_refuses_what_it_cannot_do_safely(cfg):
    path = cfg.root / "config.toml"
    path.write_text("[paths]\ndata_dir = \"data\"\n", encoding="utf-8")
    features.save(cfg, {"highlights": False})
    text = path.read_text(encoding="utf-8")
    assert text.endswith("[features]\nhighlights = false\n")
    assert tomllib.loads(text)["paths"] == {"data_dir": "data"}
    # an inline table cannot be edited line by line: refused, file untouched
    path.write_text('features = { places = false }\n[paths]\ndata_dir = "data"\n',
                    encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(DomainError, match="edit it by hand"):
        features.save(cfg, {"places": True})
    assert path.read_bytes() == before
    path.write_text("[paths\nbroken", encoding="utf-8")
    with pytest.raises(DomainError, match="cannot read"):
        features.save(cfg, {"places": True})
    path.unlink()
    with pytest.raises(DomainError, match="run: python -m lifecrm configure"):
        features.save(cfg, {"places": True})
    with pytest.raises(DomainError, match="no feature"):
        features.save(cfg, {"nope": True})


def _states(root) -> dict:
    return features.settings(load_config(root))


def test_the_cli_checks_and_unchecks_keys_numbers_and_everything(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["features"])
    assert r.exit_code == 0, r.output
    assert "14 of 14 checked" in r.output and "[x]  1 on_this_day" in r.output
    r = runner.invoke(app, ["features", "uncheck", "subscriptions", "2"])
    assert r.exit_code == 0, r.output
    assert "unchecked: subscriptions, meeting_prep" in r.output
    s = _states(root)
    assert s["subscriptions"] is False and s["meeting_prep"] is False and s["places"]
    assert "[ ]  2 meeting_prep" in r.output
    r = runner.invoke(app, ["features", "uncheck", "subscriptions"])
    assert "already unchecked: subscriptions" in r.output
    r = runner.invoke(app, ["features", "uncheck", "--all"])
    assert r.exit_code == 0 and not any(_states(root).values())
    r = runner.invoke(app, ["features", "check", "--all"])
    assert r.exit_code == 0 and all(_states(root).values())
    r = runner.invoke(app, ["features", "check"])
    assert r.exit_code == 1 and "check what?" in r.output
    r = runner.invoke(app, ["features", "check", "places", "--all"])
    assert r.exit_code == 1 and "not both" in r.output
    r = runner.invoke(app, ["features", "check", "nope"])
    assert r.exit_code == 1 and "no feature 'nope'" in r.output
    r = runner.invoke(app, ["features", "list", "--json"])
    assert r.exit_code == 0 and '"total": 14' in r.output
    # the rest of config.toml is exactly as it was
    parsed = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    assert parsed["time"]["timezone"] == "America/Chicago"


def test_pick_toggles_by_number_and_saves_on_enter(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["features", "pick"], input="3 5\nn\na\n7\n\n")
    assert r.exit_code == 0, r.output
    s = _states(root)
    # 'n' then 'a' undo each other; only 7 (search) stays toggled off
    assert s["search"] is False and s["places"] is True and s["subscriptions"] is True
    assert "unchecked search" in r.output
    r = runner.invoke(app, ["features", "pick"], input="1\nq\n")
    assert "nothing saved" in r.output and _states(root)["on_this_day"] is True
    r = runner.invoke(app, ["features", "pick"], input="bogus\n\n")
    assert "no feature 'bogus'" in r.output and "no change" in r.output


def test_doctor_names_a_table_the_features_cannot_use(conn, cfg):
    from lifecrm import doctor
    by = {c["check"]: c for c in doctor.run_checks(conn, cfg)["checks"]}
    assert by["features_config"]["status"] == "ok" and by["places_config"]["status"] == "ok"
    cfg.extra["features"] = {"subscriptons": False}
    cfg.extra["places"] = {"trip_km": 0}
    by = {c["check"]: c for c in doctor.run_checks(conn, cfg)["checks"]}
    assert by["features_config"]["status"] == "warn"
    assert "subscriptons" in by["features_config"]["detail"]
    assert by["places_config"]["status"] == "warn"


def test_the_web_status_carries_the_list_read_only(conn, cfg):
    from lifecrm.web import api
    cfg.extra["features"] = {"search": False}
    st = api.status(conn, cfg)
    by = {f["key"]: f for f in st["features"]["features"]}
    assert by["search"]["on"] is False and by["places"]["on"] is True
    assert st["features"]["on"] == 13
