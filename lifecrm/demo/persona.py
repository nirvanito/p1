"""One synthetic life, told consistently across every data source.

Alex Rivera — a product manager in Austin — and six weeks of their life: a
board deck that gets assigned, worked on and presented; a passport renewed;
a partner's birthday; a best friend whose favorite restaurant is Veracruz
and who grew up in El Paso; a trip to Lake Bled, Slovenia four years ago and
a plan to go back. Every generator under `lifecrm/demo/sources/` renders this
same story in one app's export format, so a question answered from texts,
calendar, photos, health data and the journal comes out consistent.

Everything is fictional: example.com addresses, 555-01xx phone numbers.

Dates are offsets from an ANCHOR date (the demo's "today"), so the demo always
looks current. Offsets are <= 0 (the past). Times are local wall-clock times
in TIMEZONE. Nothing here is random at import time; the health series uses a
seeded generator so two builds of the same anchor are identical.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TIMEZONE = "America/Chicago"
WINDOW_DAYS = 42            # how far back the dense data goes
# Windows+Android(Samsung), Mac+iPhone, Windows+iPhone. `lifecrm demo --combo
# both` still builds the first two, as it always has; `--combo all` builds
# every one.
COMBOS = ("android", "iphone", "windows-iphone")
BOTH = ("android", "iphone")

# What each setup is made of. Generators branch on these (has_iphone, on_mac,
# browser) rather than on combo names: a phone decides some exports, the
# computer others.
DEVICE = {
    "android": {"phone": "Galaxy S24", "maker": "samsung", "computer": "Windows laptop",
                "phone_os": "android", "computer_os": "windows", "browser": "chrome"},
    "iphone": {"phone": "iPhone 15 Pro", "maker": "Apple", "computer": "MacBook",
               "phone_os": "ios", "computer_os": "macos", "browser": "safari"},
    "windows-iphone": {"phone": "iPhone 15 Pro", "maker": "Apple",
                       "computer": "Windows laptop", "phone_os": "ios",
                       "computer_os": "windows", "browser": "edge"},
}


def _device(combo: str) -> dict:
    try:
        return DEVICE[combo]
    except KeyError:
        raise ValueError(f"unknown combo '{combo}' (expected one of "
                         f"{', '.join(COMBOS)})") from None


def has_iphone(combo: str) -> bool:
    """The phone is an iPhone (iphone, windows-iphone)."""
    return _device(combo)["phone_os"] == "ios"


def on_mac(combo: str) -> bool:
    """The computer is a Mac (iphone)."""
    return _device(combo)["computer_os"] == "macos"


def on_windows(combo: str) -> bool:
    """The computer is a Windows laptop (android, windows-iphone)."""
    return _device(combo)["computer_os"] == "windows"


def browser(combo: str) -> str:
    """The laptop's browser: 'chrome', 'edge' or 'safari'."""
    return _device(combo)["browser"]


# ---------------------------------------------------------------- people & places

@dataclass(frozen=True)
class Person:
    key: str
    name: str                  # canonical entity name
    first: str
    last: str
    phone: str | None          # E.164
    email: str | None
    relation: str
    city: str | None           # where they live now
    hometown: str | None       # where they are from
    birthday: tuple[int, int] | None = None   # (month, day); None = unknown
    birth_year: int | None = None
    aliases: tuple[str, ...] = ()
    org: str | None = None
    note: str | None = None    # what a contact card's Notes field would hold


OWNER = Person(
    key="me", name="Alex Rivera", first="Alex", last="Rivera",
    phone="+15125550100", email="alex.rivera@example.com", relation="self",
    city="Austin, TX", hometown="San Antonio, TX", birthday=(4, 2),
    birth_year=1994, org="Acme Robotics")

PEOPLE: tuple[Person, ...] = (
    Person("jake", "Jake Morales", "Jake", "Morales", "+15125550142",
           "jake.morales@example.com", "best friend", "Austin, TX",
           "El Paso, TX", (3, 14), 1993, ("Jakey",),
           note="Grew up in El Paso. Favorite restaurant: Veracruz All Natural."),
    Person("maya", "Maya Chen", "Maya", "Chen", "+15125550187",
           "maya.chen@example.com", "partner", "Austin, TX", "Portland, OR",
           None, 1995, (), org="Austin Clay Studio"),
    Person("mom", "Linda Rivera", "Linda", "Rivera", "+12105550110",
           "linda.rivera@example.com", "mother", "San Antonio, TX",
           "San Antonio, TX", (11, 30), 1966, ("Mom",)),
    Person("priya", "Priya Natarajan", "Priya", "Natarajan", "+15125550123",
           "priya@acme-robotics.example", "manager", "Austin, TX",
           "Chennai, India", None, None, (), org="Acme Robotics"),
    Person("sam", "Sam Okafor", "Sam", "Okafor", "+15125550166",
           "sam.okafor@example.com", "gym buddy", "Austin, TX", "Houston, TX",
           (8, 21), 1992, ()),
    Person("ellis", "Dr. Rebecca Ellis", "Rebecca", "Ellis", "+15125550199",
           "frontdesk@ellisdental.example", "dentist", "Austin, TX", None,
           None, None, ("Dr. Ellis",), org="Ellis Dental"),
)

