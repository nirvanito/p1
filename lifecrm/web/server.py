"""`lifecrm serve`: HTTP for the web UI, over a snapshot of the database.

stdlib `http.server`, not a Datasette plugin: Datasette's connections have no
sqlite3.Row factory (recall indexes rows by name everywhere), its plugin API
changes at 1.0, and the page needs a handful of JSON reads, not a table
browser. Datasette stays where it is good — the charts (`lifecrm dashboard`).

Three rules shape this file:

- **The live database is never held open.** A held-open file blocks restore's
  file swap on Windows. At start the live DB is copied through the SQLite
  backup API to data/web/lifecrm.db; afterwards, at most every few seconds, a
  request checks the live file's mtime and size — of the db AND its -wal,
  because a WAL commit only touches the -wal until the next checkpoint — and
  when they moved, takes a fresh snapshot beside the current one. The fresh
  one is swapped in only once no request is reading the old one, so a refresh
  never blocks a request and a request never sees a half-written file.
- **One read-only connection per request** (`db.connect(snap, readonly=True)`
  — mode=ro plus query_only). sqlite3 connections are not shared across
  threads, and a snapshot connection cannot write even by accident.
- **The only write is the question log.** With [web] log_questions (default
  on, --no-log to stop), each question asked lands in recall_log through
  domain/recall.log_question on a short-lived live connection — the same row
  `lifecrm ask` writes. There is no capture endpoint: logging a day stays the
  reviewed /log path (standing decision).

Requests are refused when they come from another site (Sec-Fetch-Site:
cross-site) or name a Host that is not this machine (a DNS-rebinding page
pointing a hostname at 127.0.0.1). An optional token makes every request
carry ?k=<token>, remembered by a cookie after the first visit. Plain http
on a LAN is still readable by anyone on that network: trusted networks only.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import db as db_mod
from .. import platforms, recall
from ..config import Config
from ..domain.util import DomainError, now_utc
from . import api, icons

PAGE = Path(__file__).parent / "index.html"
COOKIE = "lifecrm_k"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_QUESTION = 500
DEFAULT_PORT = 8765

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; "
       "manifest-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")

# Static files that hold nothing private: served without the token so a
# browser's manifest and icon fetches (which carry no cookie) still work.
PUBLIC = {"/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png",
          "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
          "/favicon.ico"}


class SnapshotError(RuntimeError):
    """The snapshot could not be taken or installed; the message says why."""


class Unavailable(RuntimeError):
    """A feature this machine cannot provide right now (503)."""


def web_settings(cfg: Config) -> dict:
    value = (cfg.extra or {}).get("web")
    return value if isinstance(value, dict) else {}


def key_query(token: str | None) -> str:
    """'?k=<token>' as it goes into a link, percent-encoded: the server reads
    the key back through parse_qs, which turns a raw '+' into a space and
    stops at '&' (and a browser drops everything after '#'), so a key of
    your own like 'a+b' would print a link that answers 401."""
    return f"?k={quote(token, safe='')}" if token else ""


def local_url(port: int, token: str | None) -> str:
    """The page on this computer, with the key when there is one."""
    return f"http://127.0.0.1:{port}/{key_query(token)}"


def flag(value, default: bool) -> bool:
    """A config boolean, strictly: the string "false" is false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, int):
        return bool(value)
    return default


# ---------------------------------------------------------------- snapshot

