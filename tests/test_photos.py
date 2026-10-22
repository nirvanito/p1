"""Photos connector: folders, Google Photos Takeouts, and Mac Photos libraries.

Every input is built in tmp_path the way the source writes it: camera JPEGs
with real EXIF (the demo generator's writer), HEIC and QuickTime containers
built box by box (test_photos_exif's builders), Takeout zips with Google's
sidecar naming in all its generations, and Photos.sqlite with the Core Data
columns Photos writes (ZASSET, or ZGENERICASSET on macOS 10.15).

What is pinned: the capture instant and the zone it was taken in (a photo in
Slovenia keeps +02:00 and its Slovenian date), where each time came from,
that names stay out of participants, that nothing is read beyond the
metadata, and the two identity rules — a Photos library is mirrored (trashed
and deleted photos leave, scoped to that library), folders and Takeouts are
remembered.
"""

import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lifecrm import platforms
from lifecrm.connectors import photos
from lifecrm.demo.sources.photos import LIBRARY_UUID, exif_tiff, jpeg_bytes, write_library
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks
from test_photos_exif import heic, png, quicktime

SLOVENIA = ZoneInfo("Europe/Ljubljana")
AUSTIN = ZoneInfo("America/Chicago")
BLED = (46.3625, 14.0936)
VERACRUZ = (30.2585, -97.7124)


def jpeg(path: Path, *, taken: datetime | None = None, offset: str | None = "+02:00",
         lat: float | None = BLED[0], lon: float | None = BLED[1],
         gps_utc: datetime | None = None, exif: bool = True,
         mtime: datetime | None = None, **kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tiff = None
    if exif:
        tiff = exif_tiff(make=kw.pop("make", "samsung"), model=kw.pop("model", "SM-S921U"),
                         taken=taken, offset=offset, subsec="482", width=96, height=72,
                         orientation=1, lat=lat, lon=lon, alt=kw.pop("alt", 476.2),
                         gps_utc=gps_utc, **kw)
    path.write_bytes(jpeg_bytes(96, 72, [(120, 170, 220), (40, 100, 140)], exif=tiff))
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def _detail(rec):
    return json.loads(rec["value_json"]) if rec["value_json"] else {}


def _by_title(scan):
    return {r["title"]: r for r in scan["records"]}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _camera_roll(root: Path) -> Path:
    gallery = root / "Samsung Gallery"
    jpeg(gallery / "DCIM" / "Camera" / "20220927_101210.jpg",
         taken=datetime(2022, 9, 27, 10, 12, 10))
    jpeg(gallery / "DCIM" / "Camera" / "20260913_194844.jpg",
         taken=datetime(2026, 9, 13, 19, 48, 44), offset="-05:00",
         lat=VERACRUZ[0], lon=VERACRUZ[1], alt=149.0)
    return gallery


# ---------------------------------------------------------------- folders

def test_a_folder_of_camera_photos_becomes_one_observation_each(cfg, tmp_path):
    gallery = _camera_roll(tmp_path)
    scan = photos.scan(gallery, cfg)
    assert scan["connector"] == "photos" and scan["target_kind"] == "folder"
    assert scan["account"] is None and scan["handles"] == []
    assert "present_ids" not in scan                     # a folder is remembered
    bled, tacos = sorted(scan["records"], key=lambda r: r["started_at"])
    size = (gallery / "DCIM/Camera/20220927_101210.jpg").stat().st_size
    assert bled["external_id"] == f"file:DCIM/Camera/20220927_101210.jpg|{size}"
    assert bled["stream"] == "photo" and bled["title"] == "20220927_101210.jpg"
    assert bled["started_at"] == "2022-09-27T08:12:10Z"
    assert bled["utc_offset_sec"] == 7200 and bled["journal_date"] == "2022-09-27"
    assert Path(bled["media_path"]) == (gallery / "DCIM/Camera/20220927_101210.jpg").resolve()
    assert bled["origin_app"] == "samsung" and bled["participants"] == []
    assert _detail(bled) == {"kind": "photo", "time_source": "exif", "lat": 46.3625,
                             "lon": 14.0936, "alt": 476.2, "make": "samsung",
                             "model": "SM-S921U", "width": 96, "height": 72,
                             "orientation": 1}
    assert tacos["started_at"] == "2026-09-14T00:48:44Z"
    assert tacos["utc_offset_sec"] == -18000 and tacos["journal_date"] == "2026-09-13"


def test_a_photo_keeps_the_zone_it_was_taken_in_and_its_local_date(cfg, tmp_path):
    """07:30 in Slovenia is 00:30 in Austin — before the rollover hour, so the
    home-zone rule would file it under the day before. It belongs to the
    Slovenian morning. The rollover itself still applies, in that zone."""
    jpeg(tmp_path / "p" / "morning.jpg", taken=datetime(2022, 9, 27, 7, 30, 0))
    jpeg(tmp_path / "p" / "late.jpg", taken=datetime(2022, 9, 28, 3, 0, 0))
    by = _by_title(photos.scan(tmp_path / "p", cfg))
    assert by["morning.jpg"]["started_at"] == "2022-09-27T05:30:00Z"
    assert by["morning.jpg"]["journal_date"] == "2022-09-27"
    assert by["late.jpg"]["journal_date"] == "2022-09-27"


def test_without_an_offset_the_gps_clock_supplies_the_zone(cfg, tmp_path):
    jpeg(tmp_path / "p" / "a.jpg", taken=datetime(2022, 9, 27, 10, 12, 10), offset=None,
         gps_utc=datetime(2022, 9, 27, 8, 12, 14, tzinfo=timezone.utc))
    rec = photos.scan(tmp_path / "p", cfg)["records"][0]
    assert rec["utc_offset_sec"] == 7200 and rec["started_at"] == "2022-09-27T08:12:10Z"
    assert _detail(rec)["time_source"] == "exif_gps"


def test_a_stale_gps_fix_is_not_used_to_guess_a_zone(cfg, tmp_path):
    jpeg(tmp_path / "p" / "a.jpg", taken=datetime(2022, 9, 27, 10, 12, 10), offset=None,
         gps_utc=datetime(2022, 9, 27, 8, 5, 10, tzinfo=timezone.utc))
    rec = photos.scan(tmp_path / "p", cfg)["records"][0]
    assert rec["utc_offset_sec"] is None
    assert rec["started_at"] == "2022-09-27T15:12:10Z"          # read in the home zone
    assert rec["journal_date"] == "2022-09-27"
    assert _detail(rec)["time_source"] == "exif_local"


def test_without_exif_the_file_name_then_the_mtime_dates_the_photo(cfg, tmp_path):
    folder = tmp_path / "p"
    for n in ("20260727_213005.jpg", "IMG_20260727_213005(1).jpg",
              "PXL_20260727_213005123.jpg", "Screenshot_20260727-213005_Chrome.jpg"):
        jpeg(folder / n, exif=False)
    jpeg(folder / "holiday.jpg", exif=False,
         mtime=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc))
    by = _by_title(photos.scan(folder, cfg))
    for n in ("20260727_213005.jpg", "IMG_20260727_213005(1).jpg",
              "Screenshot_20260727-213005_Chrome.jpg"):
        assert by[n]["started_at"] == "2026-07-28T02:30:05Z", n      # local, home zone
        assert by[n]["journal_date"] == "2026-07-27"
        assert _detail(by[n])["time_source"] == "filename"
    assert by["PXL_20260727_213005123.jpg"]["started_at"] == "2026-07-27T21:30:05Z"   # UTC
    assert _detail(by["Screenshot_20260727-213005_Chrome.jpg"])["kind"] == "screenshot"
    assert by["holiday.jpg"]["started_at"] == "2026-07-01T12:00:00Z"
    assert _detail(by["holiday.jpg"])["time_source"] == "mtime"
    assert all(r["utc_offset_sec"] is None for r in by.values())


