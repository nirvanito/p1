"""Google Contacts (lifecrm/online/google_contacts.py + _google_oauth.py): the
owner signs in once in their own browser, and each morning the address book
is fetched as one vCard the contacts connector reads — mirrored per Google
account, its first import asked about like any other source's.

Google is faked end to end: `_google_oauth._open` answers for the token
endpoint and the People API, and `_open_browser` plays the browser — it
reads the consent URL and, from a thread, sends Google's redirect to the
loopback listener the flow really opened on 127.0.0.1. Nothing leaves this
computer, and no real credential exists anywhere in it.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import threading
import urllib.error
import urllib.parse
from datetime import date

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import collect, nudges, online
from lifecrm.cli import app
from lifecrm.connectors import contacts
from lifecrm.domain import entities, observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.online import _google_oauth as oauth
from lifecrm.online import google_contacts as gc
from lifecrm.online import secrets
from test_collect import home  # noqa: F401  (the autouse home isolation)

runner = CliRunner()

EMAIL = "alex.rivera@gmail.com"
ACCOUNT = f"google:{EMAIL}"
CLIENT_ID = "123456789012-fake.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-not-a-real-secret-0001"
REFRESH = "1//fake-refresh-token-0001"
AUTH_CODE = "4/fake-auth-code-0001"
RECONNECT = "Google Contacts needs connecting again: python -m lifecrm connect google_contacts"

JAKE = {
    "resourceName": "people/c1001", "etag": "%EgUBAgMEBQ==",
    "metadata": {"sources": [{"type": "CONTACT", "id": "1001",
                              "updateTime": "2026-10-01T12:34:56.789Z"}]},
    "names": [{"metadata": {"primary": True}, "displayName": "Jake Morales",
               "familyName": "Morales", "givenName": "Jake"}],
    "nicknames": [{"value": "Jakey"}],
    "emailAddresses": [{"value": "Jake.Morales@Example.com", "type": "home",
                        "formattedType": "Home"}],
    "phoneNumbers": [
        {"value": "(512) 555-0142", "canonicalForm": "+15125550142", "type": "mobile",
         "formattedType": "Mobile"},
        {"value": "512 555 0143", "canonicalForm": "+15125550143", "type": "Work cell",
         "formattedType": "Work cell"}],
    "birthdays": [{"metadata": {"primary": True},
                   "date": {"year": 1993, "month": 10, "day": 30}}],
    "organizations": [{"name": "Acme Robotics", "department": "Platform",
                       "title": "Director of Product"}],
    "addresses": [{"type": "home", "streetAddress": "12 Oak St, Apt 4", "city": "Austin",
                   "region": "TX", "postalCode": "78702", "country": "US"}],
    "urls": [{"value": "https://jake.example.com/climbing", "type": "blog",
              "formattedType": "Blog"}],
    "biographies": [{"value": "Grew up in El Paso; met at the climbing gym on "
                              "Sundays — loves jalapeños, café con leche, and "
                              "long multi-pitch routes.\nCall before noon.",
                     "contentType": "TEXT_PLAIN"}],
    "memberships": [
        {"contactGroupMembership": {"contactGroupId": "myContacts",
                                    "contactGroupResourceName": "contactGroups/myContacts"}},
        {"contactGroupMembership": {"contactGroupId": "1a2b",
                                    "contactGroupResourceName": "contactGroups/1a2b"}}],
}
PRIYA = {
    "resourceName": "people/c1002",
    "metadata": {"sources": [{"type": "CONTACT", "updateTime": "2026-09-14T08:00:00Z"}]},
    "names": [{"displayName": "Priya Natarajan", "givenName": "Priya",
               "familyName": "Natarajan"}],
    "emailAddresses": [{"value": "priya@acme-robotics.example", "type": "work"}],
    # As typed it is a UK national number; only Google's E.164 form says so.
    "phoneNumbers": [{"value": "020 7946 0958", "canonicalForm": "+442079460958",
                      "type": "workMobile", "formattedType": "Work Mobile"}],
    "birthdays": [{"date": {"month": 2, "day": 29}}],
}
ELLIS = {
    "resourceName": "people/c1003",
    "metadata": {"sources": [{"type": "CONTACT", "updateTime": "2026-08-02T17:05:00Z"}]},
    "organizations": [{"name": "Ellis Dental"}],
    "phoneNumbers": [{"value": "512-555-0177", "canonicalForm": "+15125550177",
                      "type": "work"}],
}
GROUPS = [{"resourceName": "contactGroups/myContacts", "name": "myContacts",
           "groupType": "SYSTEM_CONTACT_GROUP"},
          {"resourceName": "contactGroups/1a2b", "name": "Climbing",
           "groupType": "USER_CONTACT_GROUP"}]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return ".".join((_b64(b'{"alg":"RS256"}'), _b64(json.dumps(claims).encode()),
                     _b64(b"not-checked")))


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _http_error(url: str, code: int, body: dict):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(json.dumps(body).encode()))


class FakeGoogle:
    """Google's sign-in page, token endpoint, People API and contact groups,
    as this code talks to them — and the browser in between."""

    def __init__(self):
        self.people = [JAKE, PRIYA, ELLIS]
        self.groups = GROUPS
        self.per_page = 1000
        self.scope = " ".join(gc.SCOPES[:2] + ["https://www.googleapis.com/auth/contacts.readonly"])
        self.answer: dict = {}             # overrides for the redirect's query
        self.token_extra: dict = {}
        self.refresh_error = None          # e.g. "invalid_grant"
        self.api_error = None              # (status, body)
        self.rotate = None                 # a new refresh token on the next refresh
        self.offline = False               # no network at all
        self.refresh_token = REFRESH
        self.access = 0
        self.consent: dict = {}
        self.calls: list[tuple] = []
        self.pages: list[str] = []
        self.threads: list[threading.Thread] = []

    # -------------------------------------------------- the browser
    def browser(self, url: str) -> bool:
        assert url.startswith(oauth.AUTH_URL + "?")
        self.consent = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        query = {"state": self.consent["state"], "code": AUTH_CODE, "scope": self.scope,
                 **self.answer}
        query = {k: v for k, v in query.items() if v is not None}
        where = urllib.parse.urlsplit(self.consent["redirect_uri"])

        def redirect():
            c = http.client.HTTPConnection(where.hostname, where.port, timeout=10)
            try:
                c.request("GET", "/?" + urllib.parse.urlencode(query))
                self.pages.append(c.getresponse().read().decode("utf-8"))
            finally:
                c.close()
        t = threading.Thread(target=redirect, daemon=True)
        t.start()
        self.threads.append(t)
        return True

    # -------------------------------------------------- HTTP
    def __call__(self, req):
        url = req.full_url
        if self.offline:
            raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")
        if url == oauth.TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            self.calls.append(("token", form))
            return self._token(url, form)
        auth = req.get_header("Authorization")
        self.calls.append(("get", url, auth))
        if self.api_error:
            raise _http_error(url, *self.api_error)
        assert auth == f"Bearer ya29.fake-access-{self.access}", auth
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if url.startswith(gc.PEOPLE_URL + "?"):
            return self._page(q, self.people, "connections")
        if url.startswith(gc.GROUPS_URL + "?"):
            return self._page(q, self.groups, "contactGroups")
        raise AssertionError(f"unexpected request: {url}")

    def _token(self, url, form):
        assert form["client_id"] == CLIENT_ID and form["client_secret"] == CLIENT_SECRET
        if form["grant_type"] == "authorization_code":
            assert form["code"] == AUTH_CODE
            assert form["redirect_uri"] == self.consent["redirect_uri"]
            challenge = _b64(hashlib.sha256(form["code_verifier"].encode()).digest())
            assert challenge == self.consent["code_challenge"]       # PKCE S256
            self.access += 1
            return _Resp(json.dumps({
                "access_token": f"ya29.fake-access-{self.access}", "expires_in": 3599,
                "refresh_token": self.refresh_token, "scope": self.scope,
                "token_type": "Bearer", "id_token": _jwt({"email": EMAIL, "sub": "42"}),
                **self.token_extra}).encode())
        assert form["grant_type"] == "refresh_token"
        if self.refresh_error:
            raise _http_error(url, 400, {"error": self.refresh_error,
                                         "error_description": "Token has been expired "
                                                              "or revoked."})
        assert form["refresh_token"] == self.refresh_token
        self.access += 1
        out = {"access_token": f"ya29.fake-access-{self.access}", "expires_in": 3599,
               "scope": self.scope, "token_type": "Bearer"}
        if self.rotate:
            self.refresh_token, self.rotate = self.rotate, None
            out["refresh_token"] = self.refresh_token
        return _Resp(json.dumps(out).encode())

    def _page(self, q, items, key):
        size = min(int(q["pageSize"]), self.per_page)
        start = int(q.get("pageToken", "page-0").split("-")[1])
        out = {key: items[start:start + size], "totalItems": len(items)}
        if start + size < len(items):
            out["nextPageToken"] = f"page-{start + size}"
        return _Resp(json.dumps(out).encode())

    def people_requests(self):
        return [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(c[1]).query))
                for c in self.calls if c[0] == "get" and c[1].startswith(gc.PEOPLE_URL)]

    def join(self):
        for t in self.threads:
            t.join(10)


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(oauth, "_open", fake)
    monkeypatch.setattr(oauth, "_open_browser", fake.browser)
    monkeypatch.setattr(oauth, "WAIT", 20)
    yield fake
    fake.join()


@pytest.fixture
def client_json(tmp_path):
    p = tmp_path / "downloads" / "client_secret_123456789012-fake.apps.googleusercontent.com.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"installed": {
        "client_id": CLIENT_ID, "project_id": "life-crm-0001",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": CLIENT_SECRET, "redirect_uris": ["http://localhost"]}}),
        encoding="utf-8")
    return p


def _connect(cfg, client_json):
    online.connect(cfg, gc.SERVICE, [str(client_json)], None)


def _only(result, name="contacts"):
    return next(r for r in result["sources"] if r["source"] == name)


# ---------------------------------------------------------------- signing in

def test_signing_in_goes_through_the_loopback_with_pkce_and_keeps_the_token(
        cfg, google, client_json):
    _connect(cfg, client_json)
    assert secrets.load(cfg)[gc.SERVICE] == {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH, "account": ACCOUNT}
    c = google.consent
    assert c["redirect_uri"].startswith("http://127.0.0.1:") and \
        int(c["redirect_uri"].rsplit(":", 1)[1]) > 0
    assert (c["response_type"], c["access_type"], c["prompt"],
            c["code_challenge_method"]) == ("code", "offline", "consent", "S256")
    assert set(c["scope"].split()) == {"openid", "email", gc.CONTACTS_SCOPE}
    assert "code_verifier" not in c and len(c["state"]) >= 32
    google.join()
    assert "Signed in" in google.pages[0]
    # A missing step (People API off) would have been said now: one page asked.
    assert [q["pageSize"] for q in google.people_requests()] == ["1"]


def test_a_wrong_state_is_refused_and_its_code_never_exchanged(cfg, google, client_json):
    google.answer = {"state": "forged-by-someone-else"}
    with pytest.raises(DomainError, match="state did not match"):
        _connect(cfg, client_json)
    google.join()
    assert "Refused" in google.pages[0]
    assert not [c for c in google.calls if c[0] == "token"]
    assert secrets.load(cfg) == {}


def test_saying_no_on_googles_page_connects_nothing(cfg, google, client_json):
    google.answer = {"code": None, "scope": None, "error": "access_denied"}
    with pytest.raises(DomainError, match="you chose not to allow it"):
        _connect(cfg, client_json)
    assert secrets.load(cfg) == {}


def test_an_unticked_contacts_box_connects_nothing(cfg, google, client_json):
    google.scope = "openid email"
    with pytest.raises(DomainError, match="See and download your contacts"):
        _connect(cfg, client_json)
    assert secrets.load(cfg) == {}


def test_a_disabled_people_api_is_said_at_connect_not_at_dawn(cfg, google, client_json):
    google.api_error = (403, {"error": {
        "code": 403, "status": "PERMISSION_DENIED",
        "message": "People API has not been used in project 1 before or it is disabled.",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "SERVICE_DISABLED"}]}})
    with pytest.raises(DomainError, match="the People API is not enabled"):
        _connect(cfg, client_json)
    assert secrets.load(cfg) == {}


def test_only_a_desktop_app_client_json_is_taken(tmp_path):
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}),
                   encoding="utf-8")
    with pytest.raises(DomainError, match="is a web client — this needs an OAuth "
                                          "client of type Desktop app"):
        oauth.load_client(web, service=gc.SERVICE)
    (tmp_path / "junk.json").write_text("not json", encoding="utf-8")
    with pytest.raises(DomainError, match="cannot read junk.json"):
        oauth.load_client(tmp_path / "junk.json", service=gc.SERVICE)
    with pytest.raises(DomainError, match="not found"):
        oauth.load_client(tmp_path / "nope.json", service=gc.SERVICE)


def test_a_testing_apps_seven_days_are_said_at_connect(cfg, google, client_json, capsys):
    google.token_extra = {"refresh_token_expires_in": 604799}
    _connect(cfg, client_json)
    err = capsys.readouterr().err
    assert "only 7 day(s)" in err and "Publish app" in err


def test_connecting_again_reuses_the_kept_client(cfg, google, client_json):
    _connect(cfg, client_json)
    google.refresh_token = "1//fake-refresh-token-0002"
    online.connect(cfg, gc.SERVICE, [], None)              # no file: the kept client
    assert secrets.load(cfg)[gc.SERVICE]["refresh_token"] == "1//fake-refresh-token-0002"


# ---------------------------------------------------------------- fetching

def test_each_fetch_refreshes_the_token_and_pages_through_every_contact(
        cfg, google, client_json):
    _connect(cfg, client_json)
    google.per_page, google.calls = 1, []
    google.rotate = "1//fake-refresh-token-rotated"
    target = online.fetch(cfg, online.get(gc.SERVICE))
    assert target == online.dest_dir(cfg, gc.SERVICE) / "contacts.vcf"
    assert target.read_bytes().count(b"BEGIN:VCARD") == 3
    token = [c[1] for c in google.calls if c[0] == "token"]
    assert [t["grant_type"] for t in token] == ["refresh_token"]
    assert token[0]["refresh_token"] == REFRESH
    pages = google.people_requests()
    assert [q.get("pageToken") for q in pages] == [None, "page-1", "page-2"]
    assert {(q["personFields"], q["sources"], q["pageSize"]) for q in pages} == {
        (gc.PERSON_FIELDS, "READ_SOURCE_TYPE_CONTACT", "1000")}
    assert secrets.load(cfg)[gc.SERVICE]["refresh_token"] == "1//fake-refresh-token-rotated"


def test_the_vcard_reads_back_through_the_contacts_connector(cfg, google, client_json):
    _connect(cfg, client_json)
    target = online.fetch(cfg, online.get(gc.SERVICE))
    scan = contacts.scan(target, cfg)
    assert scan["account"] == ACCOUNT and scan["source_detail"] == "vCard 3.0, google_contacts"
    by = {r["title"]: r for r in scan["records"]}
    assert set(by) == {"Jake Morales", "Priya Natarajan", "Ellis Dental"}

    jake = by["Jake Morales"]
    detail = json.loads(jake["value_json"])
    assert jake["external_id"] == "people/c1001"
    assert jake["started_at"] == "2026-10-01T12:34:56Z"          # REV: the last edit
    assert jake["origin_app"] == "google_contacts"
    assert {(p["kind"], p["value"]) for p in jake["participants"]} == {
        ("phone", "+15125550142"), ("phone", "+15125550143"),
        ("email", "jake.morales@example.com")}
    assert [(p["label"], p["handle"]) for p in detail["phones"]] == [
        ("mobile", "+15125550142"), ("Work cell", "+15125550143")]
    assert detail["birthday"] == "1993-10-30" and detail["nickname"] == "Jakey"
    assert (detail["org"], detail["title"]) == ("Acme Robotics", "Director of Product")
    assert detail["addresses"] == [{"label": "home", "street": "12 Oak St, Apt 4",
                                    "city": "Austin", "state": "TX", "zip": "78702",
                                    "country": "US"}]
    assert detail["urls"] == [{"label": "Blog", "url": "https://jake.example.com/climbing"}]
    assert "Birthday: October 30, 1993" in jake["body"]
    assert JAKE["biographies"][0]["value"] in jake["body"]       # folded, escaped, whole

    priya = json.loads(by["Priya Natarajan"]["value_json"])
    assert priya["birthday"] == "--02-29"
    assert priya["phones"] == [{"label": "Work Mobile", "number": "+442079460958",
                                "handle": "+442079460958"}]
    ellis = by["Ellis Dental"]
    assert ellis["external_id"] == "people/c1003" and ellis["body"] is None
    assert scan["unmapped"] == [{"what": "CATEGORIES", "count": 1}]  # groups: kept in the file
    assert b"CATEGORIES:myContacts,Climbing" in target.read_bytes()


def test_an_unchanged_address_book_leaves_the_file_alone(cfg, google, client_json):
    _connect(cfg, client_json)
    target = online.fetch(cfg, online.get(gc.SERVICE))
    stamp = target.stat().st_mtime_ns
    (target.parent / "old-export.vcf").write_bytes(b"BEGIN:VCARD\r\nEND:VCARD\r\n")
    assert online.fetch(cfg, online.get(gc.SERVICE)) == target
    assert target.stat().st_mtime_ns == stamp
    assert [p.name for p in target.parent.glob("*.vcf")] == ["contacts.vcf"]


def test_a_birthday_reaches_the_morning_nudges(conn, cfg, google, client_json):
    _connect(cfg, client_json)
    scan = contacts.scan(online.fetch(cfg, online.get(gc.SERVICE)), cfg)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    jake = entities.create(conn, "person", "Jake Morales")["id"]
    obs.link_handle(conn, "phone", "+15125550142", jake)
    conn.commit()
    pack = nudges.build(conn, cfg, today=date(2026, 10, 25))
    titles = [n["title"] for n in pack["nudges"] if n["kind"] == "birthday"]
    assert titles == ["Jake Morales's birthday is in 5 days (2026-10-30), turning 33"]


# ---------------------------------------------------------------- collect

def test_collect_asks_before_the_first_import_then_collects(conn, cfg, google, client_json):
    _connect(cfg, client_json)
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["hold"]) == ("held", "first_import"), r
    assert f"for {ACCOUNT}, 3 record(s)" in r["reason"]
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0

    done = collect.approve(conn, cfg, "contacts", apply=True)
    assert done["applied"] and done["applied_counts"]["added"] == 3
    assert _only(collect.run(conn, cfg))["outcome"] == "unchanged"

    google.people = [{**JAKE, "nicknames": [{"value": "Jake the Snake"}],
                      "metadata": {"sources": [{"type": "CONTACT",
                                                "updateTime": "2026-10-24T09:00:00Z"}]}},
                     PRIYA, ELLIS]
    r = _only(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"] == {"updated": 1, "identical": 2}, r


def _active(conn, account):
    return sorted(r[0] for r in conn.execute(
        "SELECT o.title FROM observations o JOIN imports i ON i.id = o.import_id "
        "WHERE o.status = 'active' AND i.account = ?", (account,)))


def test_a_contact_deleted_in_google_goes_and_another_accounts_stay(
        conn, cfg, google, client_json, tmp_path):
    work = tmp_path / "work.vcf"                   # the same reader, another account
    work.write_bytes(gc.vcard([{**ELLIS, "resourceName": "people/c9001"},
                               {**PRIYA, "resourceName": "people/c9002"}],
                              {}, "google:alex@work.example"))
    scan = contacts.scan(work, cfg)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    _connect(cfg, client_json)
    collect.approve(conn, cfg, "contacts", apply=True)

    google.people = [JAKE, PRIYA]                  # Ellis deleted in Google
    r = _only(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["removed"] == 1, r
    assert "new" not in r["counts"] and r["snapshot"]          # the undo, taken first
    assert _active(conn, ACCOUNT) == ["Jake Morales", "Priya Natarajan"]
    assert _active(conn, "google:alex@work.example") == ["Ellis Dental", "Priya Natarajan"]

    google.people = [{"resourceName": "people/c1004",      # both gone at once: held
                      "names": [{"displayName": "Sam Okafor"}]}]
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["hold"]) == ("held", "mass_removal"), r
    assert _active(conn, ACCOUNT) == ["Jake Morales", "Priya Natarajan"]


def test_a_revoked_sign_in_says_connect_again(conn, cfg, google, client_json):
    _connect(cfg, client_json)
    google.refresh_error = "invalid_grant"
    with pytest.raises(DomainError) as e:
        online.fetch(cfg, online.get(gc.SERVICE))
    assert str(e.value) == RECONNECT
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["reason"]) == ("error", RECONNECT), r
    pack = nudges.build(conn, cfg, today=date(2026, 10, 25))
    item = next(n for n in pack["nudges"] if n["key"] == "collect:contacts:error")
    assert item["detail"] == RECONNECT

    google.refresh_error = "invalid_client"
    with pytest.raises(DomainError, match="make or rotate a Desktop app client"):
        online.fetch(cfg, online.get(gc.SERVICE))


def test_no_network_is_said_as_such_and_tried_again(cfg, google, client_json):
    google.offline = True
    with pytest.raises(DomainError, match="could not reach Google .* nothing connected"):
        _connect(cfg, client_json)
    google.offline = False
    _connect(cfg, client_json)
    google.offline = True
    with pytest.raises(DomainError, match="could not reach Google Contacts .* the next "
                                          "run tries again"):
        online.fetch(cfg, online.get(gc.SERVICE))


def test_an_empty_answer_is_an_error_never_every_card_deleted(cfg, google, client_json):
    _connect(cfg, client_json)
    google.people = []
    with pytest.raises(DomainError, match=f"has no contacts for {ACCOUNT}"):
        online.fetch(cfg, online.get(gc.SERVICE))


# ---------------------------------------------------------------- never printed

def test_no_secret_or_token_is_ever_printed(tmp_path, monkeypatch, google, client_json):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["connect"])
    assert r.exit_code == 0 and "google_contacts" in r.output and "People API" in r.output
    said = []
    for args in (["connect", gc.SERVICE, str(client_json)], ["connect"],
                 ["fetch", gc.SERVICE], ["observe", "contacts"],
                 ["collect", "--json"], ["collect", "approve", "contacts", "--yes"],
                 ["collect", "status"]):
        r = runner.invoke(app, args)
        assert r.exit_code == 0, (args, r.output)
        said.append(r.output)
    google.refresh_error = "invalid_grant"
    r = runner.invoke(app, ["fetch", gc.SERVICE])
    assert r.exit_code != 0 and RECONNECT in r.output
    said.append(r.output)
    text = "\n".join(said)
    assert "connected: Google Contacts" in text and f"signed in as {EMAIL}" in text
    assert "Jake Morales" in text
    for secret in (CLIENT_SECRET, REFRESH, AUTH_CODE, "ya29.", "fake-access"):
        assert secret not in text, secret
    kept = json.loads((tmp_path / "data" / "secrets.json").read_text(encoding="utf-8"))
    assert kept[gc.SERVICE]["refresh_token"] == REFRESH       # kept, only there