class Snapshot:
    """The server's private copy of the database, kept fresh without ever
    holding the live file open."""

    def __init__(self, live: Path, path: Path, refresh_seconds: float = 3.0):
        self.live = Path(live)
        self.path = Path(path)
        self.refresh_seconds = refresh_seconds
        self.taken_at: str | None = None
        self.refreshes = 0
        self.last_error: str | None = None
        self._lock = threading.Lock()          # readers, pending, stamps
        self._backing_up = threading.Lock()    # one backup at a time
        self._readers = 0
        self._stamp = None
        self._pending: Path | None = None
        self._pending_stamp = None
        self._checked = 0.0

    def _live_stamp(self) -> tuple:
        """(mtime, size) of the db and its -wal. An empty -wal counts as
        absent: a read-only open creates one (this server's own snapshot
        reads would otherwise look like a change), and it holds no commits."""
        out = []
        for p in (self.live, self.live.with_name(self.live.name + "-wal")):
            try:
                st = p.stat()
            except FileNotFoundError:
                out.append(None)
                continue
            out.append((st.st_mtime_ns, st.st_size) if st.st_size or p == self.live
                       else None)
        return tuple(out)

    def _backup(self) -> Path:
        """Copy the live DB to a fresh file next to the snapshot. Rollback
        journal mode afterwards, so a read-only connection to the copy never
        needs a -wal or -shm beside it."""
        tmp = self.path.with_name(f"{self.path.name}.next-{uuid.uuid4().hex[:8]}")
        src = db_mod.connect(self.live, readonly=True)
        try:
            db_mod.snapshot(src, tmp)
        finally:
            src.close()
        target = sqlite3.connect(str(tmp))
        try:
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
        return tmp

    def take(self) -> None:
        """The first snapshot, before the server accepts a request."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for stale in self.path.parent.glob(f"{self.path.name}.next-*"):
            stale.unlink(missing_ok=True)
        stamp = self._live_stamp()
        try:
            tmp = self._backup()
        except sqlite3.Error as e:
            raise SnapshotError(f"cannot snapshot {self.live}: {e}") from e
        try:
            os.replace(tmp, self.path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise SnapshotError(
                f"{self.path} is in use ({e.strerror or e}) — is another "
                f"`lifecrm serve` running on this root?") from e
        for side in ("-wal", "-shm", "-journal"):
            self.path.with_name(self.path.name + side).unlink(missing_ok=True)
        self._stamp = stamp
        self.taken_at = now_utc()

    @contextmanager
    def reader(self):
        """A read-only connection to the current snapshot, for one request."""
        self._maybe_refresh()
        with self._lock:
            self._readers += 1
            path = self.path
        try:
            conn = db_mod.connect(path, readonly=True)
            try:
                yield conn
            finally:
                conn.close()
        finally:
            with self._lock:
                self._readers -= 1
            self._swap_if_idle()

    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        with self._lock:
            due = now - self._checked >= self.refresh_seconds
            if due:
                self._checked = now
        if due:
            stamp = self._live_stamp()
            with self._lock:
                fresh = stamp in (self._stamp, self._pending_stamp)
            # Non-blocking: if another request is already copying, this one
            # reads the current snapshot rather than waiting.
            if not fresh and self._backing_up.acquire(blocking=False):
                try:
                    tmp = self._backup()
                    with self._lock:
                        old = self._pending
                        self._pending, self._pending_stamp = tmp, stamp
                    if old is not None:
                        old.unlink(missing_ok=True)
                    self.last_error = None
                except (sqlite3.Error, OSError) as e:
                    # A locked or mid-restore live file: keep serving the
                    # snapshot we have and try again on a later request.
                    self.last_error = f"{type(e).__name__}: {e}"
                finally:
                    self._backing_up.release()
        self._swap_if_idle()

    def _swap_if_idle(self) -> None:
        with self._lock:
            if self._pending is None or self._readers:
                return
            try:
                os.replace(self._pending, self.path)
            except OSError as e:
                # Something else has the file open (an indexer, antivirus);
                # the swap is retried on the next request.
                self.last_error = f"swap deferred: {e}"
                return
            self._stamp = self._pending_stamp
            self._pending = self._pending_stamp = None
            self.taken_at = now_utc()
            self.refreshes += 1

    def close(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            pending.unlink(missing_ok=True)


# ---------------------------------------------------------------- the app

class WebApp:
    """Settings and shared state for one server: the snapshot, the token,
    the lazily loaded whisper model, cached probes."""

    def __init__(self, cfg: Config, *, token: str | None = None,
                 log_questions: bool | None = None, use_llm: bool | None = None,
                 refresh_seconds: float | None = None, verbose: bool = False):
        web = web_settings(cfg)
        self.cfg = cfg
        self.token = token or None
        self.log_questions = (flag(web.get("log_questions"), True)
                              if log_questions is None else log_questions)
        self.use_llm = flag(web.get("use_llm"), True) if use_llm is None else use_llm
        self.verbose = verbose
        self.snapshot = Snapshot(
            cfg.db_path, cfg.data_dir / "web" / "lifecrm.db",
            float(web.get("refresh_seconds", 3)) if refresh_seconds is None
            else refresh_seconds)
        self.whisper_model = str(web.get("model", "small"))
        self.dashboard_port = int(web.get("dashboard_port", 8001))
        self._extra_hosts = {str(h).lower() for h in web.get("allowed_hosts", []) or []}
        self._own_names: set[str] | None = None
        self.voice_dir = cfg.data_dir / "web" / "voice"
        self.started_at = now_utc()
        self._model = None
        self._model_lock = threading.Lock()
        self._transcribe_lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._llm_state: tuple[float, dict] | None = None
        self._dash_state: dict[str, tuple[float, bool]] = {}

    def host_allowed(self, name: str) -> bool:
        """A Host name that is this machine's own, or listed in [web]
        allowed_hosts. Looked up on first need: getfqdn can stall for seconds
        on Windows, and most requests carry an IP address."""
        if name in self._extra_hosts:
            return True
        if self._own_names is None:
            hostname = socket.gethostname().lower()
            names = {hostname, f"{hostname}.local"}
            try:
                names.add(socket.getfqdn().lower())
            except OSError:
                pass
            self._own_names = names
        return name in self._own_names

    # ---- probes, cached so the status call stays cheap

    def llm_state(self) -> dict:
        if not self.use_llm:
            return {"available": False, "reason": "local model off for the web UI "
                                                  "(--no-llm or [web] use_llm=false)"}
        from .. import llm as llm_mod
        with self._probe_lock:
            if self._llm_state and time.monotonic() - self._llm_state[0] < 30:
                return self._llm_state[1]
        state = llm_mod.status(self.cfg)
        with self._probe_lock:
            self._llm_state = (time.monotonic(), state)
        return state

    def dashboards_running(self, host: str = "127.0.0.1") -> bool:
        """Whether Datasette answers on `host` — the name the browser used for
        this page, which is where the page's link to it will point.
        `lifecrm dashboard` listens on 127.0.0.1 unless told otherwise, so a
        probe of the laptop's own loopback says nothing about the phone,
        whose link goes to the laptop's LAN address."""
        with self._probe_lock:
            cached = self._dash_state.get(host)
            if cached and time.monotonic() - cached[0] < 10:
                return cached[1]
        try:
            with socket.create_connection((host, self.dashboard_port), timeout=0.25):
                running = True
        except OSError:
            running = False
        with self._probe_lock:
            self._dash_state[host] = (time.monotonic(), running)
        return running

    def dashboards_state(self, host: str) -> dict:
        """The Health page's link to the full dashboards: shown only when they
        answer at the address the page would link to; `local_only` when they
        run on this computer but not there (a phone, Datasette on loopback)."""
        running = self.dashboards_running(host)
        local_only = (not running and not is_loopback(host)
                      and self.dashboards_running("127.0.0.1"))
        return {"port": self.dashboard_port, "running": running,
                "local_only": local_only}

    # ---- voice

    def whisper(self):
        """The faster-whisper model, loaded on first use and kept."""
        from .. import transcribe as transcribe_mod
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                self._model = transcribe_mod.load_model(self.cfg, self.whisper_model)
            except ImportError as e:
                raise Unavailable(
                    "voice questions need faster-whisper, which is not installed "
                    "in this Python — pip install faster-whisper, or use your "
                    "keyboard's dictation mic instead") from e
            except Exception as e:
                raise Unavailable(
                    f"could not load whisper model '{self.whisper_model}' "
                    f"({type(e).__name__}: {e}) — the first load downloads it, so "
                    f"it needs the network once; or set [web] model to a model "
                    f"you have") from e
            return self._model

    # ---- the one write

    def log_question(self, question: str, result: dict) -> bool:
        if not self.log_questions:
            return False
        from ..domain import recall as recall_domain
        try:
            conn = db_mod.connect(self.cfg.db_path)
            try:
                recall_domain.log_question(
                    conn, question, answered=bool(result["evidence"]["has_evidence"]),
                    answer_summary=result["answer"])
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.Error, DomainError) as e:
            # The answer is already computed; a locked database must not
            # turn it into an error.
            print(f"warning: question not logged ({e})", file=sys.stderr)
            return False
        return True


