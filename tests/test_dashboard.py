"""Dashboards-as-code stay honest: every chart query in dashboards.yml is
executed against a seeded current-schema database on every test run, in each
form the plugin can send it — no filter set, both date filters set, one set —
and its SELECT aliases are checked against every display property that
references them. A schema change that would silently blank a chart fails
here instead of the evening someone opens the dashboard.

The seed is the demo persona's July in miniature, covering every tier a card
reads: journal entries with mentions and an open loop, health records, and one
fake scan per observed-tier stream applied through domain/observations exactly
as a connector's scan is. What the browser then does with the rows —
dashboards.js reads rows[0] of a metric or a table, Leaflet needs a number for
every coordinate, Vega-Lite needs a date string on a temporal axis — is
asserted here at the SQL level, and end-to-end through an in-process Datasette
in test_dashboards_served.py."""

import re
from datetime import date

import pytest
import yaml
from conftest import cli_project

from lifecrm.config import PROJECT_ROOT
from lifecrm.connectors import base
from lifecrm.demo import persona
from lifecrm.domain import (entities, entries, health, measurements,
                            observations as obs)
from lifecrm.domain.util import canonical_json

DASH_FILE = PROJECT_ROOT / "dashboards" / "dashboards.yml"

# The libraries datasette-dashboards 0.8.x actually dispatches: dashboards.js
# renders vega, vega-lite, metric, table and map; the template renders
# markdown server-side. Anything else (including the bar/line/pie shorthands
# in the plugin's master README) renders as a silent empty card — that
# regression happened once.
_SUPPORTED_LIBRARIES = {"vega", "vega-lite", "metric", "table", "map", "markdown"}

# What the Vega-Lite specs here may use. Vega-Lite drops an unknown channel or
# property with a console warning and draws the rest, so a typo would not
# fail anywhere a test can see — except against this list.
_MARKS = {"bar", "line", "arc", "rect", "point", "area"}
_CHANNELS = {"x", "x2", "y", "y2", "theta", "color", "opacity", "tooltip"}
_CHANNEL_KEYS = {"field", "type", "timeUnit", "title", "scale", "sort", "stack"}
_TYPES = {"quantitative", "temporal", "nominal", "ordinal"}
_UTC_TIME_UNITS = {"utcyearmonthdate", "utcyearmonth", "utcyear"}
_SORT_OPS = {"sum", "count", "min", "max", "mean"}

# Cards that describe the database as it is now rather than a period, and so
# deliberately ignore the date filters. Every other chart must honour both.
_AS_OF_NOW = {"journal.open-loops-count", "journal.open-loops",
              "health.latest-weight", "health.last-night-sleep",
              "observed.unresolved-handles", "observed.latest-imports"}

FILTERED = {"date_start": "2026-07-01", "date_end": "2026-07-31"}
# The filter form submits every box, so a box left empty arrives as ''.
FORMS = {"unfiltered": {}, "filtered": FILTERED,
         "from only": {"date_start": FILTERED["date_start"]},
         "blank from": {"date_start": "", "date_end": FILTERED["date_end"]}}


# ---------------------------------------------------------------- the file

def _dashboards() -> dict:
    cfg = yaml.safe_load(DASH_FILE.read_text(encoding="utf-8"))
    return cfg["plugins"]["datasette-dashboards"]


def _charts():
    """(key 'dashboard.chart', chart) for every chart in the file."""
    for dash_name, dash in _dashboards().items():
        for chart_name, chart in dash["charts"].items():
            yield f"{dash_name}.{chart_name}", chart


def _chart(key: str) -> dict:
    return dict(_charts())[key]


# The plugin's own patterns (datasette_dashboards/__init__.py, 0.8.0). Note
# there is no DOTALL: a block that spans a line is never matched, and its
# brackets reach SQLite as a syntax error.
_OPT_BLOCK = re.compile(r"(?P<opt>\[\[.*?\]\])")
_OPT_VAR = re.compile(r"\:(?P<var>[a-zA-Z0-9_]+)")


def render(query: str, options: dict) -> str:
    """The query exactly as the plugin sends it: an optional block is kept,
    brackets stripped, when its parameter is set and non-empty; removed
    otherwise. test_local_block_substitution_matches_the_plugin keeps this
    honest against the installed plugin."""
    out = query
    for m in _OPT_BLOCK.finditer(query):
        block = m.group("opt")
        var = _OPT_VAR.search(block)
        keep = var is not None and options.get(var.group("var"), "") != ""
        out = out.replace(block, block.strip("[]") if keep else "")
    return out


def run(conn, chart: dict, options: dict) -> tuple[list[str], list[dict]]:
    cur = conn.execute(render(chart["query"], options), options)
    cols = [d[0] for d in cur.description]
    return cols, [dict(zip(cols, row)) for row in cur.fetchall()]


def _encodings(chart: dict) -> list[tuple[str, dict]]:
    """(channel, definition) pairs; a tooltip list contributes one per item."""
    out = []
    for channel, enc in ((chart.get("display") or {}).get("encoding") or {}).items():
        for e in enc if isinstance(enc, list) else [enc]:
            out.append((channel, e))
    return out


