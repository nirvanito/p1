"""Spotify connector: Account data and the extended streaming history into
music_play — one record per track per journal day.

What is easy to get wrong, and is checked here: plays dated by when they
STARTED (the export gives the end) in the home timezone with the rollover;
the 30-second floor; the two downloads overlapping on the last year without
double-counting; overlapping downloads in one folder; and the privacy rule —
the IP address, country, device and user agent of a play never reach the
archive, nor anything in Userdata.json but the username.
"""

import json
import zipfile
from datetime import date, timedelta

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.connectors import spotify
from lifecrm.demo import persona
from lifecrm.demo.sources import spotify as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()
ANCHOR = date(2026, 10, 21)
CONFIRMED = ("entries", "measurements", "mentions", "entity_aliases", "entity_links")


def acct(end, artist, track, ms):
    return {"endTime": end, "artistName": artist, "trackName": track, "msPlayed": ms}


def ext(ts, ms, track=None, artist=None, album=None, uri=None, episode=None, show=None,
        skipped=False, **extra):
    row = {"ts": ts, "platform": "android", "ms_played": ms, "conn_country": "US",
           "ip_addr": "198.51.100.7", "master_metadata_track_name": track,
           "master_metadata_album_artist_name": artist,
           "master_metadata_album_album_name": album,
           "spotify_track_uri": uri if track else None, "episode_name": episode,
           "episode_show_name": show, "spotify_episode_uri": uri if episode else None,
           "reason_start": "trackdone", "reason_end": "trackdone", "shuffle": False,
           "skipped": skipped, "offline": False, "offline_timestamp": None,
           "incognito_mode": False}
    row.update(extra)
    return row


def write_json(path, rows, bom=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + json.dumps(rows).encode("utf-8"))
    return path


def zip_of(path, members: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, obj in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 21, 9, 0, 0))
            zf.writestr(info, json.dumps(obj))
    return path


MUSIC = "Spotify Account Data/StreamingHistory_music_0.json"
AUDIO = "Spotify Extended Streaming History/Streaming_History_Audio_2025-2026_0.json"


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _rec(scan, title, day):
    return next(r for r in scan["records"]
                if r["title"] == title and r["journal_date"] == day)


def _detail(rec):
    return json.loads(rec["value_json"])


def _day(offset):
    return persona.day(ANCHOR, offset).isoformat()


# ---------------------------------------------------------------- the demo download

def test_the_demo_download_tells_the_persona_story(tmp_path, cfg):
    scan = spotify.scan(sample.write_sample(tmp_path, ANCHOR, "android"), cfg)
    assert scan["account"] == sample.USERNAME
    # Japanese Breakfast the day the gift list named their concert: Be Sweet twice.
    sweet = _rec(scan, "Be Sweet", _day(-25))
    assert sweet["body"] == "Japanese Breakfast" and _detail(sweet)["plays"] == 2
    assert sweet["value"] == round(2 * 203.417 / 60, 1) and sweet["unit"] == "min"
    # The Slovenia podcast on the day the return trip took shape, in two sittings.
    pod = _rec(scan, sample.SLOVENIA_EPISODE, _day(-16))
    assert pod["body"] == sample.SHOW
    assert _detail(pod)["kind"] == "podcast" and _detail(pod)["plays"] == 2
    assert pod["value"] == 58.0
    # The pre-board playlist ends before the 14:00 board meeting (19:00Z, CDT).
    board_day = [r for r in scan["records"] if r["journal_date"] == _day(-20)]
    assert {r["title"] for r in board_day} >= {"Harder, Better, Faster, Stronger",
                                               "All My Friends"}
    assert all(r["ended_at"] < f"{_day(-20)}T19:00:00Z" for r in board_day)
    # The Bled trip comes from the extended history: URIs and albums, on the trip days.
    trip = {d.isoformat() for d in persona.bled_trip_days(ANCHOR)}
    on_trip = [r for r in scan["records"] if r["journal_date"] in trip]
    assert {r["title"] for r in on_trip} == {"Holocene", "Mykonos", "Be Sweet"}
    assert all(_detail(r)["uri"].startswith("spotify:track:") for r in on_trip)
    assert _detail(on_trip[0])["album"]
    assert "2 × " + spotify.SHORT in scan["skipped"]
    assert "4 × " + spotify.COVERED in scan["skipped"]
    assert len(scan["records"]) == 31


