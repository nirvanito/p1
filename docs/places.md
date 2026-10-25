# Places: town names, trips and countries

Google's on-device Timeline export names no place: a visit arrives as
`Place at 46.3625, 14.0938`, and a photo carries only its GPS position. The
places feature gives those positions town and country names from
**GeoNames** (geonames.org, CC BY 4.0), a free gazetteer of the world's
towns, read from a file on this machine. Nothing about where you were is
sent anywhere: there is no web API in it.

```
python -m lifecrm places fetch          # once: downloads cities1000.zip (towns of 1,000+
                                        # people, about 10 MB) into data/geo/places.tsv
python -m lifecrm places fetch --dry-run
python -m lifecrm places status
python -m lifecrm places trips [--year 2022] [--json]
python -m lifecrm places countries [--json]
python -m lifecrm places where 2022-05-12 [--end 2022-05-15]
```

`fetch` is the only command in the feature that touches the network, and only
when you run it. `--dataset cities500 | cities5000 | cities15000` picks a
denser or a lighter file. It writes atomically (the old file stays until the
new one is complete). Instead of fetching, `[places] gazetteer` can point at
any GeoNames `cities*.txt` you downloaded yourself — the reader tells the
compact file and the raw one apart by their columns.

## What it works out

A **town** is the nearest GeoNames town within `max_km` (50) of a recorded
position, said as "near Bled, Slovenia" and cited to the row whose position
it is. A position farther than that from every town (the middle of an ocean)
gets no name, never a guess. Nothing is stored: the names are recomputed from
the file each time, so a newer gazetteer renames nothing in the database.

**Home** is `[places] home = "lat, lon"` when you set one. Otherwise, for each
day with a located record, it is the town with the most such days within 90
days either side — so a move to a new city moves home with it, and a long
stay somewhere eventually becomes home too. Where those 180 days hold fewer
than 14 located days (a trip whose photos are all there is from that year),
home is the town with the most located days of all.

A **trip** is a run of days with a located record more than `trip_km` (100)
from that day's home. A day with no located record between two away days
keeps the trip going; a located day at home ends it. Each trip lists its
towns in the order first seen (the ones within half of `trip_km` of home —
the way out and back — left out), its countries, its days and how far it went.

A **country** is every country a located record falls in, with its first and
last day, its days and its towns.

## Where it shows

- `ask`: "which countries have I been to", "what trips did I take in 2024",
  "where was I last weekend" / "on 2022-05-12", "when was I in Slovenia",
  "have I been to Bled". The answer cites the first and last record behind
  each claim and passes its own citation check. Without a gazetteer the
  answer's no-data line says so.
- The review pack's `trips` section (week, year and all lenses): trips that
  overlap the period, the countries its records fall in, and which of them
  were first on record in the period.
- The web UI: the Places page (`#/places`: home, countries, trips), a banner
  on the Today page of a day that is part of a trip, and "near ..." beside
  each place visit and photo.
- The Obsidian vault: `Trips.md` (trips and countries, newest first) and
  "(near Bled, Slovenia)" beside each place visit in a day note.
- On this day and journal prompts name the town a visit was near.

## Settings

```toml
[places]
gazetteer = "data/geo/places.tsv"   # the default
home = "30.27, -97.74"              # optional
trip_km = 100                       # 5-5000
max_km = 50                         # 1-500
```

Uncheck the feature with `python -m lifecrm features uncheck places`
([features.md](features.md)).