def _display_fields(chart: dict) -> set:
    display = chart.get("display") or {}
    if chart["library"] == "metric":
        return {display["field"]}
    if chart["library"] == "map":
        return {display.get("latitude_column", "latitude"),
                display.get("longitude_column", "longitude")}
    if chart["library"] == "vega-lite":
        fields = set()
        for _, enc in _encodings(chart):
            if isinstance(enc, dict) and "field" in enc:
                fields.add(enc["field"])
            sort = enc.get("sort") if isinstance(enc, dict) else None
            if isinstance(sort, dict) and "field" in sort:
                fields.add(sort["field"])
        return fields
    return set()


# ---------------------------------------------------------------- the seed
#
# The persona's people, numbers and places, a month of their life, every
# stream a card reads. Owner handles appear on every message, exactly as the
# texts connectors emit them, and are linked to the owner's own entity — the
# case the people cards must leave out.

ME, JAKE, MAYA, MOM, PRIYA, SAM = (persona.OWNER, *(persona.person(k) for k in
                                   ("jake", "maya", "mom", "priya", "sam")))
ME_PH, ME_EM = ("phone", ME.phone), ("email", ME.email)
JAKE_PH, JAKE_EM = ("phone", JAKE.phone), ("email", JAKE.email)
MAYA_PH, MOM_PH = ("phone", MAYA.phone), ("phone", MOM.phone)
PRIYA_PH, SAM_PH = ("phone", PRIYA.phone), ("phone", SAM.phone)
ROSA = ("app_account", "whatsapp:Tía Rosa")
# The newer messaging apps and email: handles as their connectors emit them.
ME_IG, MAYA_IG = ("app_account", "instagram:alex.rivera"), ("app_account",
                                                             "instagram:maya.climbs")
ME_FB, MOM_FB = ("app_account", "messenger:Alex Rivera"), ("app_account",
                                                           "messenger:Linda Rivera")
ME_TG, SAM_TG = ("app_account", "telegram:user100"), ("app_account",
                                                      "telegram:user555")
ME_LI, PRIYA_LI = ("app_account", "linkedin:in/alex-rivera-pm"), ("app_account",
                                                                  "linkedin:in/priya-n")
PRIYA_EM = ("email", PRIYA.email)
OWNER_HANDLES = (ME_PH, ME_EM, ME_IG, ME_FB, ME_TG, ME_LI)
_DISPLAY = {ME_PH: "you (this phone)", JAKE_PH: JAKE.name, JAKE_EM: JAKE.name,
            MAYA_PH: MAYA.name, MOM_PH: "Mom", PRIYA_PH: PRIYA.name,
            SAM_PH: SAM.name, ROSA: "Tía Rosa", MAYA_IG: MAYA.name,
            MOM_FB: MOM.name, SAM_TG: SAM.name, PRIYA_LI: PRIYA.name,
            PRIYA_EM: PRIYA.name, ME_IG: ME.name, ME_FB: ME.name, ME_TG: ME.name,
            ME_LI: ME.name}
BLED = persona.place("bled")
VERACRUZ = persona.place("veracruz")
BLED_DAY = persona.bled_trip_days(date(2026, 7, 27))[1].isoformat()


def _obs(stream: str, ext: str, started_at: str, *, day: str | None = None,
         who=(), detail: dict | None = None, **fields) -> dict:
    """One observation as a connector emits it; `who` is ((kind, value), role)."""
    return {"stream": stream, "external_id": ext, "started_at": started_at,
            "journal_date": day or started_at[:10],
            "value_json": canonical_json(detail) if detail else None,
            "participants": [{"kind": h[0], "value": h[1], "role": role}
                             for h, role in who],
            **fields}


def _import(conn, connector: str, records: list[dict],
            account: str | None = None) -> str:
    named = {(p["kind"], p["value"]) for r in records for p in r["participants"]}
    scan = base.empty_scan(
        connector, account=account, source_file=f"{connector}-export",
        records=records,
        handles=[{"kind": k, "value": v, "display_name": _DISPLAY.get((k, v))}
                 for k, v in sorted(named)])
    return obs.apply_plan(conn, scan, obs.build_plan(conn, scan))["import_uuid"]


def _in(h):
    return ((h, "sender"), (ME_PH, "recipient"))


def _out(*hs):
    return ((ME_PH, "sender"), *((h, "recipient") for h in hs))


