"""Google activity connector: Takeout's My Activity and YouTube history into
web_search and video_watch.

scan() is a pure reader, so these write Takeout files — JSON arrays and the
default HTML pages, some by hand in the markup Takeout produces, some with the
demo generator's writers — and check what is easy to get wrong: the same
event from JSON and HTML being one record, HTML dates in a named zone (and
the zones it does not know), the rollover hour, what is skipped and counted,
reading in chunks, and a retitled video staying one watch.
"""

import json
import os
import zipfile
from datetime import date, datetime, timezone

import pytest

from lifecrm import platforms
from lifecrm.connectors import google_activity as ga
from lifecrm.demo.sources import google_activity as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

ANCHOR = date(2026, 10, 21)
CONFIRMED = ("entries", "measurements", "mentions", "entity_aliases", "entity_links")


def search(query, time, product="Search", **extra):
    return {"header": product, "title": f"Searched for {query}",
            "titleUrl": "https://www.google.com/search?q=" + query.replace(" ", "+"),
            "time": time, "products": [product],
            "activityControls": ["Web & App Activity"], **extra}


def watch(title, channel, time, vid="abcdefghijk", product="YouTube"):
    return {"header": product, "title": f"Watched {title}",
            "titleUrl": f"https://www.youtube.com/watch?v={vid}",
            "subtitles": [{"name": channel, "url": "https://www.youtube.com/channel/UCchan"}],
            "time": time, "products": [product],
            "activityControls": ["YouTube watch history"]}


def write_json(path, items, bom=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"")
                     + json.dumps(items, indent=2, ensure_ascii=False).encode("utf-8"))
    return path


def activity(root, product, items, **kw):
    return write_json(root / "Takeout" / "My Activity" / product / "MyActivity.json",
                      items, **kw)


def history(root, kind, items):
    return write_json(root / "Takeout" / "YouTube and YouTube Music" / "history"
                      / f"{kind}-history.json", items)


def write_html(path, cells, bom=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    page = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
            '<title>My Activity</title><style type="text/css">.mdl-grid{display:flex}'
            '</style></head><body><div class="mdl-grid">' + "".join(cells)
            + "</div></body></html>")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + page.encode("utf-8"))
    return path


def cell(header, content, caption="<b>Products:</b><br>&emsp;Search<br>"):
    """One entry, in the markup Takeout writes."""
    return ('<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
            '<div class="mdl-grid"><div class="header-cell mdl-cell mdl-cell--12-col">'
            f'<p class="mdl-typography--title">{header}<br></p></div>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
            f'{content}</div><div class="content-cell mdl-cell mdl-cell--6-col '
            'mdl-typography--body-1 mdl-typography--text-right"></div>'
            '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
            f'{caption}<b>Why is this here?</b><br>&emsp;This activity was saved to your '
            'Google Account because the following settings were on:&nbsp;Web &amp; App '
            'Activity.&nbsp;You can control these settings &nbsp;<a href="https://'
            'myaccount.google.com/activitycontrols">here</a>.</div></div></div>')


def html_search(query, when, header="Search"):
    return cell(header, f'Searched for&nbsp;<a href="https://www.google.com/search?q='
                        f'{query.replace(" ", "+")}">{query}</a><br>{when}<br>',
                f"<b>Products:</b><br>&emsp;{header}<br>")


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _plan(scan):
    return {"new": scan["records"], "updates": [], "refreshed": [], "conflicts": [],
            "identical": 0, "dup_incoming": 0, "invalid": []}


def _report(scan, plan=None):
    return "\n".join(ga.report_lines(scan, plan or _plan(scan)))


# ---------------------------------------------------------------- field mapping