def test_heic_png_and_videos_are_read_through_their_own_containers(cfg, tmp_path):
    folder = tmp_path / "Camera Roll"
    folder.mkdir()
    tiff = exif_tiff(make="Apple", model="iPhone 15 Pro",
                     taken=datetime(2022, 9, 27, 10, 12, 10), offset="+02:00",
                     lat=BLED[0], lon=BLED[1], little_endian=False)
    (folder / "IMG_1181.HEIC").write_bytes(heic(tiff))
    (folder / "Screenshot 2026-10-19 at 13.07.41.png").write_bytes(
        png(exif_tiff(taken=datetime(2026, 10, 19, 13, 7, 41), offset="-05:00")))
    (folder / "VID_20220928_150712.mp4").write_bytes(
        quicktime(created=datetime(2022, 9, 28, 13, 7, 12, tzinfo=timezone.utc)))
    (folder / "IMG_1185.MOV").write_bytes(quicktime(
        created=datetime(2022, 9, 28, 13, 9, 0, tzinfo=timezone.utc), xyz=None,
        apple={"com.apple.quicktime.creationdate": "2022-09-28T15:09:00+0200",
               "com.apple.quicktime.location.ISO6709": "+46.3627+014.0933/"}))
    by = _by_title(photos.scan(folder, cfg))

    still = by["IMG_1181.HEIC"]
    assert still["utc_offset_sec"] == 7200 and _detail(still)["model"] == "iPhone 15 Pro"
    assert (_detail(still)["width"], _detail(still)["height"]) == (4032, 3024)
    shot = _detail(by["Screenshot 2026-10-19 at 13.07.41.png"])
    assert shot["kind"] == "screenshot" and shot["width"] == 1179

    android = by["VID_20220928_150712.mp4"]
    assert android["started_at"] == "2022-09-28T13:07:12Z"
    assert android["ended_at"] == "2022-09-28T13:07:26Z"          # + 14.53 s, whole seconds
    assert android["utc_offset_sec"] is None
    assert _detail(android) == {"kind": "video", "time_source": "quicktime",
                                "duration_sec": 14.53, "width": 1920, "height": 1080,
                                "lat": 46.3625, "lon": 14.0936, "alt": 476.0}
    iphone = by["IMG_1185.MOV"]
    assert iphone["utc_offset_sec"] == 7200 and iphone["started_at"] == "2022-09-28T13:09:00Z"