def test_the_same_listening_comes_through_on_both_phones(tmp_path, cfg):
    a = spotify.scan(sample.write_sample(tmp_path / "a", ANCHOR, "android"), cfg)
    b = spotify.scan(sample.write_sample(tmp_path / "b", ANCHOR, "iphone"), cfg)
    assert a["records"] == b["records"]            # the platform is never read


def test_the_demo_sample_is_deterministic(tmp_path, cfg):
    a = spotify.scan(sample.write_sample(tmp_path / "a", ANCHOR, "iphone"), cfg)
    b = spotify.scan(sample.write_sample(tmp_path / "b", ANCHOR, "iphone"), cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]


def test_an_unknown_combo_has_no_sample(tmp_path):
    with pytest.raises(ValueError, match="unknown combo"):
        sample.write_sample(tmp_path, ANCHOR, "blackberry")


@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_the_demo_applies_once_then_is_identical_and_doctor_stays_green(
        tmp_path, cfg, conn, combo):
    folder = sample.write_sample(tmp_path, ANCHOR, combo)
    _, first = _apply(conn, spotify.scan(folder, cfg))
    assert first["added"] == 31
    plan, again = _apply(conn, spotify.scan(folder, cfg))
    assert again["added"] == 0 and plan["identical"] == 31
    for t in CONFIRMED + ("entity_handles",):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- privacy

def test_ip_country_device_and_userdata_never_reach_the_archive(tmp_path, cfg, conn):
    rows = [ext("2026-06-10T15:00:00Z", 200_000, "Song", "Band", "Album",
                "spotify:track:abc", conn_country="XQ", ip_addr="203.0.113.99",
                platform="unit-test-platform", username="listener1",
                user_agent_decrypted="unit-test-agent/1.0",
                ip_addr_decrypted="203.0.113.98", offline_timestamp=1749567600)]
    target = zip_of(tmp_path / "my_spotify_data.zip", {
        AUDIO: rows,
        "Spotify Account Data/Userdata.json": {
            "username": "listener1", "email": "secret@example.com",
            "birthdate": "1990-01-31", "postalCode": "99999",
            "mobileNumber": "+15125559999"}})
    scan = spotify.scan(target, cfg)
    plan, _ = _apply(conn, scan)
    stored = json.dumps([dict(r) for r in conn.execute("SELECT * FROM observations")])
    shown = json.dumps({"records": scan["records"], "facts": scan["facts"]}) \
        + "\n".join(spotify.report_lines(scan, plan))
    for secret in ("XQ", "203.0.113", "unit-test-platform", "unit-test-agent",
                   "secret@example.com", "1990-01-31", "99999", "5559999",
                   "1749567600"):
        assert secret not in stored and secret not in shown, secret
    assert scan["account"] == "listener1"           # the username labels the import


def test_artists_and_shows_are_not_handles(tmp_path, cfg):
    scan = spotify.scan(sample.write_sample(tmp_path, ANCHOR, "android"), cfg)
    assert scan["handles"] == []
    assert all(r["participants"] == [] for r in scan["records"])


# ---------------------------------------------------------------- aggregation & time

def test_plays_of_a_track_on_one_day_are_one_record(tmp_path, cfg):
    rows = [ext("2026-06-10T15:00:00Z", 200_000, "Song", "Band", "Album", "spotify:track:a"),
            ext("2026-06-10T18:00:00Z", 100_000, "Song", "Band", "Album", "spotify:track:a",
                skipped=True),
            ext("2026-06-11T15:00:00Z", 200_000, "Song", "Band", "Album", "spotify:track:a")]
    scan = spotify.scan(write_json(tmp_path / "Streaming_History_Audio_2026_0.json", rows),
                        cfg)
    first, second = scan["records"]
    assert (first["journal_date"], second["journal_date"]) == ("2026-06-10", "2026-06-11")
    assert first["value"] == 5.0 and first["unit"] == "min"
    assert _detail(first) == {
        "plays": 2, "skipped": 1, "first_play": "2026-06-10T14:56:40Z",
        "last_play": "2026-06-10T18:00:00Z", "album": "Album", "uri": "spotify:track:a",
        "kind": "music"}
    assert (first["started_at"], first["ended_at"]) == ("2026-06-10T14:56:40Z",
                                                         "2026-06-10T18:00:00Z")
    assert first["last_modified_at"] == first["ended_at"]
    assert (first["title"], first["body"], first["origin_app"]) == ("Song", "Band", "spotify")