# Maya's birthday falls inside the demo window, whatever the anchor.
MAYA_BIRTHDAY_OFFSET = -9
# Sam's falls a few days after it, whatever the anchor, so the morning's
# nudges (docs/assistant.md) always have a birthday coming up.
SAM_BIRTHDAY_OFFSET = 4


@dataclass(frozen=True)
class Place:
    key: str
    name: str
    kind: str                  # restaurant, gym, park, office, store, landmark, city
    lat: float
    lon: float
    city: str
    aliases: tuple[str, ...] = ()
    address: str | None = None


PLACES: tuple[Place, ...] = (
    Place("movement", "Movement Gym", "gym", 30.2849, -97.7341, "Austin, TX",
          ("Movement", "the gym"), "2600 E 5th St, Austin, TX"),
    Place("veracruz", "Veracruz All Natural", "restaurant", 30.2585, -97.7124,
          "Austin, TX", ("Veracruz",), "1704 E Cesar Chavez St, Austin, TX"),
    Place("zilker", "Zilker Park", "park", 30.2669, -97.7729, "Austin, TX",
          ("Zilker",)),
    Place("acme", "Acme Robotics office", "office", 30.2672, -97.7431,
          "Austin, TX", ("the office",), "500 W 2nd St, Austin, TX"),
    Place("heb", "H-E-B Mueller", "store", 30.2988, -97.7066, "Austin, TX",
          ("H-E-B", "HEB")),
    Place("uchi", "Uchi", "restaurant", 30.2577, -97.7616, "Austin, TX", (),
          "801 S Lamar Blvd, Austin, TX"),
    Place("ellis_dental", "Ellis Dental", "office", 30.3072, -97.7480,
          "Austin, TX"),
    Place("bled", "Lake Bled", "landmark", 46.3625, 14.0936, "Bled, Slovenia",
          ("Bled",)),
    Place("bled_castle", "Bled Castle", "landmark", 46.3699, 14.1003,
          "Bled, Slovenia"),
    Place("ljubljana", "Ljubljana", "city", 46.0569, 14.5058,
          "Ljubljana, Slovenia"),
    Place("el_paso", "El Paso", "city", 31.7619, -106.4850, "El Paso, TX"),
)


def person(key: str) -> Person:
    if key == "me":
        return OWNER
    return next(p for p in PEOPLE if p.key == key)


def place(key: str) -> Place:
    return next(p for p in PLACES if p.key == key)


# ---------------------------------------------------------------- time helpers

def day(anchor: date, offset: int) -> date:
    return anchor + timedelta(days=offset)


def local_dt(anchor: date, offset: int, hhmm: str) -> datetime:
    """Aware local datetime for `offset` days from anchor at 'HH:MM[:SS]'."""
    parts = [int(x) for x in hhmm.split(":")]
    while len(parts) < 3:
        parts.append(0)
    d = day(anchor, offset)
    return datetime(d.year, d.month, d.day, *parts, tzinfo=ZoneInfo(TIMEZONE))


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def birthday_date(p: Person, anchor: date) -> date | None:
    """The person's birthday in the anchor's year (Maya's is pinned inside
    the demo window so the birthday storyline always shows up)."""
    if p.key == "maya":
        return day(anchor, MAYA_BIRTHDAY_OFFSET)
    if p.key == "sam":
        return day(anchor, SAM_BIRTHDAY_OFFSET)
    if p.birthday is None:
        return None
    m, d = p.birthday
    try:
        return date(anchor.year, m, d)
    except ValueError:          # Feb 29 in a non-leap year
        return date(anchor.year, m, 28)


def bled_trip_days(anchor: date) -> list[date]:
    """The trip to Slovenia, four years before the anchor: five days."""
    try:
        start = anchor.replace(year=anchor.year - 4)
    except ValueError:
        start = anchor.replace(year=anchor.year - 4, day=28)
    start = start - timedelta(days=25)
    return [start + timedelta(days=i) for i in range(5)]


# ---------------------------------------------------------------- the journal
#
# What Alex said into the phone. Each note is a list of (sentence, entry) where
# entry is None (the sentence supports no record) or a dict the demo's draft
# builder turns into a contract-v2 entry, citing exactly that sentence's
# character span. Keys:
#   kind, primary, secondary, title, time_of_day, body
#   entities:      [(person/place key or 'media:<name>' / 'project:<name>', role)]
#   measurements:  [(metric, value, unit)]
#   key:           a name later entries can refer to
#   closes:        key of an earlier task this sentence closes (entry_update)
#   links:         [(type, key)]  this entry -> an earlier keyed entry
#   relations:     [(from ref, relationship type, to ref)] stated by this
#                  sentence ("Jake grew up in El Paso" -> jake from el_paso)
#   due_offset:    for tasks/plans, a due date as an anchor offset
#   data:          the entry's data object (a plan's goal: docs/contract.md)
#   occurred:      'HH:MM' local time of the event, when known

