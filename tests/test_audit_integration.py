"""Seams left after the audit fixes merged: the web page's per-record 'who'
line never lists the owner as a third party, and observe's summary says
when stored rows were rewritten to a newer format."""

from lifecrm.web import api


def _p(name, entity_id, role="participant"):
    return {"role": role, "kind": "email", "value": f"{name}@example.com",
            "display_name": name, "entity_id": entity_id, "entity": name}


def test_a_calendar_event_lists_everyone_but_the_owner():
    parts = [_p("Alex Rivera", 1, "organizer"), _p("Dr. Rebecca Ellis", 6)]
    assert api._who("calendar_event", None, "calendar", parts, frozenset({1})) \
        == "Dr. Rebecca Ellis"


def test_an_event_with_only_the_owner_reads_you():
    assert api._who("calendar_event", None, "calendar",
                    [_p("Alex Rivera", 1, "organizer")], frozenset({1})) == "you"


def test_without_a_known_owner_everyone_is_listed():
    parts = [_p("Alex Rivera", 1, "organizer"), _p("Dr. Rebecca Ellis", 6)]
    assert api._who("calendar_event", None, "calendar", parts) \
        == "Alex Rivera, Dr. Rebecca Ellis"


def test_directed_messages_are_unchanged():
    parts = [_p("Jake Morales", 2, "sender"), _p("Alex Rivera", 1, "recipient")]
    assert api._who("android_sms", "in", "messages", parts, frozenset({1})) \
        == "Jake Morales → you"


def test_the_observe_summary_counts_format_upgrades():
    from lifecrm.cli import _observe_summary
    scan = {"connector": "ios_messages", "records": []}
    plan = {"streams": [], "new": [], "updates": [], "refreshed": [], "identical": 0,
            "dup_incoming": 0, "conflicts": [], "invalid": [],
            "format_upgrades": [("row", "{}"), ("row2", "{}")]}
    assert _observe_summary(scan, plan)["format_upgrades"] == 2
