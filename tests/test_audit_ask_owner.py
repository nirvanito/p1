"""Audit round: who "you" are, when no import says it on its own.

An iPhone Home calendar is the case that failed: its account is
'calendar:Home', its events carry no direction, so following the import's
own advice (`handles link email:<you> --new "Alex Rivera"`) still left the
owner a third party — 'with Alex Rivera, Dr. Rebecca Ellis' in ask, first
on the People page, a contact in the review pack. config.toml's [owner]
handles say it once, and lifecrm/owner.py is the one rule all three read.
"""

import dataclasses
from datetime import date

import pytest

from lifecrm import owner, recall, review
from lifecrm.config import ConfigError, load_config
from lifecrm.domain import entities, observations as obs
from lifecrm.web import api
from test_ask_demo import _apply, _rec, _scan

TODAY = date(2026, 10, 21)
ME_EMAIL = "alex.rivera@example.com"
ELLIS = "frontdesk@ellisdental.example"
SAM = "sam.okafor@example.com"


def with_owner(cfg, **table):
    return dataclasses.replace(cfg, extra={**cfg.extra, "owner": table})


@pytest.fixture
def home_calendar(conn, cfg):
    """A calendar-only root: the Home calendar, nothing else imported."""
    ids = {name: entities.create(conn, "person", name)["id"]
           for name in ("Alex Rivera", "Dr. Rebecca Ellis", "Sam Okafor")}

    def event(uid, day, title, *guests):
        start = f"{day}T15:00:00Z"
        parts = [{"kind": "email", "value": ME_EMAIL, "role": "organizer"},
                 {"kind": "email", "value": ME_EMAIL, "role": "participant"}]
        parts += [{"kind": "email", "value": g, "role": "participant"} for g in guests]
        return _rec(cfg, "calendar_event", uid, start, ended_at=f"{day}T16:00:00Z",
                    title=title, participants=parts)
    _apply(conn, _scan("calendar",
                       [event("dentist", "2026-10-15", "Dentist: Dr. Ellis", ELLIS),
                        event("lunch", "2026-10-16", "Lunch with Sam", SAM)],
                       [{"kind": "email", "value": ME_EMAIL, "display_name": "Alex Rivera"},
                        {"kind": "email", "value": ELLIS, "display_name": None},
                        {"kind": "email", "value": SAM, "display_name": "Sam Okafor"}],
                       account="calendar:Home", source_file="Home.ics"))
    # exactly what the calendar import's report tells the owner to do
    obs.link_handle(conn, "email", ME_EMAIL, ids["Alex Rivera"])
    obs.link_handle(conn, "email", ELLIS, ids["Dr. Rebecca Ellis"])
    obs.link_handle(conn, "email", SAM, ids["Sam Okafor"])
    conn.commit()
    return ids


def _ask(conn, cfg, question):
    bundle = recall.build_evidence(conn, cfg, question, today=TODAY)
    return bundle, recall.deterministic_answer(bundle)


def _owner_rows(bundle):
    event = next(o for o in bundle["observations"] if o["stream"] == "calendar_event")
    return [p for p in event["participants"] if p["handle"] == f"email:{ME_EMAIL}"]


def _review_people(conn, cfg):
    pack = review.build_pack(conn, cfg, review.resolve_period(cfg, today=TODAY),
                             lens="relationships")
    return [p["name"] for p in pack["relationships"]["people"]]


def test_without_owner_handles_a_calendar_alone_cannot_say_who_you_are(
        conn, cfg, home_calendar):
    """Why the setting exists: nothing in this import names its owner."""
    bundle, text = _ask(conn, cfg, "when did I last see Dr. Ellis")
    assert not any(p["is_owner"] for p in _owner_rows(bundle))
    assert "Alex Rivera" in text
    assert "Alex Rivera" in [p["name"] for p in api.people(conn, cfg)["people"]]


def test_owner_handles_make_the_linked_entity_you_in_ask_people_and_review(
        conn, cfg, home_calendar):
    ids = home_calendar
    mine = with_owner(cfg, name="Alex Rivera", handles=[f"email:{ME_EMAIL}"])
    for question, other in (("when did I last see Dr. Ellis", "Dr. Rebecca Ellis"),
                            ("when did I last see Sam", "Sam Okafor")):
        bundle, text = _ask(conn, mine, question)
        rows = _owner_rows(bundle)
        assert rows and all(p["is_owner"] and p["name"] == "you" for p in rows)
        assert f"with {other} [" in text, text
        assert "Alex Rivera" not in text and ME_EMAIL not in text
    people = [p["name"] for p in api.people(conn, mine)["people"]]
    assert "Alex Rivera" not in people and people[:2] == ["Sam Okafor",
                                                           "Dr. Rebecca Ellis"]
    me = api.person(conn, mine, ids["Alex Rivera"])
    assert me["is_owner"] is True and me["last_contact"] is None
    assert api.person(conn, mine, ids["Sam Okafor"])["is_owner"] is False
    assert "Alex Rivera" in _review_people(conn, cfg)
    assert "Alex Rivera" not in _review_people(conn, mine)
    assert {"Sam Okafor", "Dr. Rebecca Ellis"} <= set(_review_people(conn, mine))