@dataclass
class Note:
    time: str                          # local 'HH:MM:SS' the note was recorded
    kind: str                          # 'evening' (audio-length) or 'quick'
    sentences: list[tuple[str, dict | None]]

    @property
    def text(self) -> str:
        return " ".join(s for s, _ in self.sentences)


@dataclass
class JournalDay:
    offset: int
    notes: list[Note]
    summary: str
    mood: int | None = None
    energy: int | None = None
    sleep_hours: float | None = None
    pending: bool = False              # left un-logged: shows up in `status`


def _e(**kw) -> dict:
    return kw


JOURNAL: tuple[JournalDay, ...] = (
    JournalDay(-40, [Note("21:12:40", "evening", [
        ("Started the week with an early lift at Movement Gym with Sam, squats felt strong, about seventy-five minutes.",
         _e(kind="event", primary="exercise", secondary=["social"],
            title="Morning lift at Movement with Sam", time_of_day="morning",
            entities=[("sam", "participant"), ("movement", "location")],
            measurements=[("duration", 75, "min")], occurred="06:30")),
        ("Lunch was a chicken burrito bowl from Chipotle.",
         _e(kind="event", primary="food", title="Chicken burrito bowl from Chipotle",
            time_of_day="afternoon")),
        ("Priya asked me to own the Q4 pitch deck for the board meeting, it's due in three weeks.",
         _e(kind="task", primary="work", title="Own the Q4 board pitch deck",
            entities=[("priya", "participant"), ("project:Q4 board deck", "subject")],
            key="deck_task", due_offset=-20)),
        ("I need to renew my passport before the Slovenia trip idea gets real, so that's on the list now.",
         _e(kind="task", primary="chores", secondary=["travel"],
            title="Renew passport", key="passport_task")),
        ("Overall a good day, mood maybe a seven.", None),
    ])], "Strong start: early lift with Sam, and Priya handed me the Q4 board deck.",
        mood=7, energy=6, sleep_hours=7.2),

    JournalDay(-38, [
        Note("12:41:08", "quick", [
            ("Idea: a tiny app that texts me the weather and my calendar every morning at seven.",
             _e(kind="idea", primary="idea", title="Morning text with weather + calendar")),
        ]),
        Note("22:05:51", "evening", [
            ("Dinner at Veracruz with Jake, the fish tacos, his favorite spot as always.",
             _e(kind="event", primary="food", secondary=["social"],
                title="Fish tacos at Veracruz with Jake", time_of_day="evening",
                entities=[("jake", "participant"), ("veracruz", "location")],
                occurred="19:30")),
            ("Jake mentioned he's flying home to El Paso next month for his dad's birthday, he grew up there.",
             _e(kind="fact", primary="social", title="Jake grew up in El Paso",
                entities=[("jake", "subject"), ("el_paso", "subject")],
                relations=[("jake", "from", "el_paso")])),
            ("We reminisced about Lake Bled and the rowboat we took out to the island.",
             _e(kind="reflection", primary="travel", secondary=["social"],
                title="Reminiscing about Lake Bled with Jake",
                entities=[("jake", "participant"), ("bled", "subject")])),
        ]),
    ], "Tacos and Bled memories with Jake; an idea for a morning briefing app.",
        mood=8, energy=7, sleep_hours=7.4),

    JournalDay(-34, [Note("21:48:19", "evening", [
        ("Long day on the pitch deck, finished the market sizing section.",
         _e(kind="event", primary="work", title="Finished market sizing section of the deck",
            entities=[("project:Q4 board deck", "subject")], key="deck_progress_1",
            links=[("part_of", "deck_task")])),
        ("Skipped the gym.", None),
        ("Called Mom, she's recovering well from her knee surgery and wants me to visit San Antonio.",
         _e(kind="event", primary="social", secondary=["health"], title="Call with Mom about her knee recovery",
            entities=[("mom", "participant")], occurred="18:00")),
        ("I should plan a weekend in San Antonio soon.",
         _e(kind="plan", primary="travel", title="Visit Mom in San Antonio",
            entities=[("mom", "participant")], key="visit_mom")),
    ])], "Deep work on the deck; Mom's knee is healing.",
        mood=6, energy=5, sleep_hours=6.4),

    JournalDay(-30, [Note("22:31:02", "evening", [
        ("Went bouldering with Maya at Movement, she finally sent the purple V3, about ninety minutes.",
         _e(kind="event", primary="exercise", secondary=["social"],
            title="Bouldering with Maya at Movement", time_of_day="evening",
            entities=[("maya", "participant"), ("movement", "location")],
            measurements=[("duration", 90, "min")], occurred="18:00")),
        ("Grocery run at H-E-B afterwards, spent about sixty-four dollars.",
         _e(kind="event", primary="purchase", secondary=["food"], title="Groceries at H-E-B",
            entities=[("heb", "location")], measurements=[("money", 64, "USD")])),
    ])], "Climbing with Maya, then groceries.", mood=8, energy=7, sleep_hours=7.6),

    JournalDay(-27, [
        Note("09:16:44", "quick", [
            ("Priya's advice on the deck: lead with the customer story, not the TAM slide.",
             _e(kind="decision", primary="work",
                title="Priya: lead the deck with the customer story, not the TAM slide",
                entities=[("priya", "participant"), ("project:Q4 board deck", "subject")],
                key="priya_advice")),
        ]),
        Note("21:03:37", "evening", [
            ("Rewrote the opening of the deck around the warehouse customer story.",
             _e(kind="event", primary="work", title="Rewrote the deck opening around the customer story",
                entities=[("project:Q4 board deck", "subject")],
                links=[("continues", "deck_progress_1")])),
            ("Ran five k at Zilker Park after work in twenty-seven minutes.",
             _e(kind="event", primary="exercise", title="5k run at Zilker Park",
                time_of_day="evening", entities=[("zilker", "location")],
                measurements=[("distance", 5, "km"), ("duration", 27, "min")],
                occurred="18:15")),
        ]),
    ], "Took Priya's advice and rebuilt the deck opening; 5k at Zilker.",
        mood=7, energy=7, sleep_hours=7.1),

    JournalDay(-23, [Note("20:44:10", "evening", [
        ("Finally renewed the passport at the post office this morning, one less thing.",
         _e(kind="event", primary="chores", secondary=["travel"],
            title="Renewed passport at the post office", time_of_day="morning",
            closes="passport_task", links=[("resolves", "passport_task")],
            measurements=[("money", 130, "USD")], occurred="09:30")),
        ("Lunch with Priya, we talked through the board meeting dry run.",
         _e(kind="event", primary="work", secondary=["food"], title="Lunch with Priya: board dry run",
            entities=[("priya", "participant")], time_of_day="afternoon")),
        ("Slept badly last night, maybe five and a half hours.", None),
    ])], "Passport renewed; dry-run planning with Priya; short on sleep.",
        mood=6, energy=4, sleep_hours=5.5),

    JournalDay(-20, [Note("22:58:33", "evening", [
        ("Presented the Q4 deck to the board today and it went really well, Priya said the customer story landed.",
         _e(kind="event", primary="work", title="Presented the Q4 deck to the board",
            entities=[("priya", "participant"), ("project:Q4 board deck", "subject")],
            closes="deck_task", links=[("resolves", "deck_task")], occurred="14:00")),
        ("Celebrated with Jake and Maya at Veracruz.",
         _e(kind="event", primary="social", secondary=["food"], title="Celebration dinner at Veracruz",
            entities=[("jake", "participant"), ("maya", "participant"),
                      ("veracruz", "location")], occurred="19:30")),
    ])], "Board presentation landed; celebrated with Jake and Maya.",
        mood=9, energy=8, sleep_hours=6.9),

    JournalDay(-16, [Note("19:22:05", "evening", [
        ("Rest day, read two chapters of The Overstory.",
         _e(kind="event", primary="media", title="Read two chapters of The Overstory",
            entities=[("media:The Overstory", "subject")], key="overstory")),
        ("I'm planning a return trip to Bled next summer and asked Claude for a five day itinerary.",
         _e(kind="plan", primary="travel", title="Return trip to Lake Bled next summer",
            entities=[("bled", "subject")], key="bled_plan")),
    ])], "Rest day with a book; started planning a return to Bled.",
        mood=7, energy=6, sleep_hours=8.1),

    JournalDay(-12, [Note("21:37:58", "evening", [
        ("Gym with Sam, bench press three sets of five at one eighty-five pounds.",
         _e(kind="event", primary="exercise", title="Bench press with Sam",
            entities=[("sam", "participant"), ("movement", "location")],
            measurements=[("sets", 3, None), ("reps", 5, None), ("weight", 185, "lb")],
            time_of_day="morning", occurred="06:30")),
        ("Weighed seventy-eight point four kilos this morning.",
         _e(kind="observation", primary="health", title="Morning weigh-in",
            measurements=[("weight", 78.4, "kg")], time_of_day="morning")),
        ("Dentist appointment with Dr. Ellis, no cavities.",
         _e(kind="event", primary="health", title="Dentist check-up, no cavities",
            entities=[("ellis", "participant"), ("ellis_dental", "location")],
            occurred="10:30")),
    ])], "Bench day with Sam and a clean dentist check-up.",
        mood=7, energy=7, sleep_hours=7.3),

    JournalDay(-9, [Note("23:14:21", "evening", [
        ("It was Maya's birthday, I took her to Uchi for dinner and gave her the ceramics class voucher.",
         _e(kind="event", primary="social", secondary=["food"], title="Maya's birthday dinner at Uchi",
            entities=[("maya", "participant"), ("uchi", "location")], occurred="19:00",
            measurements=[("money", 120, "USD")])),
        ("Called Mom on the drive home.",
         _e(kind="event", primary="social", title="Called Mom on the drive home",
            entities=[("mom", "participant")], occurred="21:00")),
    ])], "Maya's birthday at Uchi.", mood=9, energy=7, sleep_hours=7.0),

    JournalDay(-5, [Note("22:49:47", "evening", [
        ("Work is piling up again with three launches next week.",
         _e(kind="reflection", primary="work", title="Workload piling up: three launches next week")),
        ("Only slept six hours.", None),
        ("I skipped the gym twice this week and need to get back to three sessions a week.",
         _e(kind="plan", primary="health", secondary=["exercise"],
            title="Get back to three gym sessions a week", key="gym_task",
            data={"goal": {"metric": "workouts", "target": 3, "per": "week"}})),
    ])], "Stretched thin: launches, short sleep, missed workouts.",
        mood=5, energy=4, sleep_hours=6.0),

    JournalDay(-2, [
        Note("13:05:12", "quick", [
            ("Remember to book flights to Ljubljana before prices go up, by this weekend.",
             _e(kind="task", primary="travel", title="Book flights to Ljubljana",
                entities=[("ljubljana", "subject")], links=[("part_of", "bled_plan")],
                due_offset=3)),
        ]),
        Note("21:26:30", "evening", [
            ("Lifted with Sam again, back on track.",
             _e(kind="event", primary="exercise", title="Lift with Sam",
                entities=[("sam", "participant"), ("movement", "location")],
                links=[("relates", "gym_task")], measurements=[("duration", 60, "min")],
                time_of_day="morning")),
            ("Texted Jake about the Bled trip and he's in.",
             _e(kind="event", primary="social", secondary=["travel"],
                title="Jake is in for the Bled trip",
                entities=[("jake", "participant"), ("bled", "subject")],
                links=[("continues", "bled_plan")])),
        ]),
    ], "Back in the gym; Jake's in for Bled.", mood=7, energy=7, sleep_hours=7.5),

    JournalDay(-1, [Note("22:10:09", "evening", [
        ("Grabbed tacos at Veracruz with Jake again.",
         _e(kind="event", primary="food", secondary=["social"], title="Tacos at Veracruz with Jake",
            entities=[("jake", "participant"), ("veracruz", "location")], occurred="19:00")),
        ("Finished The Overstory, loved the ending.",
         _e(kind="event", primary="media", title="Finished The Overstory",
            entities=[("media:The Overstory", "subject")], links=[("continues", "overstory")])),
    ])], "Tacos with Jake; finished The Overstory.", mood=8, energy=6, sleep_hours=7.2),

    # Today's note waits in the inbox: the demo leaves it for `status` / `/log`.
    JournalDay(0, [Note("12:20:00", "quick", [
        ("Sam wants to try the new climbing gym on Saturday.", None),
    ])], "", pending=True),
)


