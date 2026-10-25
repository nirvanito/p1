"""The read-side features through the web server: /api/search, /api/trips,
/api/conversation/<id>, /api/features — and the checklist's one write,
POST /api/features: only with the server's key, only same-origin JSON,
through features.save (only [features] changes, never the database), read
back at once by the running server, as a terminal change is.
"""

from __future__ import annotations

import json
import threading
import tomllib

import pytest
from test_web import Client, copy_root, seed_root

from lifecrm import db as db_mod
from lifecrm.web import server as web_server


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    base = tmp_path_factory.mktemp("features-web-seed")
    seed_root(base)
    return base / "root"


def _serve(cfg, **opts):
    server = web_server.create_server(cfg, "127.0.0.1", 0, log_questions=False,
                                      use_llm=False, refresh_seconds=0, **opts)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Client(server)


def _post(c, body, *, key="sekrit", ctype="application/json", headers=None):
    raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
    h = {"Content-Type": ctype, **(headers or {})}
    path = "/api/features" + (f"?k={key}" if key else "")
    status, _msg, data = c.request("POST", path, body=raw, headers=h)
    return status, json.loads(data)


def _table(cfg) -> dict:
    return tomllib.loads((cfg.root / "config.toml").read_text(encoding="utf-8")).get(
        "features", {})


def test_the_page_checks_and_unchecks_through_the_key(seeded, tmp_path):
    cfg = copy_root(seeded, tmp_path)
    db_before = cfg.db_path.read_bytes()
    rest_before = {k: v for k, v in tomllib.loads(
        (cfg.root / "config.toml").read_text(encoding="utf-8")).items() if k != "features"}
    server, c = _serve(cfg, token="sekrit")
    try:
        assert c.get("/api/features?k=sekrit")["writable"] is True
        assert c.get("/api/status?k=sekrit")["feature_writes"] is True
        status, out = _post(c, {"changes": {"places": False, "search": False}})
        assert status == 200, out
        assert out["changed"] == ["places", "search"] and out["on"] == 12
        assert _table(cfg) == {"places": False, "search": False}
        # the running server reads the new switches at once
        assert c.get("/api/trips?k=sekrit")["enabled"] is False
        assert c.get("/api/search?q=dinner&k=sekrit")["enabled"] is False
        status, out = _post(c, {"changes": {"places": False}})
        assert status == 200 and out["changed"] == []
        status, out = _post(c, {"all": False})
        assert status == 200 and out["on"] == 0 and not any(_table(cfg).values())
        status, out = _post(c, {"all": True})
        assert status == 200 and out["on"] == 14 and all(_table(cfg).values())
        # nothing but [features] changed, and the database not at all
        rest_after = {k: v for k, v in tomllib.loads(
            (cfg.root / "config.toml").read_text(encoding="utf-8")).items() if k != "features"}
        assert rest_after == rest_before
        assert cfg.db_path.read_bytes() == db_before
    finally:
        server.shutdown()
        server.server_close()


def test_every_refusal_changes_nothing(seeded, tmp_path):
    cfg = copy_root(seeded, tmp_path)
    before = (cfg.root / "config.toml").read_bytes()
    server, c = _serve(cfg, token="sekrit")
    open_server, open_c = _serve(copy_root(seeded, tmp_path / "open"))     # no key
    try:
        cases = [
            (_post(c, {"changes": {"places": False}}, key=None), 401, "needs its key"),
            (_post(c, {"changes": {"places": False}}, key="wrong"), 401, "needs its key"),
            (_post(open_c, {"changes": {"places": False}}, key=None), 403, "--token random"),
            (_post(c, {"changes": {"places": False}}, ctype="text/plain"), 400, "as JSON"),
            (_post(c, "not json"), 400, "not JSON"),
            (_post(c, {"changes": {"nope": False}}), 400, "no feature 'nope'"),
            (_post(c, {"changes": {"places": "off"}}), 400, "must be true or false"),
            (_post(c, {"changes": {}}), 400, "at least one feature"),
            (_post(c, {"all": "yes"}), 400, "true or false"),
            (_post(c, {"places": False}), 400, '{"changes"'),
            (_post(c, {"changes": {"places": False}},
                   headers={"Sec-Fetch-Site": "cross-site"}), 403, "cross-site"),
            (_post(c, {"changes": {"places": False}},
                   headers={"Sec-Fetch-Site": "same-site"}), 403, "cross-origin"),
            (_post(c, {"changes": {"places": False}},
                   headers={"Origin": "http://127.0.0.1:1"}), 403, "cross-origin"),
            (_post(c, {"changes": {"places": False}}, headers={"Origin": "null"}), 403,
             "cross-origin"),
        ]
        for (status, out), want, text in cases:
            assert status == want and text in out["error"], (status, out, want, text)
        assert (cfg.root / "config.toml").read_bytes() == before
        assert open_c.get("/api/features")["writable"] is False
        status, _m, data = c.request("PUT", "/api/features?k=sekrit")
        assert status == 405 and b"accepts GET or POST only" in data
        # a same-origin page's own request is fine
        port = server.server_address[1]
        status, out = _post(c, {"changes": {"highlights": False}},
                            headers={"Origin": f"http://127.0.0.1:{port}",
                                     "Sec-Fetch-Site": "same-origin",
                                     "Host": f"127.0.0.1:{port}"})
        assert status == 200 and out["changed"] == ["highlights"]
    finally:
        for s in (server, open_server):
            s.shutdown()
            s.server_close()