def test_a_configured_handle_is_you_even_before_it_is_linked(conn, cfg):
    _apply(conn, _scan("calendar", [_rec(
        cfg, "calendar_event", "solo", "2026-10-15T15:00:00Z", title="Dentist: Dr. Ellis",
        participants=[{"kind": "email", "value": ME_EMAIL, "role": "organizer"},
                      {"kind": "email", "value": ELLIS, "role": "participant"}])],
        [{"kind": "email", "value": ME_EMAIL, "display_name": None},
         {"kind": "email", "value": ELLIS, "display_name": None}],
        account="calendar:Home", source_file="Home.ics"))
    conn.commit()
    mine = with_owner(cfg, handles=[f"email:{ME_EMAIL.upper()}"])
    bundle, text = _ask(conn, mine, "what's on my calendar")
    assert all(p["is_owner"] for p in _owner_rows(bundle))
    assert ME_EMAIL not in text and f"email:{ELLIS}" in text


def test_the_rule_is_one_helper_with_the_old_rules_kept(conn, cfg):
    """An outgoing sender and an import's account still count without any
    configuration, and a database without the observed tier has only the
    configured handles."""
    phone = "+15125550100"
    text_out = _rec(cfg, "android_sms", "out-1", "2026-10-15T15:00:00Z",
                    direction="out", body="on my way",
                    participants=[{"kind": "phone", "value": phone, "role": "sender"},
                                  {"kind": "phone", "value": "+15125550142",
                                   "role": "recipient"}])
    _apply(conn, _scan("android_sms", [text_out],
                       [{"kind": "phone", "value": phone, "display_name": None},
                        {"kind": "phone", "value": "+15125550142", "display_name": None}],
                       source_file="sms.xml"))
    _apply(conn, _scan("ai_chat", [], [], account="me@example.com",
                       source_file="chatgpt.zip"))
    me = entities.create(conn, "person", "Alex Rivera")["id"]
    obs.link_handle(conn, "phone", phone, me)
    conn.commit()
    ident = owner.identity(conn, cfg)
    assert ("phone", phone) in ident.handles and ident.entities == {me}
    assert "me@example.com" in ident.accounts and ident.configured == frozenset()
    assert owner.identity(conn, with_owner(cfg, handles=["phone:(512) 555-0199"])) \
        .configured == {("phone", "+15125550199")}


@pytest.mark.parametrize("table, says", [
    ("alex@example.com", "must be a table"),
    ({"handles": "alex@example.com"}, "is not 'kind:value'"),
    ({"handles": [42]}, "must be 'kind:value' strings"),
    ({"handles": {"email": "alex@example.com"}}, "must be a list"),
    ({"handles": ["fax:5125550100"]}, "unknown kind 'fax'"),
    ({"handles": ["phone:call me"]}, "no phone number"),
    ({"handle": ["email:alex@example.com"]}, "no setting 'handle'"),
    ({"name": "", "handles": []}, "name must be"),
])
def test_a_wrong_owner_table_is_one_line_naming_the_fix(cfg, table, says):
    bad = dataclasses.replace(cfg, extra={"owner": table})
    with pytest.raises(ConfigError) as e:
        owner.configured(bad)
    message = str(e.value)
    assert says in message and "[owner]" in message and "\n" not in message


def test_owner_reads_from_config_toml_and_absent_is_fine(tmp_path, cfg):
    assert owner.configured(cfg) == (None, frozenset())
    (tmp_path / "config.toml").write_text(
        '[owner]\nname = "Alex Rivera"\n'
        'handles = ["email:Alex.Rivera@Example.com", "phone:512-555-0100"]\n',
        encoding="utf-8")
    loaded = load_config(tmp_path)
    assert owner.configured(loaded) == (
        "Alex Rivera", frozenset({("email", ME_EMAIL), ("phone", "+15125550100")}))


def test_the_example_config_documents_the_table_commented_out():
    """Commented out: load_config falls back to the example, and its sample
    address must never be taken for anyone's."""
    import tomllib
    from lifecrm.config import PROJECT_ROOT
    text = (PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert "# [owner]" in text and '# handles = ["email:' in text
    assert "owner" not in tomllib.loads(text)


def test_recall_does_not_reuse_one_configs_owner_for_another(conn, cfg, home_calendar):
    plain, _ = _ask(conn, cfg, "when did I last see Dr. Ellis")
    mine, _ = _ask(conn, with_owner(cfg, handles=[f"email:{ME_EMAIL}"]),
                   "when did I last see Dr. Ellis")
    again, _ = _ask(conn, cfg, "when did I last see Dr. Ellis")
    assert [p["is_owner"] for p in _owner_rows(plain)] == \
        [p["is_owner"] for p in _owner_rows(again)] != \
        [p["is_owner"] for p in _owner_rows(mine)]


def test_a_week_of_calendar_ask_names_no_owner(conn, cfg, home_calendar):
    mine = with_owner(cfg, handles=[f"email:{ME_EMAIL}"])
    for question in ("what's on my calendar last week", "what meetings did I have",
                     "what events did I have last week"):
        bundle, text = _ask(conn, mine, question)
        assert bundle["observations"], question
        assert "Alex Rivera" not in text and ME_EMAIL not in text, (question, text)
