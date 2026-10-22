# Photos (camera rolls, Google Photos Takeout, a Mac Photos library)

Photos are the cheapest strong evidence of where a life happened. "The lake
I went to in Slovenia four years ago" is a GPS position and a date on a few
dozen files; nobody had to write anything down. The `photos` connector reads
that **metadata only** — capture time, UTC offset, GPS, camera, pixel size —
into the observed tier, one observation per photo or video in stream `photo`.

It never decodes an image, never makes a thumbnail, and never copies media
into the database: `media_path` says where the file is. It is stdlib only —
no Pillow, no exiftool — because a library of 100k photos needs a dozen tags
from the first few kilobytes of each file, not the pixels.

## Where your photos are

**Windows laptop + Samsung phone.** Turn on OneDrive sync in the Gallery app
(*Gallery > Settings > Sync with OneDrive*), or the OneDrive app's *Camera
upload*. Either lands the camera roll on the laptop under
`OneDrive\Pictures\Samsung Gallery\...` or `OneDrive\Pictures\Camera Roll\...`.
A folder copied off the phone by cable (`DCIM\Camera`) works the same way.
Files that OneDrive keeps *online-only* are skipped and counted, never
opened — opening one would download it. Mark the folder *Always keep on this
device* first if you want them all.

**Google Photos.** [takeout.google.com](https://takeout.google.com), select
only *Google Photos*, export as .zip. Pass the zip, the extracted folder, or
the folder holding all the `takeout-*.zip` parts (a sidecar often lands in a
different part than its photo, so parts are read together).

**MacBook + iPhone.** With iCloud Photos (or a cable import) the photos are in
the Mac's Photos library, `~/Pictures/Photos Library.photoslibrary`. The
library is protected by macOS privacy controls: give the app that runs the
import (Terminal, iTerm, VS Code...) **Full Disk Access** in *System Settings >
Privacy & Security > Full Disk Access*. Without it the connector stops with a
message that says exactly that. Originals that iCloud keeps only in the cloud
(*Optimize Mac Storage*) do not matter: the library's database holds every
photo's metadata either way, and that is all the connector reads.

## Importing

Dry-run first; it is the review step:

```
.venv\Scripts\python.exe -m lifecrm observe photos            # the default place
.venv\Scripts\python.exe -m lifecrm observe photos D:\DCIM     # any folder
.venv\Scripts\python.exe -m lifecrm observe photos takeout-20261021T120000Z-001.zip
python -m lifecrm observe photos "~/Pictures/Photos Library.photoslibrary"   # Mac
```

With no path it reads `[connectors.photos] path` from `config.toml` when set;
otherwise, on a Mac, the Photos library; on Windows, the one OneDrive camera
folder (`Pictures/Camera Roll` or `Pictures/Samsung Gallery`) it finds. Two
camera folders often hold the same photos, so when it finds both it lists them
and asks you to pass one — it never guesses.

```toml
[connectors.photos]
path = "~/OneDrive/Pictures/Samsung Gallery"   # optional; '~' expands
include_hidden = false                          # Photos' Hidden album; default off
```

The report names the source and its schema, counts photos, videos and
screenshots, and then the lines worth reading before `--apply`:

- `per year: 2022 6, 2026 6` — check the oldest year against what the camera
  roll shows; a wrong range here means wrong dates everywhere.
- `time from: exif 11, filename 1 (1 without the camera's own zone)` — where
  each capture time came from (below). A large `mtime` count means the files
  lost their metadata on the way.
- `location: 11 of 12 have GPS`.
- the newest photo as a sample, with its camera, position and zone.
- `skipped:` lines with counts: hidden files, cloud-only files, Live Photo
  motion clips, edited copies, Takeout album copies, trashed photos.

Re-running on an unchanged source is a no-op (the folder's hash is over its
listing — names, sizes, times — not its bytes: hashing a 300 GB camera roll to
learn it is unchanged would cost more than reading it).

## What comes in

| field | value |
|---|---|
| `title` | the file name (the original name, for a library or Takeout) |
| `body` | the caption, where the source has one (Photos title/caption, Takeout description, a camera's real ImageDescription) |
| `started_at` | the capture instant, UTC; a video's `ended_at` adds its duration |
| `utc_offset_sec` | the zone the camera was in, when known — a photo taken in Slovenia keeps `+02:00` (7200) |
| `value_json` | `kind` (photo / video / screenshot), `time_source`, `lat`, `lon`, `alt`, `make`, `model`, `width`, `height`, `orientation`, `duration_sec`, `favorite`, `hidden`, `albums`, `people` — only the fields that apply |
| `media_path` | absolute, for a folder; relative to the library package (`originals/A/ABC.heic`), for a Photos library; relative to the archive (`Google Photos/Photos from 2022/...`), for a Takeout — so a zip and its extraction are the same records |
| `origin_app` | `apple_photos`, `google_photos`, or a folder's camera make (`samsung`, `apple`) |

Formats read from their own containers, by seeking to the metadata and
stopping (at most 256 KB per file, usually a few KB):

| format | where the metadata is |
|---|---|
| JPEG | APP1 `Exif` segment -> TIFF IFDs; SOF for the pixel size |
| HEIC / HEIF / AVIF (iPhone default) | ISO-BMFF `meta` -> `iinf` (which item is `Exif`) -> `iloc` (where its bytes are) -> TIFF; `ispe` for the size |
| PNG | `eXIf` chunk; IHDR for the size |
| WebP | RIFF `EXIF` chunk; VP8X for the size |
| TIFF, DNG, most raw | the file is the TIFF |
| MP4 / MOV | `moov/mvhd` creation time (UTC) and duration, `tkhd` frame size, `udta/©xyz` (Android) and Apple's `mdta` keys for location and the local time with its offset |

Tags used: DateTimeOriginal (then DateTimeDigitized, a scanner's; DateTime is
the last edit and is never used), OffsetTimeOriginal, Make, Model, PixelXDimension/PixelYDimension, Orientation,
ImageDescription (minus camera boilerplate like `SAMSUNG` or `SONY DSC`), and
the GPS IFD — latitude/longitude/altitude with their refs, and the GPS clock.
SubSecTimeOriginal is deliberately ignored: the observed tier stores whole
seconds, and two reads of one file must compare equal. A damaged file gives
what could be read; one odd file never fails an import of thousands.

**Videos are included**, as `kind: video`, metadata only. The QuickTime header
is cheap to reach by seeking (a dozen reads find `moov` at the end of a 2 GB
file), and a video is as much evidence of a day as a photo. A Live Photo's
motion clip beside its still is skipped and counted — it is the same moment.

## Time, and which day a photo belongs to

`value_json.time_source` records where each capture time came from, best first:

| source | meaning |
|---|---|
| `library` | Photos' own capture date and zone (`ZDATECREATED`, `ZTIMEZONEOFFSET`) |
| `takeout` | Google's capture instant (`photoTakenTime`); the zone from the file's EXIF |
| `exif` | the camera's local time and its OffsetTimeOriginal |
| `exif_gps` | the camera's local time; the zone inferred from its GPS clock (only when the two agree to two minutes on a quarter-hour offset — a stale fix gives no zone rather than a wrong one) |
| `exif_local` | the camera's local time, zone unknown: read in the configured home timezone |
| `quicktime` | a video header: UTC, plus the zone where Apple wrote one |
| `filename` | an Android camera name: `20260727_213005.jpg` or `IMG_20260727_213005.jpg` (local time, read in the home zone), `PXL_20260727_213005123.jpg` (a Pixel names files in UTC) |
| `mtime` | the file's modification time — the weakest; a copy may reset it |

The **journal date is the local capture date where the zone is known**, with
the configured rollover hour applied in that zone: a photo taken at 09:00 in
Slovenia belongs to that Slovenian morning, not to the Austin night before.
Where the zone is unknown (`exif_local`, `filename`, `mtime`, a library row
with no offset) the configured home timezone decides, as it does for every
other connector.

## Names stay out

A photo names no phone number or account, so it has **no participants and no
handles**. Google Photos' face labels (`people[]` in the sidecar) are names the
owner typed into Google Photos — exactly what an importer may not assert as
identity. They are kept in `value_json.people` the way a caption is kept: text
the source holds, searchable, cited by row. They never become handles,
participants, entity links or aliases. The same goes for place names: turning
a position into "Lake Bled" is a derived layer, not import-time work.

## Identity, and what a re-import does

| source | external_id | on re-import |
|---|---|---|
| Photos library | `apple-photos:<ZUUID>` | **mirrors the library** |
| Takeout | `takeout:<path under Google Photos/>` | remembers |
| folder | `file:<path inside the folder>\|<size in bytes>` | remembers |

**The Photos library is mirrored** (the owner's rule for sources that hold a
whole history, 2026-09-24, as for texts and AI chats): a photo trashed
(Recently Deleted) or deleted in Photos is removed by the next import. Removal
is scoped to that one library — its Core Data store UUID (`Z_METADATA`) is the
account — so it never reaches another library, a Takeout or a folder. The
shared mirroring rules apply: a removal first takes a snapshot, a photo a
confirmed entry cites is hidden rather than deleted, and removing half or
more of the library, or five or more photos that are over a tenth of it, at
once is refused without `--allow-mass-removal`. A **hidden** photo is still in the library: skipped
unless `include_hidden = true`, never removed.

**Folders and Takeouts are remembered, not mirrored.** A folder is often a
partial copy — the last year synced to a new laptop, an SD card you cleared —
and a file missing from it proves nothing about the photo. A folder's id
includes the size, so a re-encoded or edited copy under the same name is a
new record rather than a silent overwrite. In a Takeout, one photo in three
albums is three files with three sidecars; it becomes one record (the
`Photos from YYYY` copy) listing its `albums`. Photos in Google's trash are
skipped.

Library schemas: `ZASSET` (macOS 11+) and `ZGENERICASSET` (macOS 10.15), with
columns introspected rather than assumed — a column a version lacks reads as
absent. Photos.sqlite is copied through SQLite's backup API (or, for a copied
library with a `-wal` and no `-shm`, the two files as they are) and the copy
is read; the live database is never opened in place, since Photos may be
running.

## Volume

Libraries run to 10k-100k photos. Files are listed, never loaded; each read
seeks to the metadata and stops. A Photos library is one SQL query over its
database and opens no original at all. A Takeout zip member is decompressed
to reach any offset, so reads inside a zip stop at 16 MB per member.

## Freshness

There is no `photos_data_age` doctor check, by design. A month with no photos
is an ordinary month, not a broken pipeline; a staleness warning would fire on
quiet stretches and train the owner to ignore warnings.

## Known limitations, by design

- No reverse geocoding: positions stay coordinates until a derived layer
  names them.
- A photo whose camera wrote no zone and no GPS clock is dated in the home
  timezone; a holiday shot like that may land on the neighbouring day.
- A Samsung or OneDrive copy that stripped EXIF falls back to the file name
  and then the mtime — the report's `time from:` line shows how many.
- Google Photos edits (a moved date or position) win over the file's EXIF in
  a Takeout, because the sidecar is what the owner last said.

## First-real-import checklist (unproven until real exports)

Everything above is tested against synthetic inputs built in the real
formats: JPEG/HEIF/PNG/WebP/MP4 files with real EXIF and box structures, a
Takeout with every sidecar-name generation, and a Photos.sqlite with the Core
Data tables Photos writes. What only real data can settle — note the answers
here:

| check | the report line that answers it |
|---|---|
| [ ] Which OneDrive folder the Samsung sync actually uses | `read from: …` |
| [ ] Do Samsung Gallery's OneDrive copies keep their EXIF? | `time from: exif N, filename N, mtime N` |
| [ ] Does a real iPhone HEIC's Exif item parse? | `time from:` with no `exif_local` for iPhone shots, `location: N of M have GPS` |
| [ ] Real Photos.sqlite on the owner's macOS version | `Photos library, macOS 11+ schema` and no error |
| [ ] Do library offsets match the trip? | the sample line's `UTC+02:00` on a Slovenia photo |
| [ ] Takeout sidecar names in a 2026 export | `skipped: … Takeout sidecar with no photo beside it` near zero |
| [ ] The oldest photo against what the phone shows | `per year: …` |

Answers so far: none — no real camera roll, Takeout or library has been read.