def test_a_search_and_a_watch_map_to_their_fields(tmp_path, cfg):
    root = tmp_path / "export"
    activity(root, "Search", [search("lake bled pletna boat", "2026-10-12T02:41:07.123Z")])
    history(root, "watch", [watch("Lake Bled in 4K", "Wander Slowly",
                                  "2026-10-11T18:44:03.5Z")])
    scan = ga.scan(root, cfg)
    by = {r["stream"]: r for r in scan["records"]}
    s, w = by["web_search"], by["video_watch"]
    assert s["title"] == "lake bled pletna boat" and s["body"] is None
    assert s["started_at"] == s["last_modified_at"] == "2026-10-12T02:41:07Z"
    assert s["journal_date"] == "2026-10-11"                  # 21:41 in Austin
    assert json.loads(s["value_json"]) == {
        "product": "Search", "url": "https://www.google.com/search?q=lake+bled+pletna+boat"}
    assert s["origin_app"] == "google_search"
    assert w["title"] == "Lake Bled in 4K" and w["body"] == "Wander Slowly"
    assert json.loads(w["value_json"]) == {
        "product": "YouTube", "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "channel": "Wander Slowly", "channel_url": "https://www.youtube.com/channel/UCchan"}
    assert w["origin_app"] == "youtube"
    for r in scan["records"]:
        assert r["participants"] == [] and r["direction"] is None
        assert r["ended_at"] is None and r["utc_offset_sec"] is None
        assert r["value"] is None and r["unit"] is None
    assert scan["handles"] == [] and scan["account"] is None


def test_searches_in_every_product_are_web_searches(tmp_path, cfg):
    root = tmp_path / "export"
    activity(root, "Maps", [search("Uchi", "2026-10-01T01:00:00Z", product="Maps")])
    activity(root, "Google Play Store",
             [search("todo app", "2026-10-02T01:00:00Z", product="Google Play Store")])
    history(root, "search", [{"header": "YouTube", "title": "Searched for bench press",
                              "titleUrl": "https://www.youtube.com/results?search_query="
                                          "bench+press",
                              "time": "2026-10-03T01:00:00Z"}])
    scan = ga.scan(root, cfg)
    got = {(json.loads(r["value_json"])["product"], r["title"], r["origin_app"])
           for r in scan["records"]}
    assert got == {("Maps", "Uchi", "google_maps"),
                   ("Google Play Store", "todo app", "google_play_store"),
                   ("YouTube", "bench press", "youtube")}
    assert {r["stream"] for r in scan["records"]} == {"web_search"}


def test_an_entry_without_a_header_takes_its_product_from_the_folder(tmp_path, cfg):
    item = search("vintgar gorge opening hours", "2026-10-01T01:00:00Z")
    del item["header"], item["products"]
    activity(tmp_path / "export", "Image Search", [item])
    (rec,) = ga.scan(tmp_path / "export", cfg)["records"]
    assert json.loads(rec["value_json"])["product"] == "Image Search"


# ---------------------------------------------------------------- HTML

_WATCH_CELL = cell(
    "YouTube",
    'Watched\xa0<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;t=10s">Tacos &amp; '
    'Tunes</a><br><a href="https://www.youtube.com/channel/UC123">Kitchen  Ch.</a><br>'
    "Oct 12, 2026, 9:41:07 PM CDT<br>",
    "<b>Products:</b><br>&emsp;YouTube<br>")


def test_the_html_page_reads_titles_links_channels_and_dates(tmp_path, cfg):
    page = write_html(tmp_path / "MyActivity.html", [
        _WATCH_CELL,
        html_search("knee surgery recovery time", "12 Oct 2026, 21:41:07 BST"),
    ])
    scan = ga.scan(page, cfg)
    w = next(r for r in scan["records"] if r["stream"] == "video_watch")
    assert (w["title"], w["body"]) == ("Tacos & Tunes", "Kitchen Ch.")
    detail = json.loads(w["value_json"])
    assert detail["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s"
    assert detail["channel_url"] == "https://www.youtube.com/channel/UC123"
    assert w["started_at"] == "2026-10-13T02:41:07Z" and w["journal_date"] == "2026-10-12"
    s = next(r for r in scan["records"] if r["stream"] == "web_search")
    assert s["title"] == "knee surgery recovery time"
    assert s["started_at"] == "2026-10-12T20:41:07Z"
    assert scan["facts"]["formats"] == {"html": 1}
    assert scan["facts"]["zones"] == [["BST", 1], ["CDT", 1]]


@pytest.mark.parametrize("line,utc", [
    ("Oct 12, 2026, 9:41:07 PM CDT", "2026-10-13T02:41:07Z"),
    ("Jan 5, 2026, 9:41:07 AM CST", "2026-01-05T15:41:07Z"),
    ("Jul 4, 2026, 12:00:00 PM PDT", "2026-07-04T19:00:00Z"),
    ("Jul 4, 2026, 12:30:00 AM EDT", "2026-07-04T04:30:00Z"),
    ("Feb 1, 2026, 6:00:00 PM EST", "2026-02-01T23:00:00Z"),
    ("Mar 1, 2026, 6:00:00 PM MST", "2026-03-02T01:00:00Z"),
    ("Jun 1, 2026, 6:00:00 PM MDT", "2026-06-02T00:00:00Z"),
    ("Dec 24, 2025, 11:59:59 PM PST", "2025-12-25T07:59:59Z"),
    ("12 Oct 2026, 21:41:07 BST", "2026-10-12T20:41:07Z"),
    ("12 Dec 2026, 21:41:07 GMT", "2026-12-12T21:41:07Z"),
    ("26 Sept 2022, 13:18:02 CEST", "2022-09-26T11:18:02Z"),
    ("2 Jan 2023, 10:00:00 CET", "2023-01-02T09:00:00Z"),
    ("2 Jan 2023, 10:00:00 UTC", "2023-01-02T10:00:00Z"),
    ("2 Jan 2023, 10:00:00 GMT+02:00", "2023-01-02T08:00:00Z"),
    ("Jan 2, 2023, 10:00:00 AM GMT-5", "2023-01-02T15:00:00Z"),
    ("Jan 2, 2023, 10:00:00 AM GMT−5", "2023-01-02T15:00:00Z"),
])
def test_html_dates_in_a_named_zone_are_read_exactly(line, utc):
    at, _, known = ga._html_time(line, "America/Chicago")
    assert at == utc and known


@pytest.mark.parametrize("line", [
    "12.10.2026, 21:41:07 MESZ", "2026-10-12, 9:41:07 p.m. EDT", "12 Octember",
    "Oct 32, 2026, 9:41:07 PM CDT", "Oct 12, 2026, 13:41:07 PM CDT", "",
])
def test_dates_in_another_form_are_not_guessed(line):
    assert ga._html_time(line, "America/Chicago") is None


def test_a_zone_it_does_not_know_is_read_in_the_home_timezone_and_marked(tmp_path, cfg):
    page = write_html(tmp_path / "MyActivity.html", [
        html_search("bled", "12 Oct 2026, 9:41:07 pm AEST"),
        html_search("castle", "Oct 12, 2026, 9:42:07 PM"),
    ])
    scan = ga.scan(page, cfg)
    bled = next(r for r in scan["records"] if r["title"] == "bled")
    assert bled["started_at"] == "2026-10-13T02:41:07Z"         # 21:41 read as Austin time
    assert json.loads(bled["value_json"])["time_source"] == "home_timezone"
    assert scan["facts"]["home_zones"] == [["(no zone)", 1], ["AEST", 1]]
    assert ("times: 2 date(s) name a zone this reader does not know ((no zone), AEST) — "
            "read as America/Chicago wall time and marked time_source=home_timezone"
            in _report(scan))


def test_a_page_in_another_date_format_is_counted_with_an_example_not_imported(tmp_path, cfg):
    page = write_html(tmp_path / "MyActivity.html", [
        html_search("bled", "12.10.2026, 21:41:07 MESZ"),
        html_search("castle", "13.10.2026, 08:00:00 MESZ"),
        html_search("lake", "Oct 12, 2026, 9:41:07 PM CDT"),
    ])
    scan = ga.scan(page, cfg)
    assert [r["title"] for r in scan["records"]] == ["lake"]
    assert scan["unmapped"] == [{"what": "entries dated in a form this reader does not know "
                                         "(English US/UK dates only), e.g. '12.10.2026, "
                                         "21:41:07 MESZ'", "count": 2}]
    assert scan["skipped"] == []
    assert "not read: 2 × entries dated in a form" in _report(scan)


def test_the_same_event_from_json_and_html_is_one_record(tmp_path, cfg):
    e = sample._Entry("Search", datetime(2026, 10, 12, 21, 41, 7, tzinfo=sample.ZoneInfo(
        "America/Chicago")), "Searched for", "uchi austin reservations",
        "https://www.google.com/search?q=uchi+austin+reservations")
    w = sample._Entry("YouTube", datetime(2022, 9, 27, 22, 30, 8, tzinfo=sample._TRIP_ZONE),
                      "Watched", "Pletna Boats & the Island", sample.video_url("x"),
                      ("YouTube watch history",), channel="Wander Slowly")
    js = ga.scan(write_json(tmp_path / "j" / "MyActivity.json",
                            [sample._json_entry(e), sample._json_entry(w)]), cfg)
    ht = ga.scan(write_html(tmp_path / "h" / "MyActivity.html",
                            [sample._html_entry(e), sample._html_entry(w)]), cfg)
    assert js["records"] == ht["records"] and len(js["records"]) == 2
    both = tmp_path / "both" / "My Activity" / "Search"
    both.mkdir(parents=True)
    (both / "MyActivity.json").write_bytes((tmp_path / "j" / "MyActivity.json").read_bytes())
    (both / "MyActivity.html").write_bytes((tmp_path / "h" / "MyActivity.html").read_bytes())
    scan = ga.scan(tmp_path / "both", cfg)
    assert len(scan["records"]) == 2
    assert scan["skipped"] == ["2 × event listed twice (in two files, or twice in one) — "
                               "kept once"]
    assert scan["facts"]["formats"] == {"html": 1, "json": 1}


def test_a_watch_in_my_activity_and_in_youtube_history_is_kept_once(tmp_path, cfg):
    item = watch("Bench Press Form", "Barbell Basics", "2026-10-10T00:58:31.120Z")
    root = tmp_path / "export"
    activity(root, "YouTube", [item])
    history(root, "watch", [{**item, "time": "2026-10-10T00:58:31.981Z"}])
    scan = ga.scan(root, cfg)
    assert len(scan["records"]) == 1
    assert "1 × event listed twice (in two files, or twice in one) — kept once" in \
        scan["skipped"]


# ---------------------------------------------------------------- times

def test_a_search_after_midnight_belongs_to_the_evening_before(tmp_path, cfg):
    p = activity(tmp_path, "Search", [search("how to fall asleep", "2026-10-17T05:38:44Z"),
                                      search("coffee near me", "2026-10-17T10:05:00Z")])
    got = {r["title"]: r["journal_date"] for r in ga.scan(p, cfg)["records"]}
    assert got == {"how to fall asleep": "2026-10-16", "coffee near me": "2026-10-17"}
    cfg.day_rollover_hour = 0
    got = {r["title"]: r["journal_date"] for r in ga.scan(p, cfg)["records"]}
    assert got["how to fall asleep"] == "2026-10-17"


def test_times_are_utc_whatever_the_machine_zone(tmp_path, cfg):
    p = activity(tmp_path, "Search", [search("x", "2026-07-01T23:59:59.999Z")])
    (rec,) = ga.scan(p, cfg)["records"]
    assert rec["started_at"] == "2026-07-01T23:59:59Z"
    instant = datetime(2026, 7, 1, 23, 59, 59, tzinfo=timezone.utc)
    assert rec["journal_date"] == "2026-07-01" == instant.astimezone(
        sample.ZoneInfo("America/Chicago")).date().isoformat()


# ---------------------------------------------------------------- skipped, counted

def test_what_is_not_a_search_or_a_watch_is_skipped_and_counted_per_product(tmp_path, cfg):
    t = "2026-10-01T15:00:00Z"
    root = tmp_path / "export"
    activity(root, "Search", [
        {"header": "Search", "title": "Visited Lake Bled - Wikipedia",
         "titleUrl": "https://www.google.com/url?q=https://en.wikipedia.org/wiki/Lake_Bled",
         "time": t},
        search("no time", None),
        {"header": "Search", "title": "Searched for", "time": t},
        "not an object",
    ])
    activity(root, "Chrome", [{"header": "Chrome", "title": "Visited Inbox", "time": t},
                              {"header": "Chrome", "title": "Used Chrome", "time": t}])
    activity(root, "Maps", [{"header": "Maps", "title": "Directions to Uchi", "time": t},
                            {"header": "Maps", "title": "Viewed area in Google Maps",
                             "time": t}])
    activity(root, "Discover", [{"header": "Discover", "title": "5 cards in your feed",
                                 "time": t}])
    activity(root, "Assistant", [{"header": "Assistant", "title": "Said set a timer",
                                  "time": t}])
    activity(root, "Ads", [{"header": "Ads", "title": "Visited shoes.example", "time": t}])
    activity(root, "Google Play Movies & TV", [
        {"header": "Google Play Movies & TV", "title": "Watched Up", "time": t}])
    history(root, "watch", [
        {**watch("Fall Gear Sale", "Trailhead", t), "details": [{"name": "From Google Ads"}]},
        {"header": "YouTube", "title": "Watched a video that has been removed", "time": t},
        watch("kept", "Crag Notes", t),
    ])
    scan = ga.scan(root, cfg)
    assert [r["title"] for r in scan["records"]] == ["kept"]
    assert scan["skipped"] == [
        "1 × Ads: ad (From Google Ads)",
        "1 × Assistant: voice activity — not imported",
        "1 × Chrome: Used — neither a search nor a YouTube watch",
        "1 × Chrome: Visited a page — browsing is the browser connector's (docs/browser.md)",
        "1 × Discover: feed activity — not imported",
        "1 × Google Play Movies & TV: Watched — neither a search nor a YouTube watch",
        "1 × Maps: Directions to — neither a search nor a YouTube watch",
        "1 × Maps: Viewed — neither a search nor a YouTube watch",
        "1 × Search: Visited a page — browsing is the browser connector's (docs/browser.md)",
        "1 × Search: search naming nothing",
        "1 × Search: search with no time",
        "1 × YouTube: ad (From Google Ads)",
        "1 × YouTube: watch of a video since removed (no link left)",
        "1 × activity entry that is not an object",
    ]
    text = _report(scan)
    for line in scan["skipped"]:
        assert f"skipped: {line}" in text


def test_other_takeout_products_are_named_not_read(tmp_path, cfg):
    root = tmp_path / "export"
    activity(root, "Search", [search("x", "2026-10-01T15:00:00Z")])
    photos = root / "Takeout" / "Google Photos" / "Trip"
    photos.mkdir(parents=True)
    for i in range(3):
        (photos / f"IMG_{i}.jpg").write_bytes(b"\xff\xd8")
    (root / "Takeout" / "archive_browser.html").write_text("<html></html>", "utf-8")
    (root / "Takeout" / "My Activity" / "Assistant").mkdir(parents=True)
    (root / "Takeout" / "My Activity" / "Assistant" / "rec.mp3").write_bytes(b"ID3")
    scan = ga.scan(root, cfg)
    assert scan["unmapped"] == [
        {"what": "archive_browser.html — not My Activity or YouTube history", "count": 1},
        {"what": "file(s) in Google Photos/ — not My Activity or YouTube history", "count": 3},
        {"what": "file(s) in My Activity/Assistant/ — not My Activity or YouTube history",
         "count": 1}]
    assert "not read: 3 × file(s) in Google Photos/" in _report(scan)


# ---------------------------------------------------------------- reading in chunks

@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_tiny_chunks_read_exactly_what_whole_files_do(tmp_path, cfg, monkeypatch, combo):
    """Both readers stream; a chunk boundary inside a tag, an entity, a JSON
    string or a multi-byte character must change nothing."""
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    whole = ga.scan(path, cfg)
    monkeypatch.setattr(ga, "_CHUNK", 7)
    small = ga.scan(path, cfg)
    assert small["records"] == whole["records"] and small["skipped"] == whole["skipped"]
    assert len(whole["records"]) == 25


def test_a_bare_number_split_by_a_chunk_is_not_cut_short(tmp_path, cfg, monkeypatch):
    p = tmp_path / "MyActivity.json"
    p.write_text('[123456789, {"header": "Search", "title": "Searched for x", '
                 '"time": "2026-10-01T15:00:00Z"}]', encoding="utf-8")
    monkeypatch.setattr(ga, "_CHUNK", 4)
    scan = ga.scan(p, cfg)
    assert [r["title"] for r in scan["records"]] == ["x"]
    assert scan["skipped"] == ["1 × activity entry that is not an object"]


# ---------------------------------------------------------------- re-importing

def test_a_retitled_video_stays_one_watch_and_keeps_its_imported_title(tmp_path, cfg, conn):
    history(tmp_path / "a", "watch", [watch("Bench Press Form", "Barbell Basics",
                                            "2026-10-10T00:58:31Z")])
    _apply(conn, ga.scan(tmp_path / "a", cfg))
    history(tmp_path / "b", "watch", [watch("Bench Press Form (FIXED!)", "Barbell Basics TV",
                                            "2026-10-10T00:58:31Z")])
    scan = ga.scan(tmp_path / "b", cfg)
    plan, result = _apply(conn, scan)
    assert len(plan["conflicts"]) == 1 and result["added"] == 0
    assert tuple(conn.execute("SELECT title, body FROM observations").fetchone()) == \
        ("Bench Press Form", "Barbell Basics")
    text = _report(scan, plan)
    assert "kept existing: 1 watch(es) whose video or channel has been renamed since" in text
    assert "CONFLICT" not in text


def test_activity_auto_delete_removes_nothing_from_the_archive(tmp_path, cfg, conn):
    activity(tmp_path / "a", "Search", [search("old", "2023-01-01T15:00:00Z"),
                                        search("new", "2026-10-01T15:00:00Z")])
    _apply(conn, ga.scan(tmp_path / "a", cfg))
    activity(tmp_path / "b", "Search", [search("new", "2026-10-01T15:00:00Z"),
                                        search("newer", "2026-10-02T15:00:00Z")])
    scan = ga.scan(tmp_path / "b", cfg)
    assert "present_ids" not in scan and "mirror_streams" not in scan
    plan, result = _apply(conn, scan)
    assert plan["removed"] == [] and result["added"] == 1 and plan["identical"] == 1
    assert {r["title"] for r in conn.execute("SELECT title FROM observations")} == \
        {"old", "new", "newer"}


def test_apply_then_reapply_is_identical_and_leaves_the_confirmed_tier_alone(
        tmp_path, cfg, conn):
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in CONFIRMED}
    path = sample.write_sample(tmp_path, ANCHOR, "android")
    _, first = _apply(conn, ga.scan(path, cfg))
    assert first["added"] == 25
    plan, again = _apply(conn, ga.scan(path, cfg))
    assert again["added"] == 0 and plan["identical"] == 25 and not plan["conflicts"]
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in CONFIRMED}
    assert after == before == dict.fromkeys(CONFIRMED, 0)
    assert conn.execute("SELECT COUNT(*) FROM entity_handles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM observation_participants").fetchone()[0] == 0
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- bad input

