"""Obsidian as a main GUI: the start pages (Home, People, Health, Sources)
read like the web UI's pages and are dated by the database's last change,
not the clock; the vault refreshes itself after a command that changed the
database and never after one that did not; `obsidian refresh | open` and
`lifecrm open` pick the vault and the front end; the demo opens in either."""

import json
from datetime import date

import pytest
from typer.testing import CliRunner

from lifecrm import obsidian, recall
from lifecrm.cli import app
from lifecrm.domain import entities
from lifecrm.export import build_vault, export_obsidian
from test_more_fronts_review import _apply, _rec
from test_more_fronts_web import seed_more

runner = CliRunner()
AS_OF_TS = "2026-08-02T18:00:00Z"            # 13:00 in Chicago, journal day 08-02


@pytest.fixture(autouse=True)
def _no_real_launch(monkeypatch):
    """No test may hand a link to the operating system: one that wants to
    see what would open replaces obsidian.launch itself."""
    def refuse(url):
        raise AssertionError(f"a test tried to open {url} for real")
    monkeypatch.setattr(obsidian, "launch", refuse)


def _pin(conn, ts=AS_OF_TS):
    """The test seeds data dated July; its audit rows are stamped with the
    wall clock. Pin the last change so the start pages' windows hold the
    seeded weeks."""
    conn.execute("UPDATE audit_log SET ts=?", (ts,))
    conn.commit()


def _vault(conn, cfg):
    ids = seed_more(conn)
    _pin(conn)
    return ids, build_vault(conn, tz_name=cfg.timezone, cfg=cfg)


# ---------------------------------------------------------------- the pages

def test_the_start_pages_come_with_the_config_and_only_then(conn, cfg):
    _, notes = _vault(conn, cfg)
    for name in ("Home.md", "People.md", "Health.md", "Sources.md"):
        assert name in notes
        assert notes[name].startswith("---\nlifecrm: generated\n")
        assert 'as_of: "2026-08-02"' in notes[name]
    assert "Start at [[Home]] · [[People]] · [[Health]] · [[Sources]]" in notes["Index.md"]
    plain = build_vault(conn, tz_name=cfg.timezone)
    assert not {"Home.md", "People.md", "Health.md", "Sources.md"} & set(plain)
    assert "[[Home]]" not in plain["Index.md"]


def test_home_is_dated_by_the_last_change_never_the_clock(conn, cfg, monkeypatch):
    _, notes = _vault(conn, cfg)
    home = notes["Home.md"]
    assert "As of **2026-08-02**, the database's last change (2026-08-02 13:00)" in home
    monkeypatch.setattr(recall, "local_today", lambda c: date(2031, 1, 1))
    assert build_vault(conn, tz_name=cfg.timezone, cfg=cfg) == notes


def test_home_shows_the_latest_day_loops_people_and_the_week(conn, cfg):
    _, notes = _vault(conn, cfg)
    home = notes["Home.md"]
    assert "### [[2026-08-02]] · Sunday" in home
    assert "[[People]] · [[Health]] · [[Loops]] · [[Sources]] · [[Index]]" in home
    assert "## Open loops" in home and "No open tasks or plans." in home
    assert "- [[Jake Morales]] · last contact [[2026-07-28]] (observed)" in home
    assert "Not in touch for 30+ days: [[Rosa Treviño]] (2026-06-20)" in home
    assert "## Last 7 days" in home and "2026-07-27 … 2026-08-02" in home
    assert "**Logged workouts** · 2 session(s), 105 min in the week of 2026-07-27" in home
    assert "not medical advice" in home and "Vitamin D" in home
    assert "## Sources" in home and "wait to be linked" in home


def test_people_is_most_recent_contact_first_without_the_owner(conn, cfg):
    _, notes = _vault(conn, cfg)
    people = notes["People.md"]
    lines = [ln for ln in people.splitlines() if ln.startswith("- [[")]
    # Priya's LinkedIn reply on 07-31 is the newest; Jake's Venmo payment on
    # 07-30 is money, not contact, so his is the 07-28 email
    assert lines[0].startswith("- [[Priya Natarajan]] · last contact [[2026-07-31]]")
    assert lines[1].startswith("- [[Jake Morales]] · last contact [[2026-07-28]]")
    assert "[[Alex Rivera]]" not in people                # you are not a contact
    # Rosa wrote in June; being copied on Jake's July mail is not contact
    assert "- [[Rosa Treviño]] · last contact [[2026-06-20]] (observed)" in people
    assert "## Not in touch for 30+ days" in people