# ---------------------------------------------------------------- texts & calls
#
# (offset, 'HH:MM', sender key, text). Threads with one other person are 1:1;
# more than one is a group chat. Which app carries them depends on the combo:
# iMessage on the iPhone (read from the Mac's chat.db, or from an iPhone
# backup on Windows), SMS/RCS on the Samsung.

@dataclass(frozen=True)
class Thread:
    key: str
    members: tuple[str, ...]          # person keys, excluding the owner
    name: str | None                  # group name, if any
    messages: tuple[tuple[int, str, str, str], ...]


THREADS: tuple[Thread, ...] = (
    Thread("jake", ("jake",), None, (
        (-38, "18:02", "jake", "Veracruz tonight? you know it's my favorite"),
        (-38, "18:05", "me", "always. 7:30?"),
        (-38, "18:06", "jake", "perfect"),
        (-37, "22:41", "jake", "Still thinking about that rowboat on Lake Bled lol"),
        (-37, "22:45", "me", "best trip ever. we should go back"),
        (-20, "21:10", "jake", "Proud of you on the board deck!"),
        (-2, "19:30", "me", "Thinking Bled next June. You in?"),
        (-2, "19:41", "jake", "100% in. Let's stay by the lake again, not in Ljubljana"),
        (-1, "12:15", "jake", "Veracruz at 7?"),
        (-1, "12:16", "me", "see you there"),
    )),
    Thread("maya", ("maya",), None, (
        (-30, "08:10", "maya", "Climbing tonight?"),
        (-30, "08:12", "me", "yes! 6pm at Movement"),
        (-9, "07:30", "me", "Happy birthday!! Dinner at Uchi tonight, 7pm"),
        (-9, "07:45", "maya", "thank youuu, can't wait"),
        (-5, "23:02", "maya", "Go to sleep, the launches will still be there tomorrow"),
    )),
    Thread("mom", ("mom",), None, (
        (-34, "17:20", "mom", "Knee is healing nicely. When are you visiting?"),
        (-34, "17:30", "me", "Maybe next month, Mom"),
        (-9, "20:15", "mom", "Tell Maya happy birthday from me"),
    )),
    Thread("priya", ("priya",), None, (
        (-27, "09:02", "priya", "Lead with the customer story, not the TAM slide. Trust me."),
        (-20, "16:45", "priya", "The board loved it. Nice work."),
    )),
    Thread("sam", ("sam",), None, (
        (-40, "06:05", "sam", "Gym 6:30?"),
        (-12, "06:10", "sam", "Bench day"),
        (-2, "06:00", "sam", "Back at it?"),
        (-2, "06:01", "me", "on my way"),
    )),
    Thread("bled_crew", ("jake", "maya", "sam"), "Bled crew", (
        (-16, "14:00", "me", "Who's up for Lake Bled next summer?"),
        (-16, "14:05", "sam", "Never been but I'm in"),
        (-16, "14:07", "maya", "Only if we do the castle this time"),
        (-2, "19:50", "jake", "Booking the lakeside place, 4 of us"),
    )),
)