def test_hidden_files_a_nested_library_and_non_photos_are_counted_not_imported(cfg, tmp_path):
    root = tmp_path / "Pictures"
    jpeg(root / "DCIM" / "Camera" / "20260727_213005.jpg", taken=datetime(2026, 7, 27, 21, 30, 5))
    jpeg(root / "DCIM" / ".thumbnails" / "1690.jpg", exif=False)
    jpeg(root / "DCIM" / "Camera" / "._20260727_213005.jpg", exif=False)   # AppleDouble
    (root / "Photos Library.photoslibrary" / "originals").mkdir(parents=True)
    jpeg(root / "Photos Library.photoslibrary" / "originals" / "A.jpeg", exif=False)
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / "desktop.ini").write_text("x", encoding="utf-8")
    (root / "DCIM" / "Camera" / "IMG_1.AAE").write_text("<plist/>", encoding="utf-8")
    scan = photos.scan(root, cfg)
    assert [r["title"] for r in scan["records"]] == ["20260727_213005.jpg"]
    assert set(scan["skipped"]) == {
        "1 × hidden file", "1 × hidden folder (not read)",
        "1 × Photos library inside the folder (import the library itself)",
        "3 × not a photo or video (.aae 1, .ini 1, .txt 1)"}


def test_a_live_photos_motion_clip_and_an_edited_copy_are_not_separate_photos(cfg, tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "IMG_1234.HEIC").write_bytes(heic(exif_tiff(
        taken=datetime(2022, 9, 27, 10, 0, 0), offset="+02:00")))
    (folder / "IMG_1234.MOV").write_bytes(quicktime(created=datetime(2022, 9, 27, 8, 0, 0,
                                                                     tzinfo=timezone.utc)))
    jpeg(folder / "IMG_2000.jpg", taken=datetime(2022, 9, 27, 11, 0, 0))
    jpeg(folder / "IMG_2000-edited.jpg", taken=datetime(2022, 9, 27, 11, 0, 0))
    scan = photos.scan(folder, cfg)
    assert sorted(_by_title(scan)) == ["IMG_1234.HEIC", "IMG_2000.jpg"]
    assert "1 × Live Photo motion clip (its still is imported)" in scan["skipped"]
    assert "1 × edited copy (the original is imported)" in scan["skipped"]


def test_a_cloud_only_file_is_counted_and_never_opened(cfg, tmp_path, monkeypatch):
    gallery = _camera_roll(tmp_path)
    monkeypatch.setattr(photos, "_cloud_only", lambda st: True)

    def opened(f):
        raise AssertionError(f"opened {f.rel}, which would download it")
    monkeypatch.setattr(photos, "_read_meta", opened)
    scan = photos.scan(gallery, cfg)
    assert scan["records"] == []
    assert scan["skipped"] == [f"2 × {photos._CLOUD_ONLY}"]


def test_a_damaged_photo_is_imported_from_what_can_be_read(cfg, tmp_path):
    whole = jpeg(tmp_path / "x.jpg", taken=datetime(2026, 7, 27, 21, 30, 5)).read_bytes()
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "20260727_213005.jpg").write_bytes(whole[:60])
    rec = photos.scan(folder, cfg)["records"][0]
    assert _detail(rec)["time_source"] == "filename"


def test_a_folder_with_no_photos_says_what_it_saw(cfg, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DomainError, match=r"no photos or videos in docs \(1 file\(s\) seen: .txt 1\)"):
        photos.scan(tmp_path / "docs", cfg)
    (tmp_path / "notes.csv").write_text("a,b", encoding="utf-8")
    with pytest.raises(DomainError, match="is not a photo, a video, a folder"):
        photos.scan(tmp_path / "notes.csv", cfg)


def test_a_single_photo_can_be_the_target(cfg, tmp_path):
    path = jpeg(tmp_path / "IMG_0001.jpg", taken=datetime(2026, 7, 27, 21, 30, 5))
    scan = photos.scan(path, cfg)
    assert scan["target_kind"] == "file"
    assert scan["records"][0]["external_id"] == f"file:IMG_0001.jpg|{path.stat().st_size}"


def test_two_scans_are_identical(cfg, tmp_path):
    gallery = _camera_roll(tmp_path)
    a, b = photos.scan(gallery, cfg), photos.scan(gallery, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]
    jpeg(gallery / "DCIM" / "Camera" / "20260914_120000.jpg", taken=datetime(2026, 9, 14, 12))
    assert photos.scan(gallery, cfg)["source_hash"] != a["source_hash"]