def _seed_observed(conn, tag: str = "a") -> list[str]:
    """One import per connector, every stream a card reads. Returns the
    import uuids."""
    t = tag
    return [
        _import(conn, "ios_messages", [
            _obs("imessage", f"{t}1", "2026-06-15T23:00:00Z", direction="in",
                 who=_in(JAKE_PH), body="Veracruz tonight?"),
            _obs("imessage", f"{t}2", "2026-07-20T23:02:00Z", direction="in",
                 who=_in(JAKE_PH), body="Veracruz tonight? you know it's my favorite"),
            _obs("imessage", f"{t}3", "2026-07-20T23:05:00Z", direction="out",
                 who=_out(JAKE_PH), body="always. 7:30?"),
            _obs("imessage", f"{t}4", "2026-07-26T17:15:00Z", direction="in",
                 who=_in(JAKE_PH), body="Veracruz at 7?"),
            _obs("imessage", f"{t}5", "2026-07-26T17:16:00Z", direction="out",
                 who=_out(JAKE_PH), body="see you there"),
            _obs("text_reaction", f"{t}6", "2026-07-26T17:17:00Z", direction="in",
                 who=_in(JAKE_PH), detail={"reaction": "love", "target": f"{t}5"}),
            _obs("sms", f"{t}7", "2026-07-21T22:20:00Z", direction="in",
                 who=_in(MOM_PH), body="Knee is healing nicely. When are you visiting?"),
            _obs("sms", f"{t}8", "2026-07-21T22:30:00Z", direction="out",
                 who=_out(MOM_PH), body="Maybe next month, Mom"),
            _obs("rcs", f"{t}9", "2026-07-22T14:02:00Z", direction="in",
                 who=_in(PRIYA_PH), body="Lead with the customer story, not the TAM slide."),
        ], account=ME.phone),
        _import(conn, "android_sms", [
            _obs("android_sms", f"{t}10", "2026-07-23T13:10:00Z", direction="in",
                 who=_in(MAYA_PH), body="Climbing tonight?"),
            _obs("android_sms", f"{t}11", "2026-07-23T13:12:00Z", direction="out",
                 who=_out(MAYA_PH), body="yes! 6pm at Movement"),
            _obs("android_mms", f"{t}12", "2026-07-24T19:00:00Z", direction="out",
                 who=_out(JAKE_PH, MAYA_PH, SAM_PH), title="Bled crew",
                 body="Who's up for Lake Bled next summer?"),
            _obs("android_call", f"{t}13", "2026-07-21T23:00:00Z", direction="out",
                 who=_out(MOM_PH), value=1320, unit="s", detail={"call": "out"}),
            _obs("android_call", f"{t}14", "2026-07-24T22:45:00Z", direction="in",
                 who=_in(PRIYA_PH), value=0, unit="s", detail={"call": "missed"}),
        ], account=ME.phone),
        _import(conn, "whatsapp", [
            _obs("whatsapp", f"{t}15", "2026-07-25T00:00:00Z", day="2026-07-24",
                 direction="in", who=(*_in(MOM_PH), (ROSA, "recipient")),
                 title="Rivera Family", body="Family dinner at Tía Rosa's on Sunday?"),
            _obs("whatsapp", f"{t}16", "2026-07-25T00:04:00Z", day="2026-07-24",
                 direction="in", who=((ROSA, "sender"), (ME_PH, "recipient"),
                                      (MOM_PH, "recipient")),
                 title="Rivera Family", body="Sí! Bring Maya"),
            _obs("whatsapp", f"{t}17", "2026-07-25T00:10:00Z", day="2026-07-24",
                 direction="out", who=_out(MOM_PH, ROSA), title="Rivera Family",
                 body="We'll be there"),
        ], account=ME.phone),
        # a call log names only the other number, as a plain participant
        _import(conn, "ios_calls", [
            _obs("ios_call", f"{t}18", "2026-07-20T20:30:00Z", direction="in",
                 who=((JAKE_PH, "participant"),), value=300, unit="s",
                 detail={"call": "in"}),
        ], account=ME.phone),
        _import(conn, "ai_chat", [
            _obs("claude_chat", f"{t}19", "2026-07-16T19:30:00Z",
                 title="Return trip to Lake Bled", body="Sketch a 5-day itinerary",
                 detail={"conversation_id": f"{t}-bled", "turn": 0, "role": "human"}),
            _obs("claude_chat", f"{t}20", "2026-07-16T19:31:00Z",
                 title="Return trip to Lake Bled", body="Here's a relaxed 5-day plan.",
                 detail={"conversation_id": f"{t}-bled", "turn": 1,
                         "role": "assistant"}),
            _obs("chatgpt_chat", f"{t}21", "2026-07-10T15:10:00Z",
                 title="High-protein meal prep", body="A one-week meal prep plan",
                 detail={"conversation_id": f"{t}-meal", "turn": 0, "role": "user"}),
        ], account="claude-account"),
        _import(conn, "calendar", [
            _obs("calendar_event", f"{t}22", "2026-07-21T00:30:00Z", day="2026-07-20",
                 ended_at="2026-07-21T02:00:00Z", title="Dinner w/ Jake @ Veracruz",
                 who=((ME_EM, "organizer"), (JAKE_EM, "participant")),
                 detail={"calendar": "Personal", "all_day": False}),
        ], account=ME.email),
        _import(conn, "finance", [
            _obs("txn", f"{t}23", "2026-07-20T12:00:00Z", direction="out",
                 title="VERACRUZ ALL NATURAL", value=-34.5, unit="usd"),
            _obs("txn", f"{t}24", "2026-07-23T12:00:00Z", direction="out",
                 title="H-E-B #742 AUSTIN TX", value=-64.12, unit="usd"),
            _obs("txn", f"{t}25", "2026-07-24T12:00:00Z", direction="in",
                 title="ACME ROBOTICS PAYROLL", value=4200.0, unit="usd"),
        ], account="checking-1234"),
        _import(conn, "photos", [
            _obs("photo", f"{t}26", "2026-07-21T00:48:00Z", day="2026-07-20",
                 title="veracruz_tacos.jpg",
                 detail={"kind": "photo", "lat": VERACRUZ.lat, "lon": VERACRUZ.lon}),
            _obs("photo", f"{t}27", f"{BLED_DAY}T08:12:00Z", title="bled_lake_island.jpg",
                 detail={"kind": "photo", "lat": BLED.lat, "lon": BLED.lon}),
            _obs("photo", f"{t}28", "2026-07-22T15:00:00Z", title="Screenshot.png",
                 detail={"kind": "screenshot"}),
        ]),
        _import(conn, "notes", [
            _obs("note", f"{t}29", "2026-07-02T15:00:00Z", title="Gift ideas for Maya",
                 body="- ceramics class voucher (Austin Clay Studio)"),
        ]),
        # The newer sources (docs/meta_messages.md, telegram.md, linkedin.md,
        # email.md, fitness.md, spotify.md, google_activity.md, nutrition.md).
        _import(conn, "meta_messages", [
            _obs("instagram_dm", f"{t}30", "2026-07-25T18:00:00Z", direction="in",
                 who=((MAYA_IG, "sender"), (ME_IG, "recipient")), title=MAYA.name,
                 body="Bouldering Saturday?"),
            _obs("messenger", f"{t}31", "2026-07-25T19:00:00Z", direction="out",
                 who=((ME_FB, "sender"), (MOM_FB, "recipient")), title=MOM.name,
                 body="Call you Sunday"),
        ], account=ME_IG[1]),
        _import(conn, "telegram", [
            _obs("telegram", f"{t}32", "2026-07-26T09:00:00Z", direction="in",
                 who=((SAM_TG, "sender"), (ME_TG, "recipient")),
                 title="Saturday Climbers", body="Gym at 9?"),
        ], account=ME_TG[1]),
        _import(conn, "linkedin", [
            _obs("linkedin_message", f"{t}33", "2026-06-20T14:00:00Z", direction="in",
                 who=((PRIYA_LI, "sender"), (ME_LI, "recipient")), title=PRIYA.name,
                 body="Congrats on the launch"),
        ], account=ME_LI[1]),
        _import(conn, "email", [
            _obs("email", f"{t}34", "2026-07-21T15:00:00Z", direction="in",
                 who=((JAKE_EM, "sender"), (ME_EM, "recipient")), title="Bled photos"),
            _obs("email", f"{t}35", "2026-07-22T16:00:00Z", direction="out",
                 who=((ME_EM, "sender"), (JAKE_EM, "recipient")),
                 title="Re: Bled photos"),
            # Jake only copied on Priya's mail: not contact with Jake
            _obs("email", f"{t}36", "2026-07-23T13:00:00Z", direction="in",
                 who=((PRIYA_EM, "sender"), (ME_EM, "recipient"),
                      (JAKE_EM, "recipient")), title="Offsite dates"),
            _obs("email", f"{t}37", "2026-07-24T08:00:00Z", direction="self",
                 who=((ME_EM, "sender"), (ME_EM, "recipient")), title="Passport scan"),
        ], account=ME.email),
        _import(conn, "fitness", [
            _obs("workout", f"{t}38", "2026-07-22T12:00:00Z", title="Push Day",
                 value=60.0, unit="min", origin_app="strong"),
            _obs("workout", f"{t}39", "2026-07-24T12:30:00Z", title="Zilker Park 5k",
                 value=30.0, unit="min", origin_app="strava"),
            _obs("workout", f"{t}40", "2026-06-10T12:00:00Z", title="Leg Day",
                 value=45.0, unit="min", origin_app="strong"),
        ]),
        _import(conn, "spotify", [
            _obs("music_play", f"{t}41", "2026-07-20T08:00:00Z", title="Be Sweet",
                 body="Japanese Breakfast", value=30.5, unit="min",
                 origin_app="spotify", detail={"plays": 3, "kind": "music"}),
            _obs("music_play", f"{t}42", "2026-07-21T08:00:00Z",
                 title="Slovenia's Lakes", body="The Slow Traveler", value=44.0,
                 unit="min", origin_app="spotify", detail={"plays": 1, "kind": "podcast"}),
        ]),
        _import(conn, "google_activity", [
            _obs("web_search", f"{t}43", "2026-07-20T17:00:00Z",
                 title="veracruz tacos hours", origin_app="google_search"),
            _obs("web_search", f"{t}44", "2026-07-27T17:00:00Z",
                 title="bled castle tickets", origin_app="youtube"),
        ]),
        _import(conn, "nutrition", [
            _obs("nutrition_day", f"{t}45", "2026-07-20T17:00:00Z",
                 title="Nutrition 2026-07-20", value=2400.0, unit="kcal",
                 detail={"nutrients": {"energy": [2400.0, "kcal"],
                                       "protein": [150.0, "g"]}}),
            _obs("food_log", f"{t}46", "2026-07-21T12:00:00Z", title="Turkey Chili",
                 value=600.0, unit="kcal",
                 detail={"nutrients": {"energy": [600.0, "kcal"], "protein": [40.0, "g"]}}),
            _obs("food_log", f"{t}47", "2026-07-21T19:00:00Z", title="Salmon",
                 value=900.0, unit="kcal",
                 detail={"nutrients": {"energy": [900.0, "kcal"], "protein": [55.0, "g"]}}),
            # a day with a daily summary: its foods are never added again
            _obs("food_log", f"{t}48", "2026-07-20T12:00:00Z", title="Oatmeal",
                 value=800.0, unit="kcal",
                 detail={"nutrients": {"energy": [800.0, "kcal"], "protein": [30.0, "g"]}}),
        ]),
    ]