# (offset, 'HH:MM', person key, duration seconds, 'out' | 'in' | 'missed')
CALLS: tuple[tuple[int, str, str, int, str], ...] = (
    (-34, "18:00", "mom", 1320, "out"),
    (-23, "19:00", "mom", 480, "in"),
    (-20, "21:30", "jake", 300, "in"),
    (-14, "10:00", "ellis", 120, "in"),
    (-9, "21:00", "mom", 720, "out"),
    (-3, "17:45", "priya", 0, "missed"),
)

# WhatsApp: the family group lives there on both phones.
WHATSAPP_GROUP = {
    "name": "Rivera Family",
    "members": ("mom",),                       # plus Tía Rosa, who has no contact card
    "extra_names": ("Tía Rosa",),
    "messages": (
        (-15, "19:00", "mom", "Family dinner at Tía Rosa's on Sunday?"),
        (-15, "19:04", "Tía Rosa", "Sí! Bring Maya"),
        (-15, "19:10", "me", "We'll be there"),
        (-13, "21:30", "mom", "Thank you for coming, mijo. Drive safe."),
    ),
}


# ---------------------------------------------------------------- AI chats

@dataclass(frozen=True)
class AiChat:
    vendor: str                                # 'claude' | 'chatgpt'
    title: str
    offset: int
    time: str
    turns: tuple[tuple[str, str], ...]         # (role 'user'|'assistant', text)