def test_a_play_is_dated_by_its_start_with_the_rollover(tmp_path, cfg):
    """A play ending at 00:40 Austin time belongs to the evening before; so
    does one that started at 04:55 and ran past the 05:00 rollover."""
    rows = [ext("2026-06-11T05:40:00Z", 200_000, "Late", "Band"),
            ext("2026-06-11T10:10:00Z", 900_000, "Straddle", "Band"),
            ext("2026-06-11T10:20:00Z", 300_000, "Morning", "Band")]
    scan = spotify.scan(write_json(tmp_path / "Streaming_History_Audio_2026_0.json", rows),
                        cfg)
    days = {r["title"]: r["journal_date"] for r in scan["records"]}
    assert days == {"Late": "2026-06-10", "Straddle": "2026-06-10", "Morning": "2026-06-11"}


def test_account_data_end_times_are_utc_to_the_minute(tmp_path, cfg):
    rows = [acct("2026-06-10 23:59", "Band", "Song", 61_500)]
    (rec,) = spotify.scan(write_json(tmp_path / "StreamingHistory_music_0.json", rows),
                          cfg)["records"]
    assert rec["ended_at"] == "2026-06-10T23:59:00Z"
    assert rec["started_at"] == "2026-06-10T23:57:59Z"
    assert rec["journal_date"] == "2026-06-10"         # 18:57 in Austin
    assert _detail(rec) == {"plays": 1, "first_play": "2026-06-10T23:57:59Z",
                            "last_play": "2026-06-10T23:59:00Z", "kind": "music"}


def test_plays_under_30_seconds_are_dropped_and_counted(tmp_path, cfg):
    rows = [acct("2026-06-10 15:00", "Band", "Skip", 29_999),
            acct("2026-06-10 15:01", "Band", "Keep", 30_000),
            acct("2026-06-10 15:02", "Band", "Zero", 0)]
    scan = spotify.scan(write_json(tmp_path / "StreamingHistory_music_0.json", rows), cfg)
    assert [r["title"] for r in scan["records"]] == ["Keep"]
    assert scan["skipped"] == ["2 × " + spotify.SHORT]


def test_names_not_uris_are_the_identity(tmp_path, cfg):
    """Account data has no URIs, so the same track-day from either download
    must be one record — spelled with different case or spacing too."""
    a = spotify.scan(write_json(tmp_path / "a" / "StreamingHistory_music_0.json",
                                [acct("2026-06-10 15:00", "The Band", "A  Song", 60_000)]),
                     cfg)
    b = spotify.scan(write_json(tmp_path / "b" / "Streaming_History_Audio_2026_0.json",
                                [ext("2026-06-10T15:00:30Z", 90_000, "a song", "the band",
                                     uri="spotify:track:one"),
                                 ext("2026-06-10T16:00:00Z", 90_000, "A Song", "The Band",
                                     uri="spotify:track:other")]), cfg)
    assert a["records"][0]["external_id"] == b["records"][0]["external_id"]
    assert len(b["records"]) == 1 and _detail(b["records"][0])["plays"] == 2


def test_podcasts_audiobooks_videos_and_the_old_mixed_file(tmp_path, cfg):
    folder = tmp_path / "export"
    write_json(folder / "StreamingHistory_podcast_0.json",
               [{"endTime": "2026-06-10 15:00", "podcastName": "Show",
                 "episodeName": "Ep 1", "msPlayed": 600_000}])
    write_json(folder / "StreamingHistory0.json",
               [acct("2025-06-10 15:00", "Show or Band", "Ep or Song", 600_000)])
    write_json(folder / "Streaming_History_Video_2026.json",
               [ext("2026-06-12T15:00:00Z", 600_000, episode="Video Ep", show="Show",
                    uri="spotify:episode:v")])
    write_json(folder / "Streaming_History_Audio_2026_1.json",
               [ext("2026-06-13T15:00:00Z", 1_200_000, audiobook_title="A Book",
                    audiobook_uri="spotify:show:book",
                    audiobook_chapter_title="Chapter 3")])
    scan = spotify.scan(folder, cfg)
    by_title = {r["title"]: (r["body"], _detail(r)) for r in scan["records"]}
    assert by_title["Ep 1"][0] == "Show" and by_title["Ep 1"][1]["kind"] == "podcast"
    assert "kind" not in by_title["Ep or Song"][1]      # the mixed file cannot tell
    assert by_title["Video Ep"][1]["video"] is True
    assert by_title["Video Ep"][1]["kind"] == "podcast"
    body, book = by_title["A Book"]
    assert body is None and book["kind"] == "audiobook"
    assert book["chapter"] == "Chapter 3" and book["uri"] == "spotify:show:book"