def _entry(conn, day, kind, title, category, *mentions, due_at=None) -> int:
    e = entries.create(conn, journal_date=day, kind=kind, title=title,
                       primary_category=category, due_at=due_at)
    for entity_id, role in mentions:
        entries.attach_entity(conn, e["id"], entity_id, role=role)
    return e["id"]


def _health(conn, metric, day, value, unit, n, source_file="hc.zip") -> None:
    health.insert(conn, {"metric": metric, "journal_date": day,
                         "started_at": f"{day}T06:00:00Z", "value": value,
                         "unit": unit, "external_id": f"{metric}-{day}-{n}",
                         "source_file": source_file})


def _seed(conn) -> dict:
    """Every tier, every stream; returns the entity ids by persona key."""
    ids = {p.key: entities.create(conn, "person", p.name)["id"]
           for p in (ME, JAKE, MAYA, MOM)}
    veracruz = entities.create(conn, "place", VERACRUZ.name)["id"]
    _seed_observed(conn)
    for handle, key in ((JAKE_PH, "jake"), (JAKE_EM, "jake"), (MAYA_PH, "maya"),
                        (MOM_PH, "mom"), (MAYA_IG, "maya"), (MOM_FB, "mom"),
                        *((h, "me") for h in OWNER_HANDLES)):
        obs.link_handle(conn, *handle, ids[key])

    _entry(conn, "2026-06-10", "event", "Bouldering with Maya at Movement",
           "exercise", (ids["maya"], "participant"))
    _entry(conn, "2026-07-20", "event", "Fish tacos at Veracruz with Jake", "food",
           (ids["jake"], "participant"), (veracruz, "location"))
    _entry(conn, "2026-07-21", "event", "Call with Mom about her knee recovery",
           "social", (ids["mom"], "participant"))
    _entry(conn, "2026-07-24", "reflection", "Reminiscing about Lake Bled with Jake",
           "travel", (ids["jake"], "participant"), (ids["jake"], "subject"))
    _entry(conn, "2026-07-27", "task", "Book flights to Ljubljana", "travel",
           due_at="2026-08-01")
    measurements.add(conn, journal_date="2026-07-27", metric="mood", numeric_value=7)
    measurements.add(conn, journal_date="2026-07-27", metric="energy", numeric_value=6)

    _health(conn, "sleep_session", "2026-07-26", 450.0, "min", 1)
    _health(conn, "sleep_session", "2026-07-27", 300.0, "min", 1)
    _health(conn, "sleep_session", "2026-07-27", 120.0, "min", 2)
    _health(conn, "weight", "2026-07-26", 78.6, "kg", 1)
    _health(conn, "weight", "2026-07-27", 78.4, "kg", 1)
    _health(conn, "steps", "2026-07-27", 8400.0, "count", 1)
    _health(conn, "heart_rate", "2026-07-27", 61.0, "bpm", 1)
    _health(conn, "nutrition", "2026-07-27", 2300.0, "kcal", 1)
    _health(conn, "exercise_session", "2026-07-24", 60.0, "min", 1)
    conn.commit()
    return ids


