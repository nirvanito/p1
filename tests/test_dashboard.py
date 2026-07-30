"""Dashboards-as-code stay honest: every chart query in dashboards.yml is
executed against a seeded current-schema database on every test run, and its
SELECT aliases are checked against the display properties that reference
them. A schema change that would silently blank a chart fails here instead
of the evening someone opens the dashboard."""

import re

import yaml
from conftest import cli_project

from lifecrm.config import PROJECT_ROOT
from lifecrm.domain import entries, health, measurements

DASH_FILE = PROJECT_ROOT / "dashboards" / "dashboards.yml"
_OPTIONAL_BLOCK = re.compile(r"\[\[.*?\]\]", re.DOTALL)
# The libraries datasette-dashboards 0.8.x actually dispatches client-side.
# Anything else (including the bar/line/pie shorthands in the plugin's master
# README) renders as a silent empty card — that regression happened once.
_SUPPORTED_LIBRARIES = {"vega", "vega-lite", "metric", "table", "map", "markdown"}


def _display_fields(chart: dict) -> set:
    display = chart.get("display") or {}
    if chart["library"] == "metric":
        return {display["field"]}
    if chart["library"] == "vega-lite":
        return {enc["field"] for enc in (display.get("encoding") or {}).values()
                if isinstance(enc, dict) and "field" in enc}
    return set()


def _dashboards() -> dict:
    cfg = yaml.safe_load(DASH_FILE.read_text(encoding="utf-8"))
    return cfg["plugins"]["datasette-dashboards"]


def _seed(conn) -> None:
    entries.create(conn, journal_date="2026-07-27", kind="event",
                   title="Lunch", primary_category="food")
    entries.create(conn, journal_date="2026-07-27", kind="task",
                   title="Call the bank", primary_category="chores")
    measurements.add(conn, journal_date="2026-07-27", metric="mood",
                     numeric_value=7)
    health.insert(conn, {"metric": "sleep_session",
                         "journal_date": "2026-07-27",
                         "started_at": "2026-07-27T03:00:00Z",
                         "ended_at": "2026-07-27T11:00:00Z", "value": 420.0,
                         "unit": "min", "external_id": "aa" * 16,
                         "source_file": "x.zip"})
    conn.commit()


def test_every_chart_query_runs_and_matches_display(conn):
    _seed(conn)
    for dash_name, dash in _dashboards().items():
        for chart_name, chart in dash["charts"].items():
            assert chart["library"] in _SUPPORTED_LIBRARIES, (
                f"{dash_name}.{chart_name}: library '{chart['library']}' is "
                f"not dispatched by the pinned datasette-dashboards renderer")
            sql = _OPTIONAL_BLOCK.sub("", chart["query"])
            cur = conn.execute(sql)                    # raises on schema drift
            cols = {d[0] for d in cur.description}
            missing = _display_fields(chart) - cols
            assert not missing, (
                f"{dash_name}.{chart_name}: display refers to {sorted(missing)} "
                f"but the query returns {sorted(cols)}")


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
