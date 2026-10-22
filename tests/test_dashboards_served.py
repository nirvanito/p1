"""The dashboards as a browser gets them.

`lifecrm dashboard` is run for real up to the point where it would start the
server; the snapshot and metadata it hands to `datasette serve` are then
served by an in-process Datasette with the installed datasette-dashboards
plugin. Every dashboard page is fetched with and without the date filters,
and every chart's data is then fetched exactly as dashboards.js fetches it:
the chart JSON the page passes to renderChart() — its query already through
the plugin's own [[ ]] substitution — as `<db>.json?sql=...&<filters>&
_shape=objects`. The rows that come back are held to what each renderer
needs: a metric reads rows[0][field], a table Object.keys(rows[0]), a map a
number for every coordinate, a Vega-Lite spec the fields its encodings name.

JavaScript itself is not run (there is no browser here), so whether Vega-Lite
and Leaflet draw what the spec means stays unverified; everything up to the
renderer is verified. Skipped when Datasette is not installed — only the
dashboard command needs it."""

import asyncio
import json
import urllib.parse

import pytest
import yaml
from conftest import cli_project

pytest.importorskip("datasette")
pytest.importorskip("datasette_dashboards")

from datasette.app import Datasette  # noqa: E402

from lifecrm import db as db_mod  # noqa: E402
from test_dashboard import FILTERED, _display_fields, _places, _seed  # noqa: E402

_FORMS = {"unfiltered": "", "filtered": urllib.parse.urlencode(FILTERED),
          # what Apply sends with the From box left empty
          "blank from": f"date_start=&date_end={FILTERED['date_end']}"}


def _render_calls(html: str) -> dict:
    """{chart slug: (chart, query string)} from the page's
    renderChart('<slug>', <chart json>, '<query string>', ...) calls."""
    calls, decoder, i = {}, json.JSONDecoder(), 0
    marker = "renderChart('"
    while (i := html.find(marker, i)) != -1:
        start = i + len(marker)
        slug_end = html.index("', ", start)
        chart, end = decoder.raw_decode(html, slug_end + 3)
        assert html[end:end + 3] == ", '", html[end:end + 40]
        qs_end = html.index("'", end + 3)
        calls[html[start:slug_end]] = (chart, html[end + 3:qs_end])
        i = qs_end
    return calls


def _served(tmp_path, monkeypatch, seed: bool) -> tuple[str, dict]:
    """Run `lifecrm dashboard` up to the server start; return the snapshot
    path and the parsed metadata it would serve."""
    from typer.testing import CliRunner
    from lifecrm.cli import app
    runner = CliRunner()
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    if seed:
        conn = db_mod.connect(root / "data" / "lifecrm.db")
        try:
            _seed(conn)
        finally:
            conn.close()
    calls = []
    monkeypatch.setattr("lifecrm.cli.subprocess.call",
                        lambda args: calls.append(args) or 0)
    r = runner.invoke(app, ["dashboard"])
    assert r.exit_code == 0, r.output
    (args,) = calls
    snap = args[args.index("-i") + 1]
    metadata = args[args.index("--metadata") + 1]
    with open(metadata, encoding="utf-8") as f:
        return snap, yaml.safe_load(f)