def _places(map_rows: list[dict]) -> list[str]:
    """Persona places the map's pins sit on (pins are rounded to ~100 m)."""
    return sorted(p.name for p in persona.PLACES for r in map_rows
                  if abs(r["latitude"] - p.lat) < 1e-3
                  and abs(r["longitude"] - p.lon) < 1e-3)


def _all_results(conn) -> dict:
    return {(key, form): run(conn, chart, options)
            for key, chart in _charts() for form, options in FORMS.items()}


# ---------------------------------------------------------------- the file itself

def test_dashboards_file_is_plain_ascii():
    # `datasette serve --metadata` opens the file with the locale's encoding;
    # on Windows without PYTHONUTF8 that is cp1252, which turns an em dash in
    # a description into "â€”" and rejects some other UTF-8 bytes outright.
    text = DASH_FILE.read_bytes()
    bad = sorted({line for line in text.decode("utf-8").splitlines()
                  if not line.isascii()})
    assert not bad, f"non-ASCII lines in dashboards.yml: {bad[:3]}"


def test_every_library_is_one_the_pinned_renderer_dispatches():
    for key, chart in _charts():
        assert chart["library"] in _SUPPORTED_LIBRARIES, (
            f"{key}: library '{chart['library']}' is not dispatched by the "
            f"pinned datasette-dashboards renderer")
        assert chart["db"] == "lifecrm", f"{key}: db must be the snapshot's stem"