def _one_line(excinfo):
    assert "\n" not in str(excinfo.value)
    return str(excinfo.value)


def test_a_file_that_is_not_json_or_html_is_refused(tmp_path, cfg):
    p = tmp_path / "notes.txt"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(DomainError, match="notes.txt is not a .json or .html file") as e:
        ga.scan(p, cfg)
    _one_line(e)


def test_a_json_object_says_it_is_not_an_activity_list(tmp_path, cfg):
    p = write_json(tmp_path / "MyActivity.json", {"semanticSegments": []})
    with pytest.raises(DomainError, match="starts with a JSON object, not a list of "
                                          "activity entries") as e:
        ga.scan(p, cfg)
    _one_line(e)


def test_broken_json_names_the_entry(tmp_path, cfg):
    p = tmp_path / "MyActivity.json"
    p.write_text('[{"header": "Search", "title": "Searched for a", "time": "2026-10-01T15:'
                 '00:00Z"}, {"header": "Search", "title": ', encoding="utf-8")
    with pytest.raises(DomainError, match="MyActivity.json is not valid JSON at entry 2") as e:
        ga.scan(p, cfg)
    _one_line(e)
    p.write_text('[{"a": 1} {"b": 2}]', encoding="utf-8")
    with pytest.raises(DomainError, match="expected ',' or ']' after entry 1"):
        ga.scan(p, cfg)
    p.write_text("", encoding="utf-8")
    with pytest.raises(DomainError, match="MyActivity.json is empty"):
        ga.scan(p, cfg)