AI_CHATS: tuple[AiChat, ...] = (
    AiChat("claude", "Return trip to Lake Bled", -16, "14:30", (
        ("user", "I went to Lake Bled in Slovenia four years ago with friends and want to go back next summer. Can you sketch a 5-day itinerary that includes Bled Castle and Vintgar Gorge?"),
        ("assistant", "Here's a relaxed 5-day plan. Day 1: land in Ljubljana, walk the old town and the Triple Bridge. Day 2: drive to Bled, visit Bled Castle in the morning and take a pletna boat to the island church. Day 3: Vintgar Gorge early, before the crowds, then swim at the lake. Day 4: day trip to Lake Bohinj and the Savica waterfall. Day 5: back to Ljubljana for the flight."),
        ("user", "What's the best month to avoid crowds?"),
        ("assistant", "Early June or mid-September: warm enough to swim, and the July-August peak hasn't started or has passed. Book lakeside lodging early either way."),
    )),
    AiChat("claude", "Board deck structure", -28, "22:00", (
        ("user", "How should I structure a board deck for a Q4 product update?"),
        ("assistant", "Open with one customer story that shows the problem and your product solving it, then the three metrics that matter, then what you need from the board. Push market sizing to the appendix unless they ask."),
    )),
    AiChat("chatgpt", "High-protein meal prep", -26, "20:10", (
        ("user", "Give me a simple one-week meal prep plan that hits about 150 g of protein a day."),
        ("assistant", "Base it on three batch-cooked proteins: chicken thighs, ground turkey, and a big pot of black beans. Breakfast: Greek yogurt with oats. Lunch: chicken burrito bowls. Dinner: turkey chili. Snacks: cottage cheese and a protein shake. That lands around 150-160 g."),
    )),
)


# ---------------------------------------------------------------- calendar

@dataclass(frozen=True)
class CalEvent:
    uid: str
    title: str
    offset: int
    start: str                    # local 'HH:MM'
    minutes: int
    attendees: tuple[str, ...]    # person keys (owner is always organizer/attendee)
    place: str | None = None      # place key
    weekly_count: int | None = None   # recurring weekly, this many occurrences
    description: str | None = None
    all_day: bool = False


