"""The web server's own-name check never touches DNS. getfqdn's reverse
lookup stalled a request past 20 seconds on a Mac with no reverse DNS entry
(found by CI on macOS); the machine's own name, short or with .local, is
what a person types, and anything else goes in [web] allowed_hosts."""

import socket

import pytest

from lifecrm.web import server as web_server


def _server_class():
    for obj in vars(web_server).values():
        if isinstance(obj, type) and "host_allowed" in vars(obj):
            return obj
    pytest.fail("no server class with host_allowed")


def _bare(extra=()):
    obj = object.__new__(_server_class())
    obj._extra_hosts = set(extra)
    obj._own_names = None
    return obj


def test_no_dns_lookup_ever(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("host_allowed looked a name up in DNS")
    for fn in ("getfqdn", "gethostbyaddr", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, fn, refuse)
    monkeypatch.setattr(socket, "gethostname", lambda: "Alexs-MacBook.lan")
    s = _bare()
    for ok in ("alexs-macbook.lan", "alexs-macbook.lan.local", "alexs-macbook",
               "alexs-macbook.local"):
        assert s.host_allowed(ok), ok
    assert not s.host_allowed("evil.example")


def test_allowed_hosts_still_count(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    assert _bare(extra={"lifecrm.home.arpa"}).host_allowed("lifecrm.home.arpa")