def test_invalid_utf8_is_refused(tmp_path, cfg):
    p = tmp_path / "MyActivity.html"
    p.write_bytes(b"<html>\xff\xfe</html>")
    with pytest.raises(DomainError, match="MyActivity.html is not valid UTF-8") as e:
        ga.scan(p, cfg)
    _one_line(e)


def test_an_html_page_without_entries_is_refused(tmp_path, cfg):
    p = tmp_path / "MyActivity.html"
    p.write_text("<html><body><p>Your data</p></body></html>", encoding="utf-8")
    with pytest.raises(DomainError, match="holds no activity entries \\(no 'outer-cell' "
                                          "blocks\\)") as e:
        ga.scan(p, cfg)
    _one_line(e)


def test_a_bad_file_beside_good_ones_is_named_not_fatal(tmp_path, cfg):
    root = tmp_path / "export"
    activity(root, "Search", [search("x", "2026-10-01T15:00:00Z")])
    activity(root, "Maps", {"not": "a list"})
    scan = ga.scan(root, cfg)
    assert len(scan["records"]) == 1
    assert scan["unmapped"] == [{
        "what": "Takeout/My Activity/Maps/MyActivity.json — starts with a JSON object, not "
                "a list of activity entries — it is not a My Activity or YouTube history "
                "export", "count": 1}]