def test_the_report_counts_years_time_sources_and_gps(cfg, conn, tmp_path):
    gallery = _camera_roll(tmp_path)
    jpeg(gallery / "DCIM" / "Screenshots" / "Screenshot_20261019_130741_Chrome.jpg", exif=False)
    scan = photos.scan(gallery, cfg)
    text = "\n".join(photos.report_lines(scan, obs.build_plan(conn, scan)))
    assert "photos: Samsung Gallery (folder; 2 photos, 1 screenshot)" in text
    assert "per year: 2022 1, 2026 2" in text
    assert "time from: exif 2, filename 1 (1 without the camera's own zone)" in text
    assert "location: 2 of 3 have GPS" in text
    assert "photo: 3 new, 0 changed | 2022-09-27..2026-10-19 | e.g. 2026-10-19 " \
           "Screenshot_20261019_130741_Chrome.jpg (no GPS)" in text


def test_reimporting_a_folder_is_a_no_op_and_a_removed_file_is_remembered(cfg, conn, tmp_path):
    gallery = _camera_roll(tmp_path)
    _, first = _apply(conn, photos.scan(gallery, cfg))
    assert first["added"] == 2
    plan, again = _apply(conn, photos.scan(gallery, cfg))
    assert again["added"] == 0 and again["identical"] == 2
    (gallery / "DCIM" / "Camera" / "20260913_194844.jpg").unlink()
    plan = obs.build_plan(conn, photos.scan(gallery, cfg))
    assert plan["removed"] == [] and "mirror_note" not in plan
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE stream='photo'"
                        ).fetchone()["c"] == 2


# ---------------------------------------------------------------- Google Photos Takeout

def sidecar(title: str, taken: datetime, *, geo=(0.0, 0.0), geo_exif=(0.0, 0.0),
            description: str = "", people=(), favorited=False, trashed=False,
            bom: bool = False) -> bytes:
    stamp = str(int(taken.timestamp()))
    data = {
        "title": title, "description": description, "imageViews": "4",
        "creationTime": {"timestamp": str(int(taken.timestamp()) + 600),
                         "formatted": "upload time"},
        "photoTakenTime": {"timestamp": stamp, "formatted": "capture time"},
        "geoData": {"latitude": geo[0], "longitude": geo[1], "altitude": 0.0,
                    "latitudeSpan": 0.0, "longitudeSpan": 0.0},
        "geoDataExif": {"latitude": geo_exif[0], "longitude": geo_exif[1],
                        "altitude": 476.0, "latitudeSpan": 0.0, "longitudeSpan": 0.0},
        "people": [{"name": n} for n in people],
        "url": f"https://photos.google.com/photo/AF1Qip{stamp}",
        "googlePhotosOrigin": {"mobileUpload": {"deviceType": "ANDROID_PHONE"}},
    }
    if favorited:
        data["favorited"] = True
    if trashed:
        data["trashed"] = True
    raw = json.dumps(data, indent=2).encode("utf-8")
    return (b"\xef\xbb\xbf" + raw) if bom else raw


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


LONG_NAME = "Screenshot_20220929-093000_Samsung Internet.jpg"     # 47 characters


def _takeout_members(tmp_path: Path) -> dict[str, bytes]:
    year = "Takeout/Google Photos/Photos from 2022/"
    island = jpeg(tmp_path / "src" / "island.jpg", taken=datetime(2022, 9, 27, 10, 12, 10),
                  lat=BLED[0], lon=BLED[1]).read_bytes()
    plain = jpeg(tmp_path / "src" / "plain.jpg", exif=False).read_bytes()
    island_json = sidecar("IMG_20220927_101210.jpg", _utc(2022, 9, 27, 8, 12, 10),
                          geo_exif=BLED, description="The island church from the shore",
                          people=("Jake Morales", "Maya Chen"), favorited=True)
    return {
        "Takeout/archive_browser.html": b"<html></html>",
        year + "IMG_20220927_101210.jpg": island,
        year + "IMG_20220927_101210.jpg.supplemental-metadata.json": island_json,
        "Takeout/Google Photos/Bled 2022/IMG_20220927_101210.jpg": island,
        "Takeout/Google Photos/Bled 2022/IMG_20220927_101210.jpg.supplemental-metadata.json":
            island_json,
        "Takeout/Google Photos/Bled 2022/metadata.json": b'{"title": "Bled 2022"}',
        year + "IMG_20220928_150501.jpg": plain,
        year + "IMG_20220928_150501.jpg.json":
            sidecar("IMG_20220928_150501.jpg", _utc(2022, 9, 28, 13, 5, 1), geo=BLED),
        year + "IMG_20220928_150501(1).jpg": plain,
        year + "IMG_20220928_150501.jpg(1).json":
            sidecar("IMG_20220928_150501.jpg", _utc(2022, 9, 28, 13, 6, 0)),
        year + LONG_NAME: plain,
        year + LONG_NAME[:46] + ".json": sidecar(LONG_NAME, _utc(2022, 9, 29, 7, 30, 0)),
        year + "IMG_20220930_200255.jpg": plain,
        year + "IMG_20220930_200255.jpg.supplemental-metad.json":
            sidecar("IMG_20220930_200255.jpg", _utc(2022, 9, 30, 18, 2, 55), bom=True),
        year + "IMG_20220926_172044.jpg": plain,
        year + "IMG_20220926_172044.jpg.json":
            sidecar("IMG_20220926_172044.jpg", _utc(2022, 9, 26, 15, 20, 44), trashed=True),
        year + "IMG_20220925_120000.jpg.json":
            sidecar("IMG_20220925_120000.jpg", _utc(2022, 9, 25, 10, 0, 0)),
    }