def test_health_has_the_series_workouts_and_the_nutrients_disclaimer_first(conn, cfg):
    _, notes = _vault(conn, cfg)
    health = notes["Health.md"]
    assert "the last 30 days (2026-07-04 … 2026-08-02)" in health
    assert "## Logged workouts — observed" in health
    assert "- week of 2026-07-27 · 2 session(s), 105 min" in health
    section = health.split("## Nutrients — observed", 1)[1].strip().splitlines()
    assert "not medical advice" in section[0]
    assert any(ln.startswith("- **Low** · Vitamin D") for ln in section)
    assert any("Sodium" in ln and "above the" in ln for ln in section)


def test_sources_lists_connectors_handles_and_runs_as_inert_text(conn, cfg):
    seed_more(conn)
    _apply(conn, "whatsapp", None, [
        _rec(90, "whatsapp", "2026-08-01", direction="in", body="hi",
             who=((("phone", "+15550001111"), "sender"),)),
    ])
    conn.execute("UPDATE entity_handles SET display_name=? WHERE value=?",
                 ("![x](http://e.x) [[Trap]] #tag", "+15550001111"))
    _pin(conn)
    sources = build_vault(conn, tz_name=cfg.timezone, cfg=cfg)["Sources.md"]
    assert "- **email** · " in sources and "(email " in sources
    assert "Never imported: " in sources
    assert "`phone:+15550001111`" in sources
    assert r"— !\[x\](http://e.x) \[\[Trap\]\] \#tag · 1 record(s)" in sources
    assert "[[Trap]]" not in sources.replace(r"\[\[Trap\]\]", "")
    assert "## Recent import runs" in sources


def test_an_entity_named_home_gets_another_name(conn, cfg):
    seed_more(conn)
    entities.create(conn, "topic", "Home")
    entities.create(conn, "topic", "health")
    conn.commit()
    _pin(conn)
    notes = build_vault(conn, tz_name=cfg.timezone, cfg=cfg)
    assert "Topics/Home (topic).md" in notes and "Topics/health (topic).md" in notes
    assert notes["Home.md"].count("# Home") == 1
    # and the name does not depend on whether the start pages were written
    assert "Topics/Home (topic).md" in build_vault(conn, tz_name=cfg.timezone)


def test_an_empty_database_has_a_home_that_says_so(conn, cfg):
    notes = build_vault(conn, tz_name=cfg.timezone, cfg=cfg)
    assert "Nothing recorded yet." in notes["Home.md"]
    assert "People.md" not in notes


def test_a_refresh_with_nothing_new_writes_nothing(conn, cfg, tmp_path):
    _vault(conn, cfg)
    out = tmp_path / "vault"
    first = export_obsidian(conn, out, tz_name=cfg.timezone, cfg=cfg)
    assert "Home.md" in first["created"]
    again = export_obsidian(conn, out, tz_name=cfg.timezone, cfg=cfg)
    assert obsidian.changed(again) == 0 and again["unchanged"] == first["files"]


# ---------------------------------------------------------------- settings

def test_settings_vault_gui_and_auto_refresh(cfg, monkeypatch):
    assert obsidian.vault_dir(cfg) is None and obsidian.main_gui(cfg) == "web"
    assert obsidian.auto_refresh(cfg) is False
    cfg.extra = {"obsidian": {"vault": "notes/vault"}, "gui": {"main": "obsidian"}}
    assert obsidian.vault_dir(cfg) == cfg.root / "notes" / "vault"
    assert obsidian.main_gui(cfg) == "obsidian" and obsidian.main_gui(cfg, "web") == "web"
    assert obsidian.auto_refresh(cfg) is True
    cfg.extra["obsidian"]["auto_refresh"] = False
    assert obsidian.auto_refresh(cfg) is False
    cfg.extra["obsidian"]["auto_refresh"] = True
    monkeypatch.setenv(obsidian.NO_REFRESH_ENV, "1")
    assert obsidian.auto_refresh(cfg) is False
    cfg.extra["gui"]["main"] = "tui"
    with pytest.raises(obsidian.DomainError, match=r'\[gui\] main must be "web" or "obsidian"'):
        obsidian.main_gui(cfg)