def test_rows_it_does_not_understand_are_counted_never_hidden(tmp_path, cfg):
    rows = [ext("2026-06-10T15:00:00Z", 200_000, "Song", "Band"),
            ext(None, 200_000, "No time", "Band"),
            ext("2026-06-10T16:00:00Z", None, "No length", "Band"),
            ext("2026-06-10T17:00:00Z", 200_000),                       # nameless
            ext("1970-01-01T00:01:00Z", 200_000, "Before 1970", "Band"),
            ext("2026-06-10T18:00:00Z", 90_000_000, "A day long", "Band"),
            {"something": "else"}, "not an object"]
    scan = spotify.scan(write_json(tmp_path / "Streaming_History_Audio_2026_0.json", rows),
                        cfg)
    assert [r["title"] for r in scan["records"]] == ["Song"]
    what = {u["what"]: u["count"] for u in scan["unmapped"]}
    assert what == {"play with no readable ts": 1, "play with no readable ms_played": 1,
                    "play with an impossible time or length": 2,
                    "play of an unknown shape (something)": 1,
                    "play that is not an object": 1}
    assert scan["skipped"] == ["1 × " + spotify.NAMELESS]


# ---------------------------------------------------------------- the two downloads

def _both(tmp_path):
    folder = tmp_path / "Spotify"
    zip_of(folder / "my_spotify_data.zip", {MUSIC: [
        acct("2026-06-09 15:00", "Band", "Older day", 200_000),
        acct("2026-06-10 15:00", "Band", "Song", 200_000)]})
    zip_of(folder / "my_spotify_data (1).zip", {AUDIO: [
        ext("2026-06-10T15:00:41Z", 200_000, "Song", "Band", "Album", "spotify:track:s")]})
    return folder


def test_in_one_scan_the_extended_history_wins_the_days_it_covers(tmp_path, cfg):
    scan = spotify.scan(_both(tmp_path), cfg)
    song = _rec(scan, "Song", "2026-06-10")
    assert _detail(song)["uri"] == "spotify:track:s" and _detail(song)["plays"] == 1
    assert _rec(scan, "Older day", "2026-06-09")       # a day only Account data has
    assert scan["skipped"] == ["1 × " + spotify.COVERED]
    assert scan["facts"]["formats"] == {"account": {"files": 1, "plays": 2},
                                        "extended": {"files": 1, "plays": 1}}


def test_imported_one_after_the_other_the_fuller_record_stays(tmp_path, cfg, conn):
    account = zip_of(tmp_path / "a" / "my_spotify_data.zip", {MUSIC: [
        acct("2026-06-10 15:00", "Band", "Song", 200_000)]})
    extended = zip_of(tmp_path / "e" / "my_spotify_data.zip", {AUDIO: [
        ext("2026-06-10T15:00:41Z", 200_000, "Song", "Band", "Album", "spotify:track:s")]})
    _apply(conn, spotify.scan(account, cfg))
    plan, result = _apply(conn, spotify.scan(extended, cfg))
    assert result["updated"] == 1                      # ends at the second: newer
    later = spotify.scan(account, cfg)
    plan = obs.build_plan(conn, later)
    assert len(plan["conflicts"]) == 1 and not plan["updates"]
    lines = spotify.report_lines(later, plan)
    assert any(line.startswith("  kept existing: 1 track-day(s) the other Spotify download")
               for line in lines)
    stored = conn.execute("SELECT value_json FROM observations").fetchone()[0]
    assert json.loads(stored)["uri"] == "spotify:track:s"


