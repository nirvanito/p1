"""Gift ideas and anniversaries: two small shapes an entry's data may carry.

    data.gift         true, or {"status": "idea" | "given" | "dropped",
                                "occasion": "birthday", "note": "..."}
                      on an entry that mentions the person it is for
    data.anniversary  {"date": "2019-06-08" (or "--06-08" with no year),
                       "what": "wedding"} on an entry that mentions the
                      people it belongs to

Monica keeps gift ideas per contact and reminds you of dates that matter;
here both are ordinary entries you confirmed, and lifecrm/gifts.py reads
them beside the birthday nudge and as an `anniversary` nudge. The shape is
checked here, on every write path, whether or not the feature is checked.
Pure rules, no database.
"""

from __future__ import annotations

from datetime import date

from .util import DomainError

GIFT_KEYS = frozenset({"status", "occasion", "note"})
GIFT_STATUSES = ("idea", "given", "dropped")
ANNIVERSARY_KEYS = frozenset({"date", "what"})


def parse_day(value) -> tuple[int | None, int, int] | None:
    """'YYYY-MM-DD' or '--MM-DD' -> (year or None, month, day); None otherwise."""
    if not isinstance(value, str):
        return None
    try:
        if value.startswith("--") and len(value) == 7:
            m, d = int(value[2:4]), int(value[5:7])
            date(2000, m, d)                    # a leap year: Feb 29 is valid
            return None, m, d
        d_ = date.fromisoformat(value)
        return d_.year, d_.month, d_.day
    except ValueError:
        return None


def validate_gift(gift) -> dict:
    if gift is True:
        return {"status": "idea"}
    if not isinstance(gift, dict):
        raise DomainError('data.gift must be true or an object like {"status": "idea", '
                          '"occasion": "birthday"}')
    unknown = sorted(set(gift) - GIFT_KEYS)
    if unknown:
        raise DomainError(f"data.gift has unknown field(s) {', '.join(unknown)} — known: "
                          f"{', '.join(sorted(GIFT_KEYS))}")
    status = gift.get("status", "idea")
    if status not in GIFT_STATUSES:
        raise DomainError(f"data.gift.status must be one of {', '.join(GIFT_STATUSES)}, "
                          f"got {status!r}")
    for key in ("occasion", "note"):
        v = gift.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > 200):
            raise DomainError(f"data.gift.{key} must be text of 200 characters or fewer")
    return {**gift, "status": status}


def validate_anniversary(ann) -> dict:
    if not isinstance(ann, dict):
        raise DomainError('data.anniversary must be an object like {"date": "2019-06-08", '
                          '"what": "wedding"}')
    unknown = sorted(set(ann) - ANNIVERSARY_KEYS)
    if unknown:
        raise DomainError(f"data.anniversary has unknown field(s) {', '.join(unknown)} — "
                          f"known: date, what")
    if parse_day(ann.get("date")) is None:
        raise DomainError(f"data.anniversary.date must be YYYY-MM-DD, or --MM-DD when the "
                          f"year is not known, got {ann.get('date')!r}")
    what = ann.get("what")
    if what is not None and (not isinstance(what, str) or not what.strip() or len(what) > 80):
        raise DomainError("data.anniversary.what must be a short name like \"wedding\"")
    return dict(ann)


def validate_entry_data(kind: str | None, data: dict) -> None:
    if "gift" in data:
        validate_gift(data["gift"])
    if "anniversary" in data:
        validate_anniversary(data["anniversary"])