def test_vega_lite_specs_use_known_marks_channels_and_utc_dates():
    for key, chart in _charts():
        if chart["library"] != "vega-lite":
            continue
        display = chart["display"]
        mark = display["mark"]
        assert (mark["type"] if isinstance(mark, dict) else mark) in _MARKS, key
        for channel, enc in _encodings(chart):
            assert channel in _CHANNELS, f"{key}: unknown channel '{channel}'"
            assert set(enc) <= _CHANNEL_KEYS, (
                f"{key}.{channel}: unknown properties {set(enc) - _CHANNEL_KEYS}")
            if channel in ("x2", "y2"):
                continue                      # shares the primary channel's scale
            assert enc.get("type") in _TYPES, f"{key}.{channel}: type {enc.get('type')}"
            if enc["type"] == "temporal":
                # A 'YYYY-MM-DD' string parses as UTC midnight; anything shown
                # in local time reads as the day before west of Greenwich.
                utc = (enc.get("timeUnit") in _UTC_TIME_UNITS
                       or (enc.get("scale") or {}).get("type") == "utc")
                assert utc, f"{key}.{channel}: a date axis needs a utc timeUnit or scale"
            sort = enc.get("sort")
            if isinstance(sort, dict):
                assert sort.get("op") in _SORT_OPS, f"{key}.{channel}: sort op {sort}"
                assert sort.get("order") in ("ascending", "descending"), key


def test_layouts_are_consistent():
    for dash_name, dash in _dashboards().items():
        charts = set(dash["charts"])
        layout = dash["layout"]
        widths = {len(row) for row in layout}
        assert len(widths) == 1, f"{dash_name}: rows have differing widths"
        placed = {cell for row in layout for cell in row if cell != "."}
        assert placed <= charts, (
            f"{dash_name}: layout references unknown charts {placed - charts}")
        assert charts <= placed, (
            f"{dash_name}: charts missing from layout {charts - placed}")


def test_every_dashboard_offers_the_date_filters():
    for dash_name, dash in _dashboards().items():
        assert {k: v["type"] for k, v in dash["filters"].items()} == {
            "date_start": "date", "date_end": "date"}, dash_name


def test_every_dated_chart_honours_both_date_filters():
    for key, chart in _charts():
        blocks = " ".join(_OPT_BLOCK.findall(chart["query"]))
        if key in _AS_OF_NOW:
            assert not blocks, f"{key} is declared as-of-now but has filter blocks"
            continue
        for param in (":date_start", ":date_end"):
            assert param in blocks, f"{key} ignores {param}"


def test_optional_blocks_sit_on_one_line_with_one_parameter():
    for key, chart in _charts():
        query = chart["query"]
        assert "\n" not in query, (
            f"{key}: the query kept a line break (a more-indented line in the "
            f"folded scalar) — the plugin only matches [[ ]] on one line")
        assert "--" not in query, f"{key}: a -- comment would swallow the rest"
        assert query.count("[[") == query.count("]]") == len(
            _OPT_BLOCK.findall(query)), f"{key}: unbalanced optional block"
        for block in _OPT_BLOCK.findall(query):
            assert len(_OPT_VAR.findall(block)) == 1, f"{key}: {block}"


# ---------------------------------------------------------------- the queries

def test_every_chart_query_runs_in_every_filter_form_and_matches_its_display(conn):
    _seed(conn)
    for key, chart in _charts():
        if chart["library"] == "markdown":
            continue
        for form, options in FORMS.items():
            cols, rows = run(conn, chart, options)        # raises on schema drift
            missing = _display_fields(chart) - set(cols)
            assert not missing, (
                f"{key} ({form}): display refers to {sorted(missing)} but the "
                f"query returns {sorted(cols)}")
            assert len(rows) <= 1000, f"{key} ({form}): would be truncated"
            if chart["library"] == "vega-lite":
                for _, enc in _encodings(chart):
                    values = [r[enc["field"]] for r in rows]
                    # x2 carries no type of its own: it shares x's date scale
                    if enc.get("type", "temporal") == "temporal":
                        assert all(isinstance(v, str) and re.fullmatch(
                            r"\d{4}-\d{2}-\d{2}", v) for v in values), (
                            f"{key}.{enc['field']} ({form}): not dates: {values[:3]}")
                    elif enc["type"] == "quantitative":
                        assert all(isinstance(v, (int, float)) for v in values), (
                            f"{key}.{enc['field']} ({form}): not numbers: {values[:3]}")
            if chart["library"] == "map":
                for r in rows:
                    assert isinstance(r["latitude"], float) and isinstance(
                        r["longitude"], float), f"{key}: {r}"


def test_metrics_and_tables_return_one_displayable_row_on_an_empty_database(conn):
    for key, chart in _charts():
        if chart["library"] not in ("metric", "table"):
            continue
        for form, options in FORMS.items():
            _, rows = run(conn, chart, options)
            assert len(rows) == 1, f"{key} ({form}): {len(rows)} rows on an empty db"
            shown = ([rows[0][chart["display"]["field"]]]
                     if chart["library"] == "metric" else list(rows[0].values()))
            assert None not in shown, f"{key} ({form}): renders 'null': {rows[0]}"