def takeout_zip(tmp_path: Path, name: str = "takeout-20261020T120000Z-001.zip") -> Path:
    z = tmp_path / name
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in _takeout_members(tmp_path).items():
            zf.writestr(member, data)
    return z


def test_a_takeout_zip_reads_googles_sidecars(cfg, tmp_path):
    scan = photos.scan(takeout_zip(tmp_path), cfg)
    assert scan["target_kind"] == "takeout" and scan["handles"] == []
    ids = {r["external_id"] for r in scan["records"]}
    assert ids == {"takeout:Photos from 2022/IMG_20220927_101210.jpg",
                   "takeout:Photos from 2022/IMG_20220928_150501.jpg",
                   "takeout:Photos from 2022/IMG_20220928_150501(1).jpg",
                   f"takeout:Photos from 2022/{LONG_NAME}",
                   "takeout:Photos from 2022/IMG_20220930_200255.jpg"}
    by = {r["external_id"]: r for r in scan["records"]}
    island = by["takeout:Photos from 2022/IMG_20220927_101210.jpg"]
    assert island["started_at"] == "2022-09-27T08:12:10Z"
    assert island["utc_offset_sec"] == 7200                 # the zone, from the file's EXIF
    assert island["journal_date"] == "2022-09-27"
    assert island["body"] == "The island church from the shore"
    assert island["media_path"] == "Google Photos/Photos from 2022/IMG_20220927_101210.jpg"
    assert island["origin_app"] == "google_photos" and island["participants"] == []
    d = _detail(island)
    assert d["people"] == ["Jake Morales", "Maya Chen"]       # face labels: data, not handles
    assert d["albums"] == ["Bled 2022"] and d["favorite"] is True
    assert (d["lat"], d["lon"]) == BLED                      # geoData 0,0 -> geoDataExif
    assert d["time_source"] == "takeout" and d["make"] == "samsung"

    plain = by["takeout:Photos from 2022/IMG_20220928_150501.jpg"]
    assert plain["utc_offset_sec"] is None and plain["title"] == "IMG_20220928_150501.jpg"
    assert (_detail(plain)["lat"], _detail(plain)["lon"]) == BLED
    dup = by["takeout:Photos from 2022/IMG_20220928_150501(1).jpg"]
    assert dup["started_at"] == "2022-09-28T13:06:00Z"         # its own '(1)' sidecar
    assert _detail(by[f"takeout:Photos from 2022/{LONG_NAME}"])["kind"] == "screenshot"
    assert by["takeout:Photos from 2022/IMG_20220930_200255.jpg"]["started_at"] == \
        "2022-09-30T18:02:55Z"                               # cut-short name, BOM

    assert set(scan["skipped"]) == {
        "1 × album copy of a photo already listed (its album is recorded)",
        "1 × in Google Photos' trash",
        "1 × not a photo or video (.html 1)",
        "1 × Takeout sidecar with no photo beside it (a split Takeout? extract every "
        "part into one folder)"}


def test_sidecar_names_of_every_generation_find_their_photo():
    names = ["a.jpg.json", "b.jpg.supplemental-metadata.json",
             "c.jpg.supplemental-metad.json", "d.jpg(1).json",
             "e.jpg.supplemental-metadata(2).json", LONG_NAME[:46] + ".json",
             "f(1).jpg.json", "metadata.json"]
    found = photos._Sidecars(names)
    assert found.find("a.jpg") == "a.jpg.json"
    assert found.find("b.jpg") == "b.jpg.supplemental-metadata.json"
    assert found.find("c.jpg") == "c.jpg.supplemental-metad.json"
    assert found.find("d(1).jpg") == "d.jpg(1).json"
    assert found.find("d.jpg") is None
    assert found.find("e(2).jpg") == "e.jpg.supplemental-metadata(2).json"
    assert found.find(LONG_NAME) == LONG_NAME[:46] + ".json"
    assert found.find("f(1).jpg") == "f(1).jpg.json"
    assert found.find("x.jpg") is None


def test_the_extracted_takeout_is_the_same_records_as_its_zip(cfg, conn, tmp_path):
    z = takeout_zip(tmp_path)
    _apply(conn, photos.scan(z, cfg))
    out = tmp_path / "extracted"
    with zipfile.ZipFile(z) as zf:
        zf.extractall(out)
    plan = obs.build_plan(conn, photos.scan(out, cfg))
    assert plan["new"] == [] and plan["conflicts"] == [] and plan["updates"] == []
    assert plan["identical"] == 5