CALENDAR: tuple[CalEvent, ...] = (
    CalEvent("demo-1on1-priya", "1:1 Alex / Priya", -41, "10:00", 30, ("priya",),
             "acme", weekly_count=7, description="Weekly 1:1"),
    CalEvent("demo-board-q4", "Board meeting: Q4 deck", -20, "14:00", 90,
             ("priya",), "acme", description="Q4 product update to the board"),
    CalEvent("demo-dinner-jake", "Dinner w/ Jake @ Veracruz", -38, "19:30", 90,
             ("jake",), "veracruz"),
    CalEvent("demo-climb-maya", "Climbing w/ Maya", -30, "18:00", 90, ("maya",),
             "movement"),
    CalEvent("demo-dentist", "Dentist: Dr. Ellis", -12, "10:30", 45, ("ellis",),
             "ellis_dental"),
    CalEvent("demo-maya-bday", "Maya's birthday dinner @ Uchi", -9, "19:00", 120,
             ("maya",), "uchi"),
    CalEvent("demo-family-dinner", "Family dinner at Tía Rosa's", -13, "17:00", 180,
             ("mom",), None),
    CalEvent("demo-launches", "Launch week", -1, "00:00", 0, (), None,
             all_day=True, description="Three launches"),
)


# ---------------------------------------------------------------- notes

@dataclass(frozen=True)
class NoteDoc:
    key: str
    title: str
    offset: int | None            # anchor offset; None = on the Bled trip (see trip_offset)
    body: str
    trip_day: int | None = None   # index into bled_trip_days() when offset is None
    labels: tuple[str, ...] = ()


NOTES: tuple[NoteDoc, ...] = (
    NoteDoc("gifts", "Gift ideas for Maya", -25,
            "- ceramics class voucher (Austin Clay Studio)\n- the Patagonia jacket in sage\n- concert tickets: Japanese Breakfast",
            labels=("personal",)),
    NoteDoc("deck_outline", "Board deck outline", -29,
            "1. Customer story: the warehouse pilot\n2. Three metrics: activation, retention, NPS\n3. What we need from the board\nAppendix: market sizing (TAM/SAM/SOM)",
            labels=("work",)),
    NoteDoc("books", "Book list", -16,
            "Reading now: The Overstory\nNext: Project Hail Mary\nSomeday: Braiding Sweetgrass",
            labels=("personal",)),
    NoteDoc("bled_packing", "Bled packing list", None,
            "rain jacket, hiking boots, euro cash for the pletna boat, swim stuff for the lake, adapter (type F)",
            trip_day=0, labels=("travel",)),
)


# ---------------------------------------------------------------- money
# (offset or None for the Bled trip, trip_day, merchant, amount USD (negative = spend), category hint)

@dataclass(frozen=True)
class Txn:
    offset: int | None
    merchant: str
    amount: float                 # negative = money out
    memo: str | None = None
    trip_day: int | None = None


TRANSACTIONS: tuple[Txn, ...] = (
    Txn(-40, "MOVEMENT GYM AUSTIN TX", -79.00, "monthly membership"),
    Txn(-40, "CHIPOTLE 2231 AUSTIN TX", -12.85),
    Txn(-38, "VERACRUZ ALL NATURAL", -34.50),
    Txn(-30, "H-E-B #742 AUSTIN TX", -64.12),
    Txn(-28, "ACME ROBOTICS PAYROLL", 4200.00, "direct deposit"),
    Txn(-23, "USPS PASSPORT FEE", -130.00),
    Txn(-20, "VERACRUZ ALL NATURAL", -86.20),
    Txn(-19, "H-E-B #742 AUSTIN TX", -88.40),
    Txn(-14, "ACME ROBOTICS PAYROLL", 4200.00, "direct deposit"),
    Txn(-10, "MOVEMENT GYM AUSTIN TX", -79.00, "monthly membership"),
    Txn(-9, "UCHI AUSTIN", -212.40),
    Txn(-9, "AUSTIN CLAY STUDIO", -120.00, "gift voucher"),
    Txn(-6, "H-E-B #742 AUSTIN TX", -71.05),
    Txn(-1, "VERACRUZ ALL NATURAL", -29.75),
    Txn(None, "AIR FRANCE AUS-LJU", -1124.60, "flights to Ljubljana", trip_day=0),
    Txn(None, "VILA BLED HOTEL BLED SI", -642.00, "4 nights", trip_day=1),
    Txn(None, "PLETNA BLED SI", -36.00, "boat to the island", trip_day=2),
)


# ---------------------------------------------------------------- photos

@dataclass(frozen=True)
class Photo:
    name: str                     # base file name without extension
    offset: int | None
    time: str
    place: str                    # place key (GPS comes from it, with a small jitter)
    trip_day: int | None = None
    caption: str | None = None    # only exports that carry descriptions use it


PHOTOS: tuple[Photo, ...] = (
    Photo("bled_lake_island", None, "10:12", "bled", trip_day=1, caption="The island church from the shore"),
    Photo("bled_castle_view", None, "11:40", "bled_castle", trip_day=1),
    Photo("bled_rowboat", None, "15:05", "bled", trip_day=2, caption="Rowboat with Jake"),
    Photo("bled_sunset", None, "20:31", "bled", trip_day=2),
    Photo("ljubljana_bridge", None, "17:20", "ljubljana", trip_day=0),
    Photo("ljubljana_dinner", None, "20:02", "ljubljana", trip_day=4),
    Photo("veracruz_tacos", -38, "19:48", "veracruz"),
    Photo("movement_climb", -30, "18:40", "movement"),
    Photo("zilker_run", -27, "18:47", "zilker"),
    Photo("board_day_team", -20, "15:45", "acme"),
    Photo("maya_birthday", -9, "20:10", "uchi"),
)