def test_a_zip_without_activity_lists_what_it_has(tmp_path, cfg):
    z = tmp_path / "takeout-20261021T190000Z-001.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Google Photos/IMG_1.jpg", b"\xff\xd8")
        zf.writestr("Takeout/Meine Aktivitäten/Suche/MeineAktivitäten.html", "<html/>")
    with pytest.raises(DomainError, match="holds no My Activity file .* it has: Google Photos, "
                                          "Meine Aktivitäten. A Takeout in another language") as e:
        ga.scan(z, cfg)
    _one_line(e)


@pytest.mark.parametrize("kind", ["json", "html"])
def test_utf8_bom_exports_are_accepted(tmp_path, cfg, kind):
    if kind == "json":
        p = activity(tmp_path, "Search", [search("bom", "2026-10-01T15:00:00Z")], bom=True)
    else:
        p = write_html(tmp_path / "MyActivity.html",
                       [html_search("bom", "Oct 1, 2026, 10:00:00 AM CDT")], bom=True)
    (rec,) = ga.scan(p, cfg)["records"]
    assert rec["title"] == "bom" and rec["started_at"] == "2026-10-01T15:00:00Z"


# ---------------------------------------------------------------- determinism, targets

def test_scan_is_deterministic_for_a_zip_a_folder_and_a_file(tmp_path, cfg):
    z = sample.write_sample(tmp_path / "z", ANCHOR, "android")
    a, b = ga.scan(z, cfg), ga.scan(z, cfg)
    assert a == b and a["source_hash"] == ga.base.file_hash(z)
    unzipped = tmp_path / "unzipped"
    with zipfile.ZipFile(z) as zf:
        zf.extractall(unzipped)
    f1, f2 = ga.scan(unzipped, cfg), ga.scan(unzipped, cfg)
    assert f1["records"] == a["records"] and f1["source_hash"] == f2["source_hash"]
    assert f1["source_hash"] and f1["source_hash"] != a["source_hash"]
    assert f1["target_kind"] == "folder" and a["target_kind"] == "zip"
    one = unzipped / "Takeout" / "My Activity" / "Search" / "MyActivity.json"
    single = ga.scan(one, cfg)
    assert single["target_kind"] == "file" and single["source_hash"] == ga.base.file_hash(one)
    assert {r["external_id"] for r in single["records"]} < {r["external_id"]
                                                             for r in a["records"]}