def test_takeout_parts_in_one_folder_are_read_together(cfg, tmp_path):
    members = _takeout_members(tmp_path)
    photo = "Takeout/Google Photos/Photos from 2022/IMG_20220927_101210.jpg"
    folder = tmp_path / "downloads"
    folder.mkdir()
    with zipfile.ZipFile(folder / "takeout-20261020T120000Z-001.zip", "w") as zf:
        zf.writestr(photo, members[photo])
    with zipfile.ZipFile(folder / "takeout-20261020T120000Z-002.zip", "w") as zf:
        zf.writestr(photo + ".supplemental-metadata.json",
                    members[photo + ".supplemental-metadata.json"])
    scan = photos.scan(folder, cfg)
    assert scan["target_kind"] == "takeout"
    (rec,) = scan["records"]
    assert _detail(rec)["time_source"] == "takeout" and rec["body"]


# ---------------------------------------------------------------- Photos library

def _asset(uid: str, created: datetime, **kw) -> dict:
    return {"uuid": uid, "created": created, "lat": BLED[0], "lon": BLED[1],
            "make": "Apple", "model": "iPhone 13 Pro", **kw}


def _assets() -> list[dict]:
    return [
        _asset("AAAA-0001", datetime(2022, 9, 27, 10, 12, 10, tzinfo=SLOVENIA),
               original_filename="IMG_1181.HEIC", title="Lake Bled",
               caption="The island church from the shore", favorite=True),
        _asset("BBBB-0002", datetime(2022, 9, 28, 15, 7, 12, tzinfo=SLOVENIA), kind=1,
               duration=14.53, ext="mov", uti="com.apple.quicktime-movie",
               original_filename="IMG_1185.MOV", width=1920, height=1080),
        _asset("CCCC-0003", datetime(2026, 10, 19, 13, 7, 41, tzinfo=AUSTIN), subtype=10,
               lat=None, lon=None, make=None, model=None, ext="png",
               original_filename="IMG_7409.PNG"),
        _asset("DDDD-0004", datetime(2026, 9, 13, 19, 48, 31, tzinfo=AUSTIN), trashed=True),
        _asset("EEEE-0005", datetime(2026, 9, 27, 21, 15, 3, tzinfo=AUSTIN), hidden=True,
               lat=None, lon=None),
    ]


def make_library(root: Path, assets=None, *, table="ZASSET",
                 library_uuid=LIBRARY_UUID) -> Path:
    lib = root / "Photos Library.photoslibrary"
    (lib / "database").mkdir(parents=True, exist_ok=True)
    db = lib / "database" / "Photos.sqlite"
    if db.exists():
        db.unlink()
    write_library(db, _assets() if assets is None else assets, table=table,
                  library_uuid=library_uuid)
    return lib


def test_a_photos_library_reads_time_zone_place_caption_and_camera(cfg, tmp_path):
    scan = photos.scan(make_library(tmp_path), cfg)
    assert scan["target_kind"] == "library" and scan["schema"] == "macOS 11+"
    assert scan["account"] == f"photos-library:{LIBRARY_UUID}"
    by = {r["external_id"]: r for r in scan["records"]}
    assert set(by) == {"apple-photos:AAAA-0001", "apple-photos:BBBB-0002",
                       "apple-photos:CCCC-0003"}
    lake = by["apple-photos:AAAA-0001"]
    assert lake["started_at"] == "2022-09-27T08:12:10Z" and lake["utc_offset_sec"] == 7200
    assert lake["journal_date"] == "2022-09-27" and lake["title"] == "IMG_1181.HEIC"
    assert lake["body"] == "Lake Bled — The island church from the shore"
    assert lake["media_path"] == "originals/A/AAAA-0001.heic"
    assert lake["origin_app"] == "apple_photos"
    assert _detail(lake) == {"kind": "photo", "time_source": "library", "lat": 46.3625,
                             "lon": 14.0936, "make": "Apple", "model": "iPhone 13 Pro",
                             "width": 4032, "height": 3024, "orientation": 1,
                             "favorite": True}
    video = by["apple-photos:BBBB-0002"]
    assert _detail(video)["kind"] == "video" and _detail(video)["duration_sec"] == 14.53
    assert video["ended_at"] == "2022-09-28T13:07:26Z"
    shot = by["apple-photos:CCCC-0003"]
    assert _detail(shot)["kind"] == "screenshot" and "lat" not in _detail(shot)
    assert shot["utc_offset_sec"] == -18000
    assert set(scan["skipped"]) == {
        "1 × hidden in Photos (set [connectors.photos] include_hidden = true to import)",
        "1 × in Recently Deleted (removed from the archive if it was imported)"}
    assert scan["present_ids"] == {"apple-photos:AAAA-0001", "apple-photos:BBBB-0002",
                                   "apple-photos:CCCC-0003", "apple-photos:EEEE-0005"}


def test_the_catalina_schema_reads_without_camera_details(cfg, tmp_path):
    scan = photos.scan(make_library(tmp_path, table="ZGENERICASSET"), cfg)
    assert scan["schema"] == "macOS 10.15" and len(scan["records"]) == 3
    assert all("make" not in _detail(r) for r in scan["records"])