def test_the_three_empty_state_cards_say_so_instead_of_going_blank(conn):
    assert run(conn, _chart("health.latest-weight"), {})[1] == [{"kg": "no data"}]
    assert run(conn, _chart("health.last-night-sleep"), {})[1] == [{"hours": "no data"}]
    assert run(conn, _chart("journal.open-loops"), {})[1] == [
        {"kind": "", "title": "No open loops", "since": "", "due": ""}]
    _seed(conn)
    assert run(conn, _chart("health.latest-weight"), {})[1] == [{"kg": "78.4 kg"}]
    assert run(conn, _chart("health.last-night-sleep"), {})[1] == [{"hours": "7.0 h"}]
    assert run(conn, _chart("journal.open-loops"), {})[1] == [
        {"kind": "task", "title": "Book flights to Ljubljana", "since": "2026-07-27",
         "due": "2026-08-01"}]


def test_seeded_rows_come_back_on_every_observed_card(conn):
    _seed(conn)

    def rows(name, options=None):
        return run(conn, _chart(f"observed.{name}"), options or {})[1]

    assert rows("observed-records") == [{"records": 48}]
    assert rows("messages-count") == [{"messages": 18}]       # no reaction
    assert {r["stream"] for r in rows("texts-per-day")} == {
        "imessage", "sms", "rcs", "android_sms", "android_mms", "whatsapp",
        "instagram_dm", "messenger", "telegram", "linkedin_message"}
    assert rows("calls-per-week") == [{"week": "2026-07-20", "week_end": "2026-07-27",
                                       "calls": 3, "minutes": 27.0}]
    assert rows("ai-chats-per-week") == [
        {"week": "2026-07-13", "week_end": "2026-07-20", "vendor": "Claude",
         "chats": 1, "messages": 2},
        {"week": "2026-07-06", "week_end": "2026-07-13", "vendor": "ChatGPT",
         "chats": 1, "messages": 1}]
    assert rows("calendar-per-week") == [
        {"week": "2026-07-20", "week_end": "2026-07-27", "events": 1}]
    # money out only, as positive amounts: Veracruz + H-E-B, not the payroll
    assert rows("spending-per-week") == [
        {"week": "2026-07-20", "week_end": "2026-07-27", "currency": "USD",
         "spent": 98.62, "transactions": 2}]
    assert rows("photos-per-month") == [{"month": "2026-07-01", "photos": 2},
                                        {"month": "2022-07-01", "photos": 1}]
    assert rows("notes-per-month") == [{"month": "2026-07-01", "notes": 1}]
    # the Lake Bled trip four years back is on the map; the screenshot has no GPS
    assert _places(rows("photo-map")) == ["Lake Bled", "Veracruz All Natural"]
    assert {r["photos"] for r in rows("photo-map")} == {1}
    imports = rows("latest-imports")
    # the twelve newest of the seventeen runs, newest first
    assert [r["connector"] for r in imports] == [
        "nutrition", "google_activity", "spotify", "fitness", "email", "linkedin",
        "telegram", "meta_messages", "notes", "photos", "finance", "calendar"]
    assert all(r["status"] == "applied" and r["finished"] for r in imports)
    assert next(r for r in imports if r["connector"] == "email")["counts"] \
        == "4 added, 0 updated, 0 identical"
    # Priya (phone, LinkedIn, email), Sam (phone, Telegram) and Tia Rosa:
    # seen, not linked to anyone yet
    assert rows("unresolved-handles") == [{"handles": 6}]


def test_date_filters_narrow_the_observed_cards(conn):
    _seed(conn)

    def rows(name, options):
        return run(conn, _chart(f"observed.{name}"), options)[1]

    assert rows("messages-count", FILTERED) == [{"messages": 16}]   # not June
    assert [r["month"] for r in rows("photos-per-month", FILTERED)] == ["2026-07-01"]
    assert _places(rows("photo-map", FILTERED)) == ["Veracruz All Natural"]
    assert rows("ai-chats-per-week", {"date_start": "2026-07-13"})[0]["vendor"] == "Claude"
    assert len(rows("ai-chats-per-week", {"date_start": "2026-07-13"})) == 1
    assert rows("notes-per-month", {"date_end": "2026-07-01"}) == []


def test_top_people_resolve_through_handles_and_never_include_the_owner(conn):
    _seed(conn)
    rows = run(conn, _chart("observed.top-people"), {})[1]
    # The other party of each message: whom the owner wrote to, who wrote to
    # the owner. Jake: 5 iMessages + the group MMS; Mom: 2 SMS + 2 WhatsApp
    # + 1 Messenger; Maya: 2 SMS + the MMS + 1 Instagram DM. An unlinked
    # handle is its own row: Priya by phone and on LinkedIn, Sam by phone and
    # on Telegram.
    assert rows == [
        {"person": "Jake Morales", "handle": "linked", "messages": 6},
        {"person": "Linda Rivera", "handle": "linked", "messages": 5},
        {"person": "Maya Chen", "handle": "linked", "messages": 4},
        {"person": "Tía Rosa", "handle": "not linked yet", "messages": 2},
        {"person": "Priya Natarajan", "handle": "not linked yet", "messages": 1},
        {"person": "Priya Natarajan", "handle": "not linked yet", "messages": 1},
        {"person": "Sam Okafor", "handle": "not linked yet", "messages": 1},
        {"person": "Sam Okafor", "handle": "not linked yet", "messages": 1}]
    assert run(conn, _chart("observed.top-people"), FILTERED)[1][0]["messages"] == 5


