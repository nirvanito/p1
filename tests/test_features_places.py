"""Places: town names from a local gazetteer, home, trips, countries — and
the questions `ask` answers with them.

The load-bearing claims: a position gets the nearest town within max_km and
nothing farther; without a gazetteer the feature says so and names nothing;
home follows where the days are; a trip is the away days, and a home day
ends it; countries carry first and last day; `ask` answers the four question
shapes with an answer that passes its own citation check; `fetch` writes the
compact file from a GeoNames zip (no network in the test); the vault gets a
Trips page; the review pack a trips section.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta

import pytest
from feature_seed import apply, rec

from lifecrm import places, recall, review
from lifecrm.domain.util import DomainError

TOWNS = [
    ("Austin", 30.2672, -97.7431, "US", "Texas", 961855),
    ("Round Rock", 30.5083, -97.6789, "US", "Texas", 133372),
    ("Bled", 46.3683, 14.1146, "SI", "Radovljica", 5100),
    ("Ljubljana", 46.0511, 14.5051, "SI", "Ljubljana", 284355),
    ("Lisbon", 38.7167, -9.1333, "PT", "Lisbon", 517802),
]
HOME = (30.2700, -97.7400)


@pytest.fixture
def gaz(cfg):
    places.write_compact(cfg.data_dir / "geo" / "places.tsv", TOWNS)
    yield
    places._CACHE.clear()


def _visit(day: str, lat: float, lon: float, title: str = "Place", pid: str | None = None,
           hhmm: str = "12:00") -> dict:
    vj = {"lat": lat, "lon": lon}
    if pid:
        vj["place_id"] = pid
    return rec("place_visit", day, hhmm, [], title=title, value=60.0, unit="min",
               value_json=vj)


def _days(start: str, n: int) -> list[str]:
    d = date.fromisoformat(start)
    return [(d + timedelta(days=i)).isoformat() for i in range(n)]


@pytest.fixture
def travels(conn, cfg, gaz):
    recs = [_visit(d, *HOME, title="Home") for d in _days("2022-04-01", 40)]
    # a trip to Slovenia: 2022-05-11 .. 2022-05-14, one day with no record inside it
    recs += [_visit("2022-05-11", 46.0510, 14.5060, "Place at 46.0510, 14.5060"),
             _visit("2022-05-12", 46.3625, 14.0938, "Lake Bled", pid="pid-bled"),
             _visit("2022-05-14", 46.0500, 14.5000, "Place at 46.05, 14.50")]
    recs += [rec("photo", "2022-05-12", f"10:{i:02d}", [], title=f"IMG_{i}.jpg",
                 value_json={"lat": 46.3680, "lon": 14.1140}) for i in range(4)]
    recs += [_visit(d, *HOME, title="Home") for d in _days("2022-05-15", 20)]
    # a day trip to Round Rock is under trip_km (30 km): not a trip
    recs += [_visit("2022-06-10", 30.5080, -97.6790, "Round Rock Donuts")]
    # Lisbon, 2024
    recs += [_visit(d, 38.7170, -9.1330, "Place") for d in _days("2024-03-02", 3)]
    recs += [_visit(d, *HOME, title="Home") for d in _days("2024-03-05", 5)]
    # far from any town: a position but no name
    recs += [_visit("2024-03-20", 0.5, -150.0, "Mid-Pacific")]
    apply(conn, recs, connector="location_fixture", source="timeline")
    conn.commit()


def test_the_nearest_town_within_max_km_and_nothing_farther(cfg, gaz):
    assert places.label_for(cfg, 46.3625, 14.0938) == "Bled, Slovenia"
    assert places.label_for(cfg, 30.30, -97.70) == "Austin, United States"
    assert places.label_for(cfg, 0.5, -150.0) is None
    assert places.label_for(cfg, 0, 0) is None and places.label_for(cfg, "x", 1) is None
    cfg.extra["places"] = {"max_km": 1}
    assert places.label_for(cfg, 46.3625, 14.0938) is None     # Bled is 2.6 km away


def test_settings_are_checked_and_without_a_gazetteer_nothing_is_named(conn, cfg):
    assert places.status(cfg)["ok"] is False
    assert "run: python -m lifecrm places fetch" in places.status(cfg)["error"]
    assert places.label_for(cfg, 46.3625, 14.0938) is None
    assert places.timeline(conn, cfg)["ok"] is False
    for bad, msg in (({"trip_km": 1}, "trip_km must be a distance"),
                     ({"home": "north"}, "home must be"),
                     ({"color": "red"}, "unknown setting")):
        cfg.extra["places"] = bad
        with pytest.raises(DomainError, match=msg):
            places.settings(cfg)
    cfg.extra["places"] = {"home": "30.27, -97.74"}
    assert places.settings(cfg)["home"] == (30.27, -97.74)


def test_country_names_and_aliases():
    assert places.country_code("Slovenia") == "SI"
    assert places.country_code("the UK") == "GB" and places.country_code("U.S.") == "US"
    assert places.country_code("Türkiye") == "TR" and places.country_code("Narnia") is None
    assert places.country_name("si") == "Slovenia"


def test_home_trips_and_countries(conn, cfg, travels):
    tl = places.timeline(conn, cfg)
    assert tl["ok"]
    assert [h["label"] for h in tl["home"]["homes"]] == ["Austin, United States"]
    trips = tl["trips"]
    assert [(t["start"], t["end"]) for t in trips] == [("2022-05-11", "2022-05-14"),
                                                        ("2024-03-02", "2024-03-04"),
                                                        ("2024-03-20", "2024-03-20")]
    slo = trips[0]
    assert [x["name"] for x in slo["towns"]] == ["Ljubljana", "Bled"]
    assert slo["countries"] == [{"cc": "SI", "name": "Slovenia"}]
    assert slo["days"] == 4 and slo["days_located"] == 3 and slo["far_km"] > 8000
    assert places.describe_trip(slo).startswith("2022-05-11 to 2022-05-14: Ljubljana, Bled "
                                                "(Slovenia) — 4 days, up to ")
    assert [c["cc"] for c in tl["countries"]] == ["US", "SI", "PT"]
    si = tl["countries"][1]
    assert (si["first_date"], si["last_date"], si["days"]) == ("2022-05-11", "2022-05-14", 3)
    assert si["towns"] == ["Ljubljana", "Bled"]


def test_a_trip_with_nothing_around_it_still_counts(conn, cfg, gaz):
    # 2022 holds only the trip's own records: home falls back to most days overall
    recs = [_visit(d, *HOME, title="Home") for d in _days("2026-09-01", 20)]
    recs += [_visit("2022-09-29", 46.0569, 14.5058), _visit("2022-09-30", 46.3625, 14.0936),
             _visit("2022-10-01", 46.3625, 14.0936), _visit("2022-10-03", 46.0569, 14.5058)]
    apply(conn, recs, connector="location_fixture", source="sparse")
    conn.commit()
    tl = places.timeline(conn, cfg)
    assert [(t["start"], t["end"]) for t in tl["trips"]] == [("2022-09-29", "2022-10-03")]
    assert tl["trips"][0]["home"] == "Austin, United States"
    # the timeline is kept until a located row changes, then worked out again
    assert places.timeline(conn, cfg) is tl
    apply(conn, [_visit("2026-10-01", 38.7170, -9.1330)], connector="location_fixture",
          source="lisbon")
    conn.commit()
    again = places.timeline(conn, cfg)
    assert again is not tl and again["trips"][-1]["start"] == "2026-10-01"


def test_a_configured_home_wins(conn, cfg, travels):
    cfg.extra["places"] = {"home": "46.05, 14.50"}           # living in Ljubljana, say
    tl = places.timeline(conn, cfg)
    assert tl["home"]["how"] == "config"
    assert tl["trips"][0]["start"] == "2022-04-01"           # Austin is now away


def _answer(conn, cfg, q: str) -> dict:
    result = recall.answer(conn, cfg, q, use_llm=False, today=date(2026, 10, 21))
    check = recall.check_citations(result["answer"], result["evidence"])
    assert not check["unknown"] and not check["unsupported"], (result["answer"], check)
    return result


def test_ask_which_countries(conn, cfg, travels):
    r = _answer(conn, cfg, "which countries have I been to?")
    m = next(x for x in r["evidence"]["metrics"] if x["metric"] == "places")
    assert m["label"] == "countries visited"
    assert "Your located records fall in 3 countries" in r["answer"]
    assert "Slovenia, 2022-05-11 to 2022-05-14" in r["answer"]


def test_ask_when_was_i_in_a_country_or_a_town(conn, cfg, travels):
    r = _answer(conn, cfg, "when was I in Slovenia?")
    assert "Your records place you in Slovenia on 3 days" in r["answer"]
    assert "most recently 2022-05-14" in r["answer"] and "Towns: Ljubljana, Bled" in r["answer"]
    r = _answer(conn, cfg, "have I been to Bled")
    assert "Your records place you in Bled on 1 day" in r["answer"]
    r = _answer(conn, cfg, "when was I last in Narnia")
    assert not any(x["metric"] == "places" for x in r["evidence"]["metrics"])


def test_ask_trips_and_where_was_i(conn, cfg, travels):
    r = _answer(conn, cfg, "what trips did I take in 2022?")
    m = next(x for x in r["evidence"]["metrics"] if x["metric"] == "places")
    assert m["label"] == "trips" and "1 trip away from home" in r["answer"]
    r = _answer(conn, cfg, "where was I on 2022-05-12?")
    assert "2022-05-12: Bled, Slovenia" in r["answer"]
    index = recall.evidence_index(r["evidence"])
    assert any(v.startswith("where you were") for v in index.values())


def test_without_the_gazetteer_ask_says_why(conn, cfg):
    r = recall.answer(conn, cfg, "which countries have I been to?", use_llm=False)
    assert any("place names (no gazetteer" in x for x in r["evidence"]["no_data"])


def test_fetch_writes_the_compact_file_from_a_geonames_zip(cfg):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        rows = ["3196359\tBled\tBled\t\t46.36833\t14.11361\tP\tPPL\tSI\t\tH5\t\t\t\t5100\t\t"
                "502\tEurope/Ljubljana\t2019-09-05",
                "1\tSomething\tSomething\t\t1\t1\tS\tHTL\tXX\t\t01\t\t\t\t0\t\t0\tUTC\t2020-01-01"]
        zf.writestr("cities1000.txt", "\n".join(rows) + "\n")
    payloads = {"https://example.test/cities1000.zip": buf.getvalue(),
                "https://example.test/admin1CodesASCII.txt":
                    "SI.H5\tRadovljica\tRadovljica\t3194351\n".encode("utf-8")}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    got = places.fetch(cfg, base="https://example.test/", opener=lambda u: Resp(payloads[u]))
    assert got["towns"] == 1
    assert places.label_for(cfg, 46.36, 14.10) == "Bled, Slovenia"
    town = places.town_at(cfg, 46.36, 14.10)
    assert town.region == "Radovljica" and town.population == 5100
    dry = places.fetch(cfg, dry_run=True)
    assert dry["downloads"][0].endswith("/cities1000.zip")
    with pytest.raises(DomainError, match="dataset must be"):
        places.fetch(cfg, dataset="everything")


def test_the_review_pack_and_the_vault(conn, cfg, travels):
    period = review.resolve_period(cfg, start="2022-05-09", end="2022-05-15",
                                   today=date(2026, 10, 21))
    pack = review.build_pack(conn, cfg, period, lens="week")
    trips = pack["trips"]
    assert trips["enabled"] and len(trips["items"]) == 1
    assert trips["items"][0]["line"].startswith("2022-05-11 to 2022-05-14: Ljubljana, Bled")
    # most located days first (3 each here), then by country code
    names = [(c["name"], c["days"]) for c in trips["countries"]]
    assert names == [("Slovenia", 3), ("United States", 3)]
    si = trips["countries"][0]
    assert si["first_on_record"] is True
    md = review.render_markdown(pack)
    assert "## Trips and countries" in md and "(first time on record)" in md
    from lifecrm import export
    notes = export.build_vault(conn, tz_name=cfg.timezone, cfg=cfg)
    assert "Trips.md" in notes
    assert "Ljubljana, Bled (Slovenia)" in notes["Trips.md"]
    day = notes["Journal/2022/2022-05-12.md"]
    assert "(near Bled, Slovenia)" in day