async def _browse(snap: str, meta: dict) -> dict:
    """{(dashboard, chart, form): (chart as rendered, status, json)} for every
    chart a browser would draw, and every single-chart page."""
    ds = Datasette([], immutables=[snap], metadata=meta)
    await ds.invoke_startup()
    index = await ds.client.get("/-/dashboards")
    assert index.status_code == 200
    out = {}
    for slug, dash in meta["plugins"]["datasette-dashboards"].items():
        assert f"/-/dashboards/{slug}" in index.text, slug
        for form, qs in _FORMS.items():
            page = await ds.client.get(f"/-/dashboards/{slug}" + (f"?{qs}" if qs else ""))
            assert page.status_code == 200, (slug, form, page.text[:300])
            calls = _render_calls(page.text)
            assert set(calls) == set(dash["charts"]), slug
            for chart_slug, (chart, query_string) in calls.items():
                # the plugin builds it from a set: same filters, any order
                assert dict(urllib.parse.parse_qsl(query_string, True)) == dict(
                    urllib.parse.parse_qsl(qs, True)), (slug, chart_slug)
                if chart["library"] == "markdown":
                    continue
                # getDataUrl() in dashboards.js; quote() mirrors encodeURIComponent
                sql = urllib.parse.quote(chart["query"], safe="!~*'()")
                r = await ds.client.get(
                    f"/{chart['db']}.json?sql={sql}&{query_string}&_shape=objects")
                out[(slug, chart_slug, form)] = (chart, r.status_code, r.json())
        for chart_slug in dash["charts"]:
            page = await ds.client.get(f"/-/dashboards/{slug}/{chart_slug}")
            assert page.status_code == 200, (slug, chart_slug)
            # the single-chart page names its element after the dashboard
            ((rendered, _),) = _render_calls(page.text).values()
            assert rendered["title"] == dash["charts"][chart_slug]["title"]
    return out


def _assert_renderable(fetched: dict) -> None:
    for (slug, chart_slug, form), (chart, status, data) in fetched.items():
        key = f"{slug}.{chart_slug} ({form})"
        assert status == 200 and data.get("ok"), f"{key}: {data.get('error')}"
        assert not data["truncated"], key
        rows = data["rows"]
        missing = _display_fields(chart) - set(data["columns"])
        assert not missing, f"{key}: display refers to {sorted(missing)}"
        library = chart["library"]
        if library == "metric":
            assert rows and rows[0][chart["display"]["field"]] is not None, (
                f"{key}: the card would read 'undefined' or 'null'")
        elif library == "table":
            assert rows, f"{key}: Object.keys(rows[0]) would throw — a blank card"
        elif library == "map":
            for r in rows:
                assert all(isinstance(r[c], (int, float)) for c in
                           _display_fields(chart)), f"{key}: unplottable {r}"


def test_every_card_is_served_and_renderable_on_an_empty_database(tmp_path, monkeypatch):
    fetched = asyncio.run(_browse(*_served(tmp_path, monkeypatch, seed=False)))
    _assert_renderable(fetched)
    assert fetched[("health", "latest-weight", "unfiltered")][2]["rows"] == [
        {"kg": "no data"}]
    assert fetched[("observed", "latest-imports", "filtered")][2]["rows"][0][
        "connector"] == "No imports yet"


def test_every_card_is_served_and_renderable_with_the_persona_seeded(tmp_path, monkeypatch):
    fetched = asyncio.run(_browse(*_served(tmp_path, monkeypatch, seed=True)))
    _assert_renderable(fetched)

    def rows(slug, chart, form):
        return fetched[(slug, chart, form)][2]["rows"]

    # The date filters round-trip: page form -> plugin -> Datasette params.
    assert _places(rows("observed", "photo-map", "unfiltered")) == [
        "Lake Bled", "Veracruz All Natural"]
    assert _places(rows("observed", "photo-map", "filtered")) == ["Veracruz All Natural"]
    assert rows("observed", "messages-count", "unfiltered") == [{"messages": 14}]
    assert rows("observed", "messages-count", "filtered") == [{"messages": 13}]
    assert rows("journal", "days-logged", "filtered") == [{"days": 4}]
    assert rows("journal", "days-logged", "unfiltered") == [{"days": 5}]
    assert rows("journal", "days-logged", "blank from") == [{"days": 5}]
    assert _places(rows("observed", "photo-map", "blank from")) == [
        "Lake Bled", "Veracruz All Natural"]
    assert rows("health", "last-night-sleep", "unfiltered") == [{"hours": "7.0 h"}]
    assert rows("people", "people-in-touch", "unfiltered") == [{"people": 3}]
    assert rows("observed", "top-people", "unfiltered")[0]["person"] == "Jake Morales"