def photo_dt(p: Photo, anchor: date) -> datetime:
    """Local capture time; trip photos are in Slovenia's zone."""
    if p.offset is None:
        d = bled_trip_days(anchor)[p.trip_day or 0]
        hh, mm = (int(x) for x in p.time.split(":"))
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo("Europe/Ljubljana"))
    return local_dt(anchor, p.offset, p.time)


def trip_dt(trip_day: int, hhmm: str, anchor: date) -> datetime:
    d = bled_trip_days(anchor)[trip_day]
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo("Europe/Ljubljana"))


# ---------------------------------------------------------------- health

@dataclass
class HealthDay:
    date: date
    sleep_start: datetime         # local, the night BEFORE `date` (into the morning)
    sleep_end: datetime
    steps: int
    resting_hr: int
    weight_kg: float | None       # morning weigh-in, not every day
    kcal_in: int
    workouts: list[dict] = field(default_factory=list)
    # {type: 'strength'|'climbing'|'running', start: dt, minutes, distance_km?, kcal}

    @property
    def sleep_hours(self) -> float:
        return round((self.sleep_end - self.sleep_start).total_seconds() / 3600, 2)


_WORKOUTS = {
    -40: ("strength", "06:30", 75, None),
    -37: ("strength", "06:30", 55, None),
    -33: ("running", "18:10", 32, 5.6),
    -30: ("climbing", "18:00", 90, None),
    -27: ("running", "18:15", 27, 5.0),
    -24: ("strength", "06:30", 60, None),
    -19: ("strength", "06:30", 65, None),
    -15: ("running", "07:00", 41, 7.2),
    -12: ("strength", "06:30", 70, None),
    -2: ("strength", "06:30", 60, None),
}


def health_series(anchor: date, days: int = WINDOW_DAYS) -> list[HealthDay]:
    """Deterministic daily health data for the window ending at `anchor`.

    Sleep follows the journal where the journal names it (so 'slept badly,
    five and a half hours' is also what the watch recorded); the last week
    before the anchor dips, which the advisor brief should notice.
    """
    rng = random.Random(f"lifecrm-demo-{anchor.isoformat()}")
    said = {j.offset: j.sleep_hours for j in JOURNAL if j.sleep_hours}
    out: list[HealthDay] = []
    tz = ZoneInfo(TIMEZONE)
    for off in range(-days + 1, 1):
        d = day(anchor, off)
        hours = said.get(off)
        if hours is None:
            hours = round(rng.uniform(6.6, 7.8), 2) if not -6 <= off <= -3 \
                else round(rng.uniform(5.6, 6.3), 2)
        bed_min = 23 * 60 + 10 + rng.randint(-35, 40)
        prev = d - timedelta(days=1)
        start = datetime(prev.year, prev.month, prev.day, tzinfo=tz) + timedelta(minutes=bed_min)
        end = start + timedelta(hours=hours)
        steps = rng.randint(5200, 11800)
        workouts = []
        if off in _WORKOUTS:
            kind, hhmm, minutes, km = _WORKOUTS[off]
            w_start = local_dt(anchor, off, hhmm)
            kcal = int(minutes * (10.5 if kind == "running" else 7.5))
            workouts.append({"type": kind, "start": w_start, "minutes": minutes,
                             "distance_km": km, "kcal": kcal})
            if kind == "running":
                steps += int((km or 5) * 1300)
        weight = None
        if (off % 3 == 0) or off == -12:
            weight = 78.4 if off == -12 else round(79.3 - (off + days) * 0.022
                                                    + rng.uniform(-0.25, 0.25), 1)
        out.append(HealthDay(
            date=d, sleep_start=start, sleep_end=end, steps=steps,
            resting_hr=rng.randint(57, 64) + (3 if -6 <= off <= -3 else 0),
            weight_kg=weight, kcal_in=rng.randint(2050, 2700), workouts=workouts))
    return out


# ---------------------------------------------------------------- demo questions
# What the demo asks once everything is imported; each should be answerable
# from the data above, with citations.

QUESTIONS: tuple[str, ...] = (
    "how did I sleep last week",
    "what's Jake's favorite restaurant",
    "where is Jake from",
    "where is that lake I went to in Slovenia",
    "what did I ask Claude about Bled",
    "when did I last talk to Mom",
    "what advice did Priya give me about the deck",
    "how much did I spend at Veracruz",
    "what are my open tasks",
    "how many steps did I average this month",
    # The newer sources: mail, workouts, nutrition, payments, reading,
    # searches, rides and LinkedIn.
    "what advice did Priya give me over email",
    "what's my bench PR",
    "what am I low on",
    "how much did I pay Jake",
    "what did I highlight in The Overstory",
    "what did I search for about Bled",
    "when did I last take an Uber",
    "where does Priya work",
)