def test_hidden_photos_import_when_the_owner_asks(cfg, tmp_path):
    cfg.connectors = {"photos": {"include_hidden": True}}
    by = {r["external_id"]: r for r in photos.scan(make_library(tmp_path), cfg)["records"]}
    assert _detail(by["apple-photos:EEEE-0005"])["hidden"] is True


def test_the_library_and_its_database_file_are_the_same_target(cfg, tmp_path):
    lib = make_library(tmp_path)
    a = photos.scan(lib, cfg)
    b = photos.scan(lib / "database" / "Photos.sqlite", cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]
    assert a["source_hash"] == photos.scan(lib, cfg)["source_hash"]


def test_a_database_that_is_not_a_photos_library_is_refused(cfg, tmp_path):
    db = tmp_path / "other.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE notes (id INTEGER)")
    c.commit()
    c.close()
    with pytest.raises(DomainError, match=r"not a Photos library .*\(has: notes\)"):
        photos.scan(db, cfg)
    (tmp_path / "Empty.photoslibrary").mkdir()
    with pytest.raises(DomainError, match="has no database/Photos.sqlite"):
        photos.scan(tmp_path / "Empty.photoslibrary", cfg)


def _add_asset_in_wal(db: Path, uid: str) -> sqlite3.Connection:
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("INSERT INTO ZASSET (Z_PK, ZKIND, ZUUID, ZDATECREATED, ZLATITUDE, ZLONGITUDE, "
              "ZFILENAME, ZDIRECTORY) VALUES (99, 0, ?, 700000000.0, -180.0, -180.0, ?, ?)",
              (uid, f"{uid}.heic", uid[0]))
    c.commit()
    return c                                # left open: the write stays in the -wal


def test_a_live_library_is_copied_never_read_in_place(cfg, tmp_path):
    lib = make_library(tmp_path)
    db = lib / "database" / "Photos.sqlite"
    writer = _add_asset_in_wal(db, "FFFF-0006")
    try:
        before = db.read_bytes()
        ids = {r["external_id"] for r in photos.scan(lib, cfg)["records"]}
        assert "apple-photos:FFFF-0006" in ids          # read from the -wal
        assert db.read_bytes() == before
    finally:
        writer.close()


def test_a_copied_library_with_a_wal_and_no_shm(cfg, tmp_path):
    lib = make_library(tmp_path / "mac")
    db = lib / "database" / "Photos.sqlite"
    writer = _add_asset_in_wal(db, "FFFF-0006")
    try:
        copy = tmp_path / "usb" / "Photos Library.photoslibrary" / "database"
        copy.mkdir(parents=True)
        shutil.copyfile(db, copy / "Photos.sqlite")
        shutil.copyfile(db.with_name("Photos.sqlite-wal"), copy / "Photos.sqlite-wal")
    finally:
        writer.close()
    ids = {r["external_id"] for r in photos.scan(copy.parent, cfg)["records"]}
    assert "apple-photos:FFFF-0006" in ids


def test_a_permission_error_says_how_to_grant_access(cfg, tmp_path, monkeypatch):
    def denied(target, cfg):
        raise PermissionError(1, "Operation not permitted",
                              "/Users/alex/Pictures/Photos Library.photoslibrary")
    monkeypatch.setattr(photos, "_scan", denied)
    monkeypatch.setattr(platforms, "is_mac", lambda: True)
    with pytest.raises(DomainError, match=r"Photos Library\.photoslibrary: permission "
                                          r"denied — grant Full Disk Access"):
        photos.scan(tmp_path, cfg)


# ---------------------------------------------------------------- the library is mirrored

def test_a_photo_trashed_or_deleted_in_photos_leaves_the_archive(cfg, conn, tmp_path):
    _apply(conn, photos.scan(make_library(tmp_path), cfg))
    assets = _assets()
    assets[0]["trashed"] = True                          # the Bled photo, trashed
    del assets[2]                                        # the screenshot, gone entirely
    scan = photos.scan(make_library(tmp_path, assets), cfg)
    plan = obs.build_plan(conn, scan)
    assert sorted(r["external_id"] for r in plan["removed"]) == [
        "apple-photos:AAAA-0001", "apple-photos:CCCC-0003"]
    text = "\n".join(photos.report_lines(scan, plan))
    assert "remove: 2 no longer in the library (trashed or deleted)" in text
    _, result = _apply(conn, scan)
    assert result["removed"] == 2
    left = {r["external_id"] for r in conn.execute(
        "SELECT external_id FROM observations WHERE status='active'")}
    assert left == {"apple-photos:BBBB-0002"}
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_photo_hidden_after_import_stays(cfg, conn, tmp_path):
    _apply(conn, photos.scan(make_library(tmp_path), cfg))
    assets = _assets()
    assets[1]["hidden"] = True
    plan = obs.build_plan(conn, photos.scan(make_library(tmp_path, assets), cfg))
    assert plan["removed"] == []


