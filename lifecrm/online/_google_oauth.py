"""Signing in to Google from this computer: OAuth 2.0 for installed apps,
stdlib only (https://developers.google.com/identity/protocols/oauth2/native-app).

Google offers no way into contacts (or mail, or the calendar API) without
OAuth — its CardDAV server refuses everything else — so a service that needs
one signs in once, here, and keeps the refresh token:

1. A listener on 127.0.0.1 at a random free port (the loopback redirect
   Google supports for Desktop app clients), accepting nothing but the one
   answer it waits for.
2. The consent page is opened in the owner's own browser — and printed, for
   when no browser opens. The owner signs in on Google's page; this code
   never sees their password, only what Google sends back.
3. Google redirects the browser to the listener with a one-time code. The
   `state` it carries must be the one this run sent (anything else is
   refused, never exchanged), and the code is exchanged with the PKCE
   verifier only this run knows (S256), so a code intercepted on its way is
   useless to anyone else.
4. `access_type=offline` + `prompt=consent` make Google return a refresh
   token every time; each daily fetch trades it for an hour's access token
   and keeps nothing else.

Every HTTP call goes through `_open` (tests replace it) and the browser
through `_open_browser`; a refused refresh token (revoked, expired, a
Testing app's seven days over) is one line that says to connect again.
Nothing here prints a token, a code or the client secret, and the request
log of the listener is silenced: the redirect's query string holds the code.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from ..domain.util import DomainError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 60                 # one HTTP call
WAIT = 300                   # the owner signing in, in their browser
USER_AGENT = "lifecrm (personal use)"


def _open(req: urllib.request.Request):
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _open_browser(url: str) -> bool:
    return webbrowser.open(url)


def reconnect(title: str, service: str) -> str:
    return f"{title} needs connecting again: python -m lifecrm connect {service}"


# ---------------------------------------------------------------- the client

def load_client(path, *, service: str) -> dict:
    """{client_id, client_secret} from the JSON Google Cloud downloads for an
    OAuth client of type Desktop app."""
    p = Path(str(path).strip().strip("\"'")).expanduser()
    where = (f"Google Cloud › Google Auth Platform › Clients › Create client › "
             f"Desktop app (docs/{service}.md)")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise DomainError(f"not found: {p} — pass the client JSON downloaded from "
                          f"{where}") from None
    except (OSError, ValueError) as e:
        raise DomainError(f"cannot read {p.name} as the client JSON Google downloads "
                          f"({type(e).__name__}) — get it from {where}") from None
    if not isinstance(data, dict):
        data = {}
    inst = data.get("installed")
    if not isinstance(inst, dict):
        kind = next((k for k in ("web", "android", "ios") if k in data), None)
        raise DomainError(
            f"{p.name} is {'a ' + kind + ' client' if kind else 'not an OAuth client JSON'}"
            f" — this needs an OAuth client of type Desktop app: {where}")
    if not isinstance(inst.get("client_id"), str) or not inst["client_id"].strip():
        raise DomainError(f"{p.name} holds no client_id — download it again from {where}")
    return {"client_id": inst["client_id"].strip(),
            "client_secret": str(inst.get("client_secret") or "").strip()}


# ---------------------------------------------------------------- HTTP

def _send(req: urllib.request.Request) -> tuple[int, bytes]:
    """(status, body); an HTTP error is an answer too. A connection that
    never got one raises OSError, which the caller says as 'could not
    reach' — its text names no token."""
    try:
        with _open(req) as resp:
            return getattr(resp, "status", 200) or 200, resp.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read() or b""
        finally:
            e.close()


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _token(form: dict, *, title: str, service: str) -> dict:
    req = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json", "User-Agent": USER_AGENT})
    status, body = _send(req)
    data = _json(body)
    if status == 200 and isinstance(data.get("access_token"), str):
        return data
    err = str(data.get("error") or "")
    if err == "invalid_grant":
        raise DomainError(reconnect(title, service))
    if err in ("invalid_client", "unauthorized_client", "deleted_client"):
        raise DomainError(
            f"Google refused this computer's OAuth client ({err}) — it was deleted or "
            f"its secret changed: in Google Cloud › Google Auth Platform › Clients make "
            f"or rotate a Desktop app client, download its JSON, then run: python -m "
            f"lifecrm connect {service} <that file>")
    raise DomainError(f"Google's sign-in service answered HTTP {status}"
                      f"{' (' + err + ')' if err else ''} — nothing fetched; the next "
                      f"run tries again")


def refresh(client: dict, refresh_token: str, *, title: str, service: str) -> dict:
    """An access token for the next hour (and, rarely, a new refresh token
    to keep: Google may rotate it)."""
    form = {"grant_type": "refresh_token", "client_id": client["client_id"],
            "refresh_token": refresh_token}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    return _token(form, title=title, service=service)