def test_report_names_each_stream_its_span_and_what_was_read(tmp_path, cfg, conn):
    scan = ga.scan(sample.write_sample(tmp_path, ANCHOR, "iphone"), cfg)
    text = "\n".join(ga.report_lines(scan, obs.build_plan(conn, scan)))
    assert ("google activity: takeout-20261021T190000Z-001.zip (Google Takeout, HTML; "
            "6 file(s) read)") in text
    assert "web_search: 19 new, 0 changed | 2022-09-26..2026-10-21" in text
    assert "video_watch: 6 new, 0 changed | 2022-09-27..2026-10-16" in text
    assert 'e.g. 2026-10-21 Search: "climbing gym austin new"' in text
    assert "activity spans 2022-09-26..2026-10-21" in text
    assert "times: HTML dates read in the zone each one names (CDT 25)" in text
    _apply(conn, scan)
    again = "\n".join(ga.report_lines(scan, obs.build_plan(conn, scan)))
    assert "nothing new — the export matches the database" in again
    assert "25 identical (skipped)" in again


# ---------------------------------------------------------------- default target

def _downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    d = tmp_path / "Downloads"
    d.mkdir(exist_ok=True)
    return d


def test_default_target_is_the_newest_takeout_with_activity(tmp_path, cfg, monkeypatch):
    d = _downloads(tmp_path, monkeypatch)
    with pytest.raises(DomainError, match="no Google Takeout with My Activity .* looked for "
                                          "takeout-\\*.zip in"):
        ga.default_target(cfg)
    older = sample.write_sample(tmp_path / "x", ANCHOR, "android")
    older = older.rename(d / "takeout-20260901T100000Z-001.zip")
    newer = sample.write_sample(tmp_path / "y", ANCHOR, "iphone")
    newer = newer.rename(d / "takeout-20261001T100000Z-001.zip")
    photos_only = d / "takeout-20261015T100000Z-002.zip"
    with zipfile.ZipFile(photos_only, "w") as zf:
        zf.writestr("Takeout/Google Photos/IMG_1.jpg", b"\xff\xd8")
    (d / "takeout-20261016T100000Z-003.zip").write_bytes(b"not a zip")
    for i, p in enumerate((older, newer, photos_only, d / "takeout-20261016T100000Z-003.zip")):
        os.utime(p, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
    assert ga.default_target(cfg) == newer


def test_default_target_is_the_configured_path(tmp_path, cfg):
    z = sample.write_sample(tmp_path, ANCHOR, "android")
    cfg.connectors = {"google_activity": {"path": str(z)}}
    assert ga.default_target(cfg) == z
    cfg.connectors = {"google_activity": {"path": "nope/takeout.zip"}}
    with pytest.raises(DomainError, match="\\[connectors.google_activity\\] path does not "
                                          "exist"):
        ga.default_target(cfg)