def test_overlapping_downloads_in_one_folder_count_a_play_once(tmp_path, cfg):
    folder = tmp_path / "Spotify"
    zip_of(folder / "my_spotify_data.zip", {MUSIC: [
        acct("2026-05-10 15:00", "Band", "Song", 40_000),
        acct("2026-05-10 15:00", "Band", "Song", 40_000)]})      # twice in a minute
    zip_of(folder / "my_spotify_data (1).zip", {MUSIC: [
        acct("2026-05-10 15:00", "Band", "Song", 40_000),
        acct("2026-06-10 15:00", "Band", "Newer", 40_000)]})
    scan = spotify.scan(folder, cfg)
    assert _detail(_rec(scan, "Song", "2026-05-10"))["plays"] == 2
    assert _rec(scan, "Newer", "2026-06-10")
    assert scan["skipped"] == ["1 × " + spotify.OVERLAP]


def test_two_accounts_in_one_folder_are_refused(tmp_path, cfg):
    folder = tmp_path / "Spotify"
    zip_of(folder / "a.zip", {MUSIC: [acct("2026-06-10 15:00", "B", "S", 60_000)],
                              "Spotify Account Data/Userdata.json": {"username": "alex"}})
    zip_of(folder / "b.zip", {MUSIC: [acct("2026-06-10 15:00", "B", "S", 60_000)],
                              "Spotify Account Data/Userdata.json": {"username": "maya"}})
    with pytest.raises(DomainError, match=r"more than one Spotify account \(alex, maya\)"):
        spotify.scan(folder, cfg)


# ---------------------------------------------------------------- re-importing

def test_today_grows_and_updates_while_earlier_days_stay_identical(tmp_path, cfg, conn):
    path = tmp_path / "Streaming_History_Audio_2026_0.json"
    first = [ext("2026-06-09T15:00:00Z", 200_000, "Song", "Band"),
             ext("2026-06-10T15:00:00Z", 200_000, "Song", "Band")]
    _apply(conn, spotify.scan(write_json(path, first), cfg))
    grown = first + [ext("2026-06-10T20:00:00Z", 100_000, "Song", "Band")]
    plan, result = _apply(conn, spotify.scan(write_json(path, grown), cfg))
    assert (result["updated"], plan["identical"]) == (1, 1)
    row = conn.execute("SELECT value, value_json FROM observations "
                       "WHERE journal_date='2026-06-10'").fetchone()
    assert row["value"] == 5.0 and json.loads(row["value_json"])["plays"] == 2


def test_nothing_is_mirrored(tmp_path, cfg, conn):
    path = tmp_path / "StreamingHistory_music_0.json"
    _apply(conn, spotify.scan(write_json(path, [
        acct("2025-06-10 15:00", "Band", "Aged out", 60_000),
        acct("2026-06-10 15:00", "Band", "Recent", 60_000)]), cfg))
    scan = spotify.scan(write_json(path, [acct("2026-06-10 15:00", "Band", "Recent",
                                               60_000)]), cfg)
    assert "present_ids" not in scan
    plan, _ = _apply(conn, scan)
    assert plan["removed"] == []
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


# ---------------------------------------------------------------- bad input

def test_a_bom_prefixed_history_file_reads(tmp_path, cfg):
    path = write_json(tmp_path / "StreamingHistory_music_0.json",
                      [acct("2026-06-10 15:00", "Band", "Song", 60_000)], bom=True)
    assert len(spotify.scan(path, cfg)["records"]) == 1


def test_a_zip_without_listening_history_names_what_it_holds(tmp_path, cfg):
    target = zip_of(tmp_path / "x.zip", {"Spotify Account Data/YourLibrary.json": {},
                                         "Spotify Account Data/Inferences.json": {}})
    with pytest.raises(DomainError) as e:
        spotify.scan(target, cfg)
    msg = str(e.value)
    assert "\n" not in msg and "holds no Spotify listening history" in msg
    assert "YourLibrary.json" in msg


@pytest.mark.parametrize("content, expected", [
    (b"{not json", "is not valid JSON: line 1"),
    (b'{"a": 1}', "is a JSON dict, not a list of plays"),
    (b'[{"a": 1}]', "is a list, but not of Spotify plays — the first item has: a"),
    (b"\xff\xfe[]", "is not valid UTF-8"),
])
def test_a_bad_history_file_fails_in_one_line_saying_what_it_saw(tmp_path, cfg, content,
                                                                 expected):
    path = tmp_path / "StreamingHistory_music_0.json"
    path.write_bytes(content)
    with pytest.raises(DomainError) as e:
        spotify.scan(path, cfg)
    assert expected in str(e.value) and "\n" not in str(e.value)