def test_an_ignored_handle_drops_out_of_top_people(conn):
    _seed(conn)
    obs.ignore_handle(conn, *SAM_PH)
    rows = run(conn, _chart("observed.top-people"), {})[1]
    # his Telegram handle is a handle of its own, still listed until ignored too
    assert [r["person"] for r in rows].count(SAM.name) == 1
    obs.ignore_handle(conn, *SAM_TG)
    people = {r["person"] for r in run(conn, _chart("observed.top-people"), {})[1]}
    assert SAM.name not in people and "Tía Rosa" in people


def test_people_dashboard_counts_both_tiers_and_leaves_the_owner_out(conn):
    _seed(conn)

    def rows(name, options=None):
        return run(conn, _chart(f"people.{name}"), options or {})[1]

    assert rows("people-in-touch") == [{"people": 3}]          # never Alex
    # Jake twice (one entry naming him in two roles counts once), Mom, Maya
    assert rows("journal-mentions") == [{"mentions": 4}]
    assert rows("journal-mentions", FILTERED) == [{"mentions": 3}]
    # + Maya's Instagram DM, the Messenger text to Mom, and Jake's two emails
    # (the one he was only copied on is not contact)
    assert rows("observed-contacts") == [{"contacts": 19}]
    by_tier = {(r["person"], r["tier"]): r["contacts"] for r in rows("contacts-by-tier")}
    assert by_tier == {("Jake Morales", "observed"): 10, ("Jake Morales", "journal"): 2,
                       ("Linda Rivera", "observed"): 6, ("Linda Rivera", "journal"): 1,
                       ("Maya Chen", "observed"): 4, ("Maya Chen", "journal"): 1}
    assert rows("last-contact") == [
        {"person": "Jake Morales", "last_contact": "2026-07-26", "via": "imessage",
         "journal": 2, "observed": 10},
        {"person": "Linda Rivera", "last_contact": "2026-07-25", "via": "messenger",
         "journal": 1, "observed": 6},
        {"person": "Maya Chen", "last_contact": "2026-07-25", "via": "instagram_dm",
         "journal": 1, "observed": 4}]
    weeks = rows("contacts-per-week", FILTERED)
    assert {r["person"] for r in weeks} == {"Jake Morales", "Linda Rivera", "Maya Chen"}
    assert sum(r["contacts"] for r in weeks if r["person"] == "Jake Morales") == 11
    assert all(r["week_end"] > r["week"] for r in weeks)


def test_a_retracted_import_leaves_every_card_as_if_never_imported(conn):
    _seed(conn)
    before = _all_results(conn)
    # A late export touching every stream and the same people, plus a new
    # unknown number, and a health export with a heavier weigh-in — then both
    # undone. Retraction must hide them from every card, not just most.
    late = _seed_observed(conn, tag="late")
    _import(conn, "ios_messages", [
        _obs("imessage", "late-unknown", "2026-07-27T15:00:00Z", direction="in",
             who=_in(("phone", "+15125550177")), body="who is this"),
    ], account=ME.phone)
    late.append(conn.execute("SELECT uuid FROM imports ORDER BY id DESC LIMIT 1")
                .fetchone()["uuid"])
    _health(conn, "weight", "2026-07-28", 90.0, "kg", 1, source_file="late.zip")
    _health(conn, "sleep_session", "2026-07-28", 600.0, "min", 1, source_file="late.zip")
    assert _all_results(conn) != before, "the late data never reached a card"
    for import_uuid in late:
        obs.retract_import(conn, import_uuid)
    health.retract_source_file(conn, "late.zip")
    after = _all_results(conn)
    changed = sorted({key for key in before if before[key] != after[key]})
    assert not changed, f"retracted rows still show on {changed}"


@pytest.mark.parametrize("options", list(FORMS.values()), ids=list(FORMS))
def test_local_block_substitution_matches_the_plugin(options):
    plugin = pytest.importorskip("datasette_dashboards")
    for key, chart in _charts():
        assert render(chart["query"], options) == plugin.replace_opts_in_query(
            chart["query"], options), key


# ---------------------------------------------------------------- the command

def test_dashboard_command_snapshots_and_serves(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lifecrm.cli import app
    runner = CliRunner()

    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0

    calls = []
    monkeypatch.setattr("lifecrm.cli.subprocess.call",
                        lambda args: calls.append(args) or 0)
    r = runner.invoke(app, ["dashboard", "--port", "8123"])
    assert r.exit_code == 0, r.output
    assert "/-/dashboards" in r.output

    snap = root / "data" / "dashboard" / "lifecrm.db"
    assert snap.exists(), "no snapshot was written"
    (args,) = calls
    assert "-i" in args and str(snap) in args
    assert "--port" in args and "8123" in args
    meta_idx = args.index("--metadata") + 1
    assert args[meta_idx].endswith("dashboards.yml")