def test_removal_never_reaches_another_library_or_a_folder(cfg, conn, tmp_path):
    _apply(conn, photos.scan(make_library(tmp_path / "one"), cfg))
    _apply(conn, photos.scan(_camera_roll(tmp_path), cfg))
    other = [_asset("ZZZZ-0009", datetime(2026, 1, 1, 12, tzinfo=AUSTIN))]
    scan = photos.scan(make_library(tmp_path / "two", other,
                                    library_uuid="0D9E8F7A-0000-4000-8000-000000000002"), cfg)
    plan = obs.build_plan(conn, scan)
    assert plan["removed"] == [] and plan["mirror_scope"] == 0
    _apply(conn, scan)
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 3 + 2 + 1


def test_trashing_most_of_the_library_at_once_is_refused(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "MASS_REMOVAL_MIN", 1)
    _apply(conn, photos.scan(make_library(tmp_path), cfg))
    assets = _assets()
    for a in assets[:3]:
        a["trashed"] = True
    scan = photos.scan(make_library(tmp_path, assets), cfg)
    plan = obs.build_plan(conn, scan)
    assert obs.mass_removal(plan)
    with pytest.raises(DomainError):
        obs.apply_plan(conn, scan, plan)
    conn.rollback()


# ---------------------------------------------------------------- the confirmed tier is untouched

def test_the_importer_writes_none_of_the_confirmed_tables(cfg, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs", "entity_handles")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    _apply(conn, photos.scan(make_library(tmp_path), cfg))
    _apply(conn, photos.scan(takeout_zip(tmp_path), cfg))
    _apply(conn, photos.scan(_camera_roll(tmp_path), cfg))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    assert after == before
    assert conn.execute("SELECT COUNT(*) c FROM observation_participants").fetchone()["c"] == 0


def test_doctor_is_green_after_every_kind_of_photos_import(cfg, conn, tmp_path):
    from test_doctor import _seed
    _seed(conn, cfg)
    for target in (make_library(tmp_path), takeout_zip(tmp_path), _camera_roll(tmp_path)):
        _apply(conn, photos.scan(target, cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
    assert not any(c["check"] == "photos_data_age" for c in report["checks"])


# ---------------------------------------------------------------- finding the photos

def test_no_path_reads_the_configured_folder_first(cfg, tmp_path):
    cfg.connectors = {"photos": {"path": str(tmp_path / "pics")}}
    assert photos.default_target(cfg) == tmp_path / "pics"
    cfg.connectors = {"photos": {"path": "~/Pictures/Camera Roll"}}
    assert "~" not in str(photos.default_target(cfg))


def test_no_path_on_a_mac_reads_the_photos_library(cfg, tmp_path, monkeypatch):
    lib = make_library(tmp_path)
    monkeypatch.setattr(platforms, "is_mac", lambda: True)
    monkeypatch.setattr(platforms, "mac_photos_db", lambda: lib / "database" / "Photos.sqlite")
    assert photos.default_target(cfg) == lib
    monkeypatch.setattr(platforms, "mac_photos_db",
                        lambda: tmp_path / "none.photoslibrary" / "database" / "Photos.sqlite")
    with pytest.raises(DomainError, match="no Photos library at"):
        photos.default_target(cfg)


def test_no_path_on_windows_finds_the_one_onedrive_camera_folder(cfg, tmp_path, monkeypatch):
    onedrive = tmp_path / "OneDrive"
    monkeypatch.setattr(platforms, "is_mac", lambda: False)
    monkeypatch.setattr(platforms, "onedrive_candidates", lambda: [onedrive])
    with pytest.raises(DomainError, match="no camera folder found — looked for "
                                          "Pictures/Camera Roll"):
        photos.default_target(cfg)
    (onedrive / "Pictures" / "Samsung Gallery").mkdir(parents=True)
    assert photos.default_target(cfg) == onedrive / "Pictures" / "Samsung Gallery"
    (onedrive / "Pictures" / "Camera Roll").mkdir()
    with pytest.raises(DomainError, match="2 camera folders found — pass one of them"):
        photos.default_target(cfg)


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_apply_reimport_and_the_configured_default(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    gallery = _camera_roll(tmp_path / "synced" / "Pictures")

    r = runner.invoke(app, ["observe", "photos", str(gallery)])
    assert r.exit_code == 0, r.output
    assert "photos: Samsung Gallery (folder; 2 photos)" in r.output
    assert "dry run — nothing written" in r.output

    r = runner.invoke(app, ["observe", "photos", str(gallery), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 2 added" in r.output

    with open(root / "config.toml", "a", encoding="utf-8") as f:
        f.write(f"[connectors.photos]\npath = '{gallery}'\n")
    r = runner.invoke(app, ["observe", "photos"])
    assert r.exit_code == 0, r.output
    assert "2 identical (skipped)" in r.output
    assert "this exact file was already imported" in r.output

    r = runner.invoke(app, ["sql", "SELECT stream, utc_offset_sec FROM observations "
                                   "ORDER BY started_at", "--json"])
    assert [row["utc_offset_sec"] for row in json.loads(r.output)] == [7200, -18000]
    r = runner.invoke(app, ["connectors"])
    assert "photos" in r.output and "photo" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0