def test_a_file_that_is_not_json_or_a_zip_is_refused(tmp_path, cfg):
    path = tmp_path / "history.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(DomainError, match="is not a .zip, a folder or a .json file"):
        spotify.scan(path, cfg)


# ---------------------------------------------------------------- determinism & report

def test_scan_is_deterministic(tmp_path, cfg):
    folder = _both(tmp_path)
    a, b = spotify.scan(folder, cfg), spotify.scan(folder, cfg)
    assert a == b and a["source_hash"]


def test_report_names_the_stream_span_and_what_it_dropped(tmp_path, cfg, conn):
    scan = spotify.scan(sample.write_sample(tmp_path, ANCHOR, "android"), cfg)
    lines = spotify.report_lines(scan, obs.build_plan(conn, scan))
    text = "\n".join(lines)
    trip = persona.bled_trip_days(ANCHOR)[0]
    assert lines[0].startswith("spotify: Spotify (Account data: 2 file(s), 29 play(s); "
                               "extended streaming history: 1 file(s), 11 play(s); "
                               "account alexrivera94)")
    # The oldest record is the podcast three days before the trip, at home.
    oldest = trip - timedelta(days=sample.TRIP_PODCAST_DAYS_BEFORE)
    assert f"  music_play: 31 new, 0 changed | {oldest.isoformat()}..{_day(-1)} | e.g. " \
        in text
    assert "skipped: 2 × play under 30 seconds" in text
    assert "not read: 1 other file(s) of the download" in text and "YourLibrary.json" in text
    assert "never read: the IP address, country, device and user agent" in text


def test_report_says_nothing_new_on_a_re_import(tmp_path, cfg, conn):
    folder = sample.write_sample(tmp_path, ANCHOR, "android")
    _apply(conn, spotify.scan(folder, cfg))
    scan = spotify.scan(folder, cfg)
    lines = spotify.report_lines(scan, obs.build_plan(conn, scan))
    assert "  nothing new — the download matches the database" in lines
    assert "  31 identical (skipped)" in lines


# ---------------------------------------------------------------- default target & CLI

def test_default_target_is_the_configured_path(tmp_path, cfg):
    target = _both(tmp_path)
    cfg.connectors = {"spotify": {"path": str(target)}}
    assert spotify.default_target(cfg) == target
    cfg.connectors = {"spotify": {"path": str(tmp_path / "missing")}}
    with pytest.raises(DomainError, match=r"\[connectors.spotify\] path does not exist"):
        spotify.default_target(cfg)


def test_default_target_finds_the_one_download(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    monkeypatch.setattr(platforms, "onedrive_candidates", lambda: [])
    monkeypatch.setattr(platforms, "is_mac", lambda: False)
    with pytest.raises(DomainError, match="no Spotify download found"):
        spotify.default_target(cfg)
    one = zip_of(tmp_path / "Downloads" / "my_spotify_data.zip", {MUSIC: []})
    assert spotify.default_target(cfg) == one
    zip_of(tmp_path / "Downloads" / "my_spotify_data (1).zip", {AUDIO: []})
    with pytest.raises(DomainError, match="found several Spotify downloads — put them "
                                          "in one folder"):
        spotify.default_target(cfg)


def test_cli_dry_run_then_apply_then_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    folder = sample.write_sample(tmp_path / "exp", ANCHOR, "iphone")
    dry = runner.invoke(app, ["observe", "spotify", str(folder)])
    assert dry.exit_code == 0, dry.output
    assert "music_play: 31 new" in dry.output
    assert "dry run — nothing written" in dry.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0
    applied = runner.invoke(app, ["observe", "spotify", str(folder), "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "31 added" in applied.output
    again = runner.invoke(app, ["observe", "spotify", str(folder), "--apply"])
    assert "0 added" in again.output and "31 identical skipped" in again.output
    imports = runner.invoke(app, ["sql", "SELECT account FROM imports", "--json"])
    assert {r["account"] for r in json.loads(imports.output)} == {sample.USERNAME}