def test_the_change_stamp_moves_with_a_domain_write_only(conn, cfg):
    before = obsidian.change_stamp(cfg)
    conn.execute("INSERT INTO recall_log (asked_at, question) VALUES (?, ?)",
                 ("2026-08-02T18:00:00Z", "how did I sleep"))
    conn.commit()
    assert obsidian.change_stamp(cfg) == before          # a logged question
    entities.create(conn, "person", "Sam Okafor")
    conn.commit()
    assert obsidian.change_stamp(cfg) != before


# ---------------------------------------------------------------- the CLI

def _project(tmp_path, monkeypatch, extra=""):
    from conftest import cli_project
    root = cli_project(tmp_path, monkeypatch)
    cfg_file = root / "config.toml"
    cfg_file.write_text(cfg_file.read_text(encoding="utf-8")
                        + '[obsidian]\nvault = "vault"\n' + extra, encoding="utf-8")
    assert runner.invoke(app, ["init"]).exit_code == 0
    return root


def test_a_write_refreshes_the_vault_and_a_read_does_not(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    home = root / "vault" / "Home.md"
    assert home.is_file()                                   # init wrote the db
    done = runner.invoke(app, ["categories", "add", "climbing"])
    assert done.exit_code == 0, done.output
    assert "obsidian vault refreshed" not in done.stdout     # only on stderr
    stamp = home.stat().st_mtime_ns
    for args in (["status"], ["categories", "list"], ["doctor"],
                 ["ask", "how did I sleep last week", "--no-llm"]):
        r = runner.invoke(app, args)
        assert "obsidian vault refreshed" not in r.output, args
    assert home.stat().st_mtime_ns == stamp


def test_a_refresh_follows_any_write_with_its_own_line(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    from lifecrm.config import load_config
    from lifecrm import db as db_mod
    cfg = load_config(root)
    c = db_mod.connect(cfg.db_path)
    ids = seed_more(c)                 # written past the CLI: no refresh yet
    c.close()
    assert not (root / "vault" / "People.md").exists()
    done = runner.invoke(app, ["entities", "alias-add", str(ids["jake"]), "Jakey"])
    assert done.exit_code == 0, done.output
    people = (root / "vault" / "People.md").read_text(encoding="utf-8")
    assert "[[Jake Morales]]" in people
    assert "obsidian vault refreshed:" in done.output
    jake = (root / "vault" / "People" / "Jake Morales.md").read_text(encoding="utf-8")
    assert "Jakey" in jake


def test_a_failed_refresh_warns_and_keeps_the_commands_result(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    import shutil
    shutil.rmtree(root / "vault")
    (root / "vault").write_text("not a folder", encoding="utf-8")
    done = runner.invoke(app, ["categories", "add", "climbing"])
    assert done.exit_code == 0
    assert "warning: Obsidian vault not refreshed" in done.output
    assert "lifecrm obsidian refresh" in done.output


def test_auto_refresh_off_leaves_the_vault_alone(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, "auto_refresh = false\n")
    assert not (root / "vault").exists()
    runner.invoke(app, ["categories", "add", "climbing"])
    assert not (root / "vault").exists()
    done = runner.invoke(app, ["obsidian", "refresh", "--dry-run"])
    assert done.exit_code == 0 and "dry run — nothing written" in done.stdout
    assert not (root / "vault").exists()
    done = runner.invoke(app, ["obsidian", "refresh"])
    assert done.exit_code == 0 and (root / "vault" / "Home.md").is_file()


def test_refresh_without_a_vault_says_how_to_set_one(tmp_path, monkeypatch):
    from conftest import cli_project
    cli_project(tmp_path, monkeypatch)
    runner.invoke(app, ["init"])
    done = runner.invoke(app, ["obsidian", "refresh"])
    assert done.exit_code == 1
    assert "no vault folder: pass --vault, or set it once in config.toml" in done.output
    other = tmp_path / "elsewhere"
    done = runner.invoke(app, ["obsidian", "refresh", "--vault", str(other)])
    assert done.exit_code == 0 and (other / "Home.md").is_file()


def _settings(tmp_path, *paths):
    f = tmp_path / "obsidian.json"
    f.write_text(json.dumps({"vaults": {f"id{i}": {"path": str(p), "ts": 1}
                                        for i, p in enumerate(paths)}}),
                 encoding="utf-8")
    return f


def test_open_goes_to_home_in_a_vault_obsidian_knows(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(obsidian, "launch", lambda url: opened.append(url))
    monkeypatch.setattr(obsidian, "_obsidian_settings_file",
                        lambda: _settings(tmp_path, root / "vault"))
    done = runner.invoke(app, ["obsidian", "open"])
    assert done.exit_code == 0, done.output
    assert opened and opened[0].startswith("obsidian://open?path=")
    assert opened[0].endswith("Home.md")
    assert "opened" in done.stdout


def test_open_the_first_time_shows_the_vault_manager_and_the_step(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(obsidian, "launch", lambda url: opened.append(url))
    monkeypatch.setattr(obsidian, "_obsidian_settings_file",
                        lambda: _settings(tmp_path, tmp_path / "some other vault"))
    done = runner.invoke(app, ["obsidian", "open", "--no-refresh"])
    assert done.exit_code == 0, done.output
    assert opened == [obsidian.CHOOSE_VAULT_URL]
    assert 'choose "Open folder as vault"' in done.stdout


def test_open_with_no_obsidian_settings_tries_and_says_what_to_do(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(obsidian, "launch", lambda url: opened.append(url))
    monkeypatch.setattr(obsidian, "_obsidian_settings_file", lambda: None)
    done = runner.invoke(app, ["obsidian", "open"])
    assert done.exit_code == 0 and opened[0].startswith("obsidian://open?path=")
    assert "if Obsidian says the vault is not found" in done.stdout


def test_open_reports_when_nothing_opens_obsidian_links(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)

    def refuse(url):
        raise obsidian.DomainError("nothing on this computer opens obsidian:// links")
    monkeypatch.setattr(obsidian, "launch", refuse)
    monkeypatch.setattr(obsidian, "_obsidian_settings_file", lambda: None)
    done = runner.invoke(app, ["obsidian", "open"])
    assert done.exit_code == 1
    assert "nothing on this computer opens obsidian:// links" in done.output


def test_lifecrm_open_follows_gui_main(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, '[gui]\nmain = "obsidian"\n')
    opened, served = [], []
    monkeypatch.setattr(obsidian, "launch", lambda url: opened.append(url))
    monkeypatch.setattr(obsidian, "_obsidian_settings_file", lambda: None)
    from lifecrm.commands import web
    monkeypatch.setattr(web, "serve", lambda **kw: served.append(kw))
    assert runner.invoke(app, ["open"]).exit_code == 0
    assert len(opened) == 1 and not served
    assert runner.invoke(app, ["open", "--gui", "web", "--port", "8899"]).exit_code == 0
    assert served == [{"host": None, "port": 8899, "token": None, "no_log": False,
                       "no_llm": False, "open_browser": True, "verbose": False}]
    done = runner.invoke(app, ["open", "--gui", "web", "--vault", str(root)])
    assert done.exit_code == 1 and "--vault is for --gui obsidian" in done.output
    done = runner.invoke(app, ["open", "--gui", "tui"])
    assert done.exit_code == 1 and '[gui] main must be "web" or "obsidian"' in done.output


def test_known_vault_compares_paths_not_spellings(tmp_path):
    vault = tmp_path / "My Vault"
    vault.mkdir()
    f = _settings(tmp_path, str(vault) + "/")
    assert obsidian.known_vault(vault, f) is True
    assert obsidian.known_vault(tmp_path / "Other", f) is False
    # a subfolder of a vault Obsidian knows opens in that vault
    assert obsidian.known_vault(vault / "LifeCRM", f) is True
    assert obsidian.known_vault(tmp_path / "My Vault 2", f) is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert obsidian.known_vault(vault, bad) is None
    assert obsidian.known_vault(vault, tmp_path / "missing.json") is None


def test_open_url_names_the_home_note(tmp_path):
    url = obsidian.open_url(tmp_path / "LifeCRM vault")
    assert url.startswith("obsidian://open?path=")
    assert url.endswith("Home.md") and " " not in url and "%20vault" in url


# ---------------------------------------------------------------- the demo

def test_the_demo_config_names_its_vault():
    from lifecrm.config import Config
    from lifecrm.demo.build import _config_text
    import tomllib
    from pathlib import Path
    raw = tomllib.loads(_config_text(Path("/tmp/demo"), "android", date(2026, 10, 22)))
    assert raw["obsidian"] == {"vault": "vault"}


def test_the_demo_build_switches_auto_refresh_off_for_its_own_commands(tmp_path, monkeypatch):
    from lifecrm.demo import build
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw["env"])

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()
    monkeypatch.setattr(build.subprocess, "run", fake_run)
    b = build.DemoBuilder(tmp_path / "demo", "android")
    b._subprocess(["version"])
    assert seen[obsidian.NO_REFRESH_ENV] == "1"