# ---------------------------------------------------------------- HTTP

def _one(query: dict, name: str, default: str | None = None) -> str | None:
    values = query.get(name)
    return values[0] if values else default


def _int(query: dict, name: str, default: int | None, lo: int, hi: int) -> int | None:
    raw = _one(query, name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got '{raw}'") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be {lo}-{hi}, got {value}")
    return value


class Handler(BaseHTTPRequestHandler):
    app: WebApp                                  # set per server by make_server
    server_version = "lifecrm"
    sys_version = ""

    # ---- plumbing

    def log_message(self, fmt, *args):           # paths only: never questions or tokens
        if self.app.verbose:
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path} "
                             f"{args[1] if len(args) > 1 else ''}\n")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send(self, status: int, body: bytes, ctype: str,
              headers: tuple = (), cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self._cookie:
            # Encoded like the link: a raw ';', ',' or space would end the
            # cookie's value early.
            self.send_header("Set-Cookie", f"{COOKIE}={quote(self.app.token, safe='')}; "
                                           f"Path=/; HttpOnly; SameSite=Lax; "
                                           f"Max-Age=31536000")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message, "status": status})

    # ---- gatekeeping

    def _host_name(self) -> str:
        """The name in the request's Host header, without its port: what the
        browser calls this machine (and what location.hostname is)."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host[1:host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name.rstrip(".")

    def _host_ok(self) -> bool:
        if not (self.headers.get("Host") or "").strip():
            return True
        name = self._host_name()
        if name == "localhost" or name.endswith(".localhost") or name.endswith(".local"):
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return self.app.host_allowed(name)

    def _token_ok(self, query: dict) -> bool:
        token = self.app.token
        if not token:
            return True
        given = _one(query, "k")
        if given is not None and hmac.compare_digest(given.encode(), token.encode()):
            self._cookie = True
            return True
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return False
        held = jar.get(COOKIE)
        return held is not None and hmac.compare_digest(unquote(held.value).encode(),
                                                        token.encode())

    def _authorized_key(self, query: dict) -> str | None:
        """The token, when this request proved it holds it — so the page can
        hand it on to the manifest's start_url (home-screen launches)."""
        return self.app.token if self.app.token and self._token_ok(query) else None

    # ---- dispatch

    def _dispatch(self, method: str) -> None:
        self._cookie = False
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query, keep_blank_values=True)
        try:
            if not self._host_ok():
                return self._error(403, f"host '{self.headers.get('Host')}' is not this "
                                        f"machine — add it to [web] allowed_hosts")
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site == "cross-site" and path.startswith("/api/"):
                return self._error(403, "cross-site request refused")
            if path not in PUBLIC and not self._token_ok(query):
                return self._error(401, "this server needs its key — open the link "
                                        "`lifecrm serve` printed (it ends in ?k=...)")
            for verb, pattern, fn in ROUTES:
                m = pattern.match(path)
                if m is None:
                    continue
                if verb != method:
                    return self._error(405, f"{path} accepts {verb} only")
                return fn(self, query, *m.groups())
            return self._error(404, f"no such endpoint: {path}")
        except api.NotFound as e:
            return self._error(404, str(e))
        except Unavailable as e:
            return self._error(503, str(e))
        except (ValueError, DomainError) as e:
            return self._error(400, str(e))
        except sqlite3.Error as e:
            return self._error(500, f"database error: {e}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception as e:                   # a bug: loud on stderr, JSON to the page
            traceback.print_exc()
            return self._error(500, f"internal error: {type(e).__name__}: {e}")

    # ---- pages and static files

    def page(self, query):
        key = self._authorized_key(query)
        manifest = "/manifest.webmanifest" + key_query(key)
        html = PAGE.read_text(encoding="utf-8").replace("{{MANIFEST}}", manifest)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                   headers=(("Content-Security-Policy", CSP),))

    def manifest(self, query):
        key = self._authorized_key(query)
        body = {
            "name": "Life CRM", "short_name": "Life CRM",
            "description": "Your life database: ask with citations, timeline, "
                           "people, loops, health.",
            "start_url": "/" + key_query(key), "scope": "/",
            "display": "standalone", "background_color": "#0f1115",
            "theme_color": "#4f46e5",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
                 "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
                {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }
        self._send(200, json.dumps(body).encode("utf-8"),
                   "application/manifest+json; charset=utf-8")

    def icon_svg(self, query):
        self._send(200, icons.SVG.encode("utf-8"), "image/svg+xml",
                   cache="max-age=86400")

    def icon_png(self, query, size: str):
        self._send(200, icons.png(int(size)), "image/png", cache="max-age=86400")

    def apple_icon(self, query, _suffix=None):
        self._send(200, icons.png(180), "image/png", cache="max-age=86400")

    def favicon(self, query):
        self._send(200, icons.png(48), "image/png", cache="max-age=86400")

    # ---- the JSON API

    def _read(self, fn, *args):
        with self.app.snapshot.reader() as conn:
            return fn(conn, self.app.cfg, *args)

    def _probe_host(self) -> str:
        """Where the dashboards link would point, as an address to probe:
        loopback for this computer's own names, the Host name otherwise."""
        name = self._host_name()
        if not name or name == "localhost" or name.endswith(".localhost"):
            return "127.0.0.1"
        return name

    def status(self, query):
        out = self._read(api.status)
        snap = self.app.snapshot
        out.update({
            "snapshot": {"taken_at": snap.taken_at, "refreshes": snap.refreshes,
                         "error": snap.last_error},
            "started_at": self.app.started_at,
            "log_questions": self.app.log_questions,
            "llm": self.app.llm_state(),
            "voice": {"model": self.app.whisper_model,
                      "loaded": self.app._model is not None},
            "dashboards": self.app.dashboards_state(self._probe_host()),
            "token": bool(self.app.token),
        })
        self._json(200, out)

    def ask(self, query):
        question = (_one(query, "q") or "").strip()
        if not question:
            raise ValueError('ask what? pass ?q=, e.g. /api/ask?q=how+did+I+sleep+last+week')
        if len(question) > MAX_QUESTION:
            raise ValueError(f"question is {len(question)} characters — keep it under "
                             f"{MAX_QUESTION}")
        days = _int(query, "days", None, 1, 3650)
        use_llm = self.app.use_llm and _one(query, "llm", "1") not in ("0", "false", "no")
        warnings: list[str] = []
        with self.app.snapshot.reader() as conn:
            result = recall.answer(conn, self.app.cfg, question, days=days,
                                   use_llm=use_llm, warn=warnings.append)
        result["warnings"] = warnings
        result["index"] = recall.evidence_index(result["evidence"])
        result["speech"] = api.speakable(result["answer"])
        result["logged"] = self.app.log_question(question, result)
        self._json(200, result)

    def day(self, query):
        d = _one(query, "date") or api.today(self.app.cfg)
        self._json(200, self._read(api.day, d))

    def days(self, query):
        from datetime import date, timedelta
        end = _one(query, "end") or api.today(self.app.cfg)
        start = _one(query, "start") or (
            date.fromisoformat(api.parse_date(end, "end")) - timedelta(days=34)).isoformat()
        with self.app.snapshot.reader() as conn:
            self._json(200, api.days(conn, start, end))

    def people(self, query):
        self._json(200, self._read(api.people))

    def person(self, query, raw_id: str):
        try:
            entity_id = int(raw_id)
        except ValueError:
            raise ValueError(f"person id must be a number, got '{raw_id}'") from None
        self._json(200, self._read(api.person, entity_id))

    def entry(self, query, entry_uuid: str):
        self._json(200, self._read(api.entry, entry_uuid))

    def observation(self, query, obs_uuid: str):
        self._json(200, self._read(api.observation, obs_uuid))

    def loops(self, query):
        self._json(200, self._read(api.loops))

    def imports(self, query):
        self._json(200, self._read(api.imports))

    def health(self, query):
        days = _int(query, "days", 14, 1, 366)
        self._json(200, self._read(api.health, days, _one(query, "end") or None))

    def transcribe(self, query):
        """Recorded audio in the body -> its text. The page then asks it."""
        from .. import transcribe as transcribe_mod
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ValueError("POST the recorded audio as the request body")
        if length > MAX_AUDIO_BYTES:
            return self._error(413, f"audio is {length // 1024 // 1024} MB — a "
                                    f"question should be under "
                                    f"{MAX_AUDIO_BYTES // 1024 // 1024} MB")
        audio = self.rfile.read(length)
        model = self.app.whisper()                     # 503 when unavailable
        ctype = (self.headers.get("Content-Type") or "").lower()
        ext = next((e for k, e in (("webm", ".webm"), ("ogg", ".ogg"),
                                   ("mp4", ".mp4"), ("m4a", ".m4a"), ("aac", ".m4a"),
                                   ("mpeg", ".mp3"), ("wav", ".wav")) if k in ctype),
                   ".audio")
        self.app.voice_dir.mkdir(parents=True, exist_ok=True)
        path = self.app.voice_dir / f"question-{uuid.uuid4().hex}{ext}"
        path.write_bytes(audio)
        try:
            prompt = None
            if self.app.cfg.whisper.get("seed_initial_prompt", True):
                with self.app.snapshot.reader() as conn:
                    prompt = transcribe_mod.entity_prompt(conn)
            with self.app._transcribe_lock:
                result = transcribe_mod.transcribe_audio(self.app.cfg, model, path,
                                                         initial_prompt=prompt)
        except Unavailable:
            raise
        except Exception as e:
            raise ValueError(f"could not transcribe that recording "
                             f"({type(e).__name__}: {e})") from e
        finally:
            path.unlink(missing_ok=True)       # the question's audio is never kept
        self._json(200, {"text": result["text"].strip(),
                         "language": result.get("language"),
                         "duration_sec": result.get("duration_sec"),
                         "model": self.app.whisper_model})


ROUTES = [
    ("GET", re.compile(r"^/$"), Handler.page),
    ("GET", re.compile(r"^/index\.html$"), Handler.page),
    ("GET", re.compile(r"^/manifest\.webmanifest$"), Handler.manifest),
    ("GET", re.compile(r"^/icon\.svg$"), Handler.icon_svg),
    ("GET", re.compile(r"^/icon-(192|512)\.png$"), Handler.icon_png),
    ("GET", re.compile(r"^/apple-touch-icon(-precomposed)?\.png$"), Handler.apple_icon),
    ("GET", re.compile(r"^/favicon\.ico$"), Handler.favicon),
    ("GET", re.compile(r"^/api/status$"), Handler.status),
    ("GET", re.compile(r"^/api/ask$"), Handler.ask),
    ("GET", re.compile(r"^/api/day$"), Handler.day),
    ("GET", re.compile(r"^/api/days$"), Handler.days),
    ("GET", re.compile(r"^/api/people$"), Handler.people),
    ("GET", re.compile(r"^/api/person/([^/]+)$"), Handler.person),
    ("GET", re.compile(r"^/api/entry/([^/]+)$"), Handler.entry),
    ("GET", re.compile(r"^/api/observation/([^/]+)$"), Handler.observation),
    ("GET", re.compile(r"^/api/loops$"), Handler.loops),
    ("GET", re.compile(r"^/api/imports$"), Handler.imports),
    ("GET", re.compile(r"^/api/health$"), Handler.health),
    ("POST", re.compile(r"^/api/transcribe$"), Handler.transcribe),
]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows' SO_REUSEADDR lets a second server bind a port that is already
    # in use, and requests then land on either; refuse instead.
    allow_reuse_address = not platforms.is_windows()

    def server_bind(self):
        # HTTPServer.server_bind does a reverse-DNS lookup (getfqdn) that can
        # stall for seconds on Windows; the name is never used.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def server_close(self):
        super().server_close()
        app = getattr(self, "app", None)    # unset when binding itself failed
        if app is not None:
            app.snapshot.close()


def make_server(app: WebApp, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> Server:
    handler = type("LifeCRMHandler", (Handler,), {"app": app})
    server = Server((host, port), handler)
    server.app = app
    return server


def create_server(cfg: Config, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                  **opts) -> Server:
    """Snapshot first, then bind: a server that answers is always reading a
    complete copy."""
    app = WebApp(cfg, **opts)
    app.snapshot.take()
    return make_server(app, host, port)


# ---------------------------------------------------------------- banner

def lan_ip() -> str:
    """This machine's address on the LAN: the source address the OS would
    use to reach a non-local host. A UDP connect sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def banner(cfg: Config, host: str, port: int, token: str | None,
           log_questions: bool) -> list[str]:
    key = key_query(token)
    local = local_url(port, token) if host in ("0.0.0.0", "") or \
        is_loopback(host) else f"http://{host}:{port}/{key}"
    lines = [f"serving a read-only snapshot of {cfg.db_path} — refreshed when "
             f"the database changes; the live file is never held open",
             f"open: {local}"]
    if not is_loopback(host):
        ip = lan_ip() if host in ("0.0.0.0", "") else host
        lines.append(f"on your phone (same Wi-Fi): http://{ip}:{port}/{key}")
        lines.append("warning: no login and plain http — anyone on this network can "
                     "read it; trusted networks only"
                     + ("" if token else " (--token random requires a key)"))
        lines.append("  Windows: allow Python on Private networks when the firewall "
                     "asks · Mac: click Allow for incoming connections"
                     if platforms.is_windows() or platforms.is_mac() else
                     "  allow incoming connections on this port in your firewall")
    if token:
        lines.append("every request needs the key; a cookie remembers it after "
                     "the first visit")
    lines.append("questions are logged to recall_log (--no-log to stop)"
                 if log_questions else "questions are not logged (--no-log)")
    lines.append("Ctrl+C to stop")
    return lines