def get_json(url: str, access_token: str, *, title: str, service: str,
             api: str) -> dict:
    """One API GET; a refusal said as what to do about it."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}", "Accept": "application/json",
        "User-Agent": USER_AGENT})
    status, body = _send(req)
    data = _json(body)
    if status == 200:
        if not data and body.strip() not in (b"{}", b""):
            raise DomainError(f"{title} answered something that is not JSON — nothing "
                              f"fetched; the next run tries again")
        return data
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    reasons = {str(d.get("reason")) for d in (error.get("details") or [])
               + (error.get("errors") or []) if isinstance(d, dict)}
    message = str(error.get("message") or "")
    if status == 401 or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons \
            or "insufficientPermissions" in reasons:
        raise DomainError(reconnect(title, service))
    if status == 403 and ({"SERVICE_DISABLED", "accessNotConfigured"} & reasons
                          or "has not been used" in message or "is disabled" in message):
        raise DomainError(f"the {api} is not enabled in your Google Cloud project — "
                          f"enable it (docs/{service}.md, step 2); the next run fetches")
    what = str(error.get("status") or "")
    raise DomainError(f"{title} answered HTTP {status}{' ' + what if what else ''} — "
                      f"nothing fetched; the next run tries again")


# ---------------------------------------------------------------- signing in

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce() -> tuple[str, str]:
    """(verifier, S256 challenge): 43 unreserved characters, and their hash."""
    verifier = _b64(os.urandom(32))
    return verifier, _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def id_claims(id_token: str | None) -> dict:
    """The claims of the ID token that came back with the tokens. Read, not
    verified: it arrived straight from Google's token endpoint over TLS,
    which OpenID Connect accepts in place of checking the signature."""
    try:
        payload = str(id_token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Life CRM</title>
<style>body{{font:16px system-ui,sans-serif;margin:3em auto;max-width:34em;color:#222}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}}}</style></head>
<body><h2>{head}</h2><p>{body}</p></body></html>"""


class _Server(http.server.HTTPServer):
    allow_reuse_address = False         # Windows would let another process share it
    expected_state = ""
    answer: dict | None = None


class _Callback(http.server.BaseHTTPRequestHandler):
    timeout = 15                        # a browser's idle pre-connect never blocks us

    def do_GET(self):  # noqa: N802 (http.server's name)
        url = urllib.parse.urlsplit(self.path)
        if url.path not in ("", "/"):
            self.send_error(404)
            return
        got = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if not got:
            self.send_error(404)
            return
        self.server.answer = got
        if got.get("state") != self.server.expected_state:
            head, body = ("Refused", "This answer was not for the sign-in Life CRM "
                                     "started. Nothing was connected.")
        elif got.get("error"):
            head, body = ("Not connected", "Google did not give access. You can "
                                           "close this tab.")
        else:
            head, body = ("Signed in", "You can close this tab and go back to the "
                                       "terminal.")
        page = _PAGE.format(head=head, body=body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):       # the request line carries the code
        pass


def authorize(client: dict, scopes: list[str], *, title: str, service: str,
              say=print, wait: float | None = None) -> dict:
    """Sign in once in the owner's browser; the token response (access_token,
    refresh_token, scope, id_token, ...). Raises DomainError when the owner
    says no, the answer is not this run's, or nobody answers in `wait`."""
    wait = WAIT if wait is None else wait
    verifier, challenge = pkce()
    state = _b64(os.urandom(24))
    server = _Server(("127.0.0.1", 0), _Callback)
    server.expected_state = state
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect,
        "response_type": "code", "scope": " ".join(scopes), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent"})
    try:
        say(f"Signing in to {title}: your browser opens Google's page — sign in there "
            f"and allow access (Life CRM never sees your password). If no browser "
            f"opens, paste this link into one on this computer:\n  {url}")
        try:
            _open_browser(url)
        except Exception:               # no browser at all: the printed link stands
            pass
        deadline = time.monotonic() + wait
        while server.answer is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise DomainError(f"no answer from the browser within {int(wait // 60) or 1}"
                                  f" minute(s) — nothing connected; run: python -m "
                                  f"lifecrm connect {service}")
            server.timeout = left
            server.handle_request()
    finally:
        server.server_close()
    got = server.answer
    if got.get("state") != state:
        raise DomainError("the browser's answer was not for this sign-in (its state "
                          "did not match) — refused, nothing connected; run connect "
                          "again")
    if got.get("error"):
        why = ("you chose not to allow it" if got["error"] == "access_denied"
               else f"Google said {got['error']}")
        raise DomainError(f"{title} not connected — {why}; run: python -m lifecrm "
                          f"connect {service}")
    if not got.get("code"):
        raise DomainError("Google's answer carried no code — nothing connected; run "
                          "connect again")
    form = {"grant_type": "authorization_code", "code": got["code"],
            "client_id": client["client_id"], "redirect_uri": redirect,
            "code_verifier": verifier}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    tokens = _token(form, title=title, service=service)
    if not tokens.get("refresh_token"):
        raise DomainError(
            f"Google sent no refresh token — remove the app's access at "
            f"myaccount.google.com/connections, then run: python -m lifecrm connect "
            f"{service}")
    return tokens


def granted(tokens: dict, requested: list[str]) -> set[str]:
    """The scopes the owner allowed — they may untick some on Google's page.
    An answer without `scope` granted what was asked (RFC 6749, 5.1)."""
    if "scope" not in tokens:
        return set(requested)
    return set(str(tokens.get("scope") or "").split())


def lifetime_note(tokens: dict, *, service: str) -> str | None:
    """What to tell the owner when Google says the sign-in will end on its
    own: an app still in Testing gets seven days, and the owner may grant
    time-limited access on the consent page. None when it lasts until
    revoked."""
    try:
        seconds = int(tokens.get("refresh_token_expires_in") or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    days = max(1, round(seconds / 86400))
    if days <= 7:
        return (f"Google gave this sign-in only {days} day(s): the app is still in "
                f"Testing. Publish it (Google Auth Platform › Audience › Publish app), "
                f"then connect again — docs/{service}.md")
    return (f"Google gave this sign-in {days} days (time-limited access) — after that "
            f"collect says to connect again")