def test_a_change_made_in_the_terminal_shows_without_a_restart(seeded, tmp_path):
    cfg = copy_root(seeded, tmp_path)
    server, c = _serve(cfg)
    try:
        assert c.get("/api/search?q=dinner")["enabled"] is True
        from lifecrm import features
        from lifecrm.config import load_config
        features.save(load_config(cfg.root), {"search": False})
        assert c.get("/api/search?q=dinner")["enabled"] is False
        by = {x["key"]: x["on"] for x in c.get("/api/features")["features"]}
        assert by["search"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_the_new_routes_answer_and_write_nothing(seeded, tmp_path):
    cfg = copy_root(seeded, tmp_path)
    before = cfg.db_path.read_bytes()
    server, c = _serve(cfg)
    try:
        conn = db_mod.connect(cfg.db_path, readonly=True)
        jake = conn.execute("SELECT id FROM entities WHERE name='Jake'").fetchone()["id"]
        conn.close()
        s = c.get("/api/search?q=dinner")
        assert s["enabled"] and "facets" in s and s["total"] == len(s["results"])
        assert c.get("/api/search?q=dinner&tier=confirmed")["filters"]["tier"] == "confirmed"
        c.get("/api/search?q=dinner&tier=nope", expect=400)
        t = c.get("/api/trips")
        assert t["enabled"] and t["ok"] is False and "places fetch" in t["error"]
        conv = c.get(f"/api/conversation/{jake}")
        assert conv["enabled"] and conv["name"] == "Jake" and conv["items"]
        c.get("/api/conversation/abc", expect=400)
        c.get("/api/conversation/99999", expect=404)
        f = c.get("/api/features")
        assert f["total"] == 14 and f["on"] == 14
        st = c.get("/api/status")
        assert st["features"]["total"] == 14
        page = c.get("/")
        assert b'data-feature="search"' in page and b"viewSearch" in page
    finally:
        server.shutdown()
        server.server_close()
    assert cfg.db_path.read_bytes() == before


def test_an_unchecked_feature_answers_disabled(seeded, tmp_path):
    cfg = copy_root(seeded, tmp_path)
    with (cfg.root / "config.toml").open("a", encoding="utf-8") as fh:
        fh.write("\n[features]\nsearch = false\nconversations = false\nplaces = false\n")
    from lifecrm.config import load_config
    cfg = load_config(cfg.root)
    server, c = _serve(cfg)
    try:
        assert c.get("/api/search?q=dinner")["enabled"] is False
        assert c.get("/api/trips")["enabled"] is False
        conn = db_mod.connect(cfg.db_path, readonly=True)
        jake = conn.execute("SELECT id FROM entities WHERE name='Jake'").fetchone()["id"]
        conn.close()
        assert c.get(f"/api/conversation/{jake}")["enabled"] is False
        by = {x["key"]: x["on"] for x in c.get("/api/features")["features"]}
        assert by["search"] is False and by["highlights"] is True
    finally:
        server.shutdown()
        server.server_close()
