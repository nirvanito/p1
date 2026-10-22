"""The demo's photo samples (lifecrm/demo/sources/photos.py) through the real
connector.

Both combos must tell the persona's story the way the phones would: Alex's
Samsung Gallery folder of camera JPEGs, and the MacBook's Photos library.
The facts pinned here are the ones a demo question leans on — a photo at
Veracruz All Natural, the Lake Bled trip four years back with its Slovenian
zone and dates, the camera — plus the generator contract: deterministic for
(anchor, combo), ValueError for a combo without this source, and an import
that applies once, re-applies as all-identical, and leaves doctor green.
"""

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from lifecrm.connectors import photos
from lifecrm.demo import persona
from lifecrm.demo.sources import photos as sample
from lifecrm.domain import observations as obs
from lifecrm.doctor import run_checks

ANCHOR = date(2026, 10, 21)          # the trip falls in late September: CEST, +02:00


def _detail(rec):
    return json.loads(rec["value_json"])


def _near(rec, place_key, within=0.001):
    d, p = _detail(rec), persona.place(place_key)
    return "lat" in d and abs(d["lat"] - p.lat) < within and abs(d["lon"] - p.lon) < within


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(f.read_bytes())
        h.update(str(int(f.stat().st_mtime)).encode())
    return h.hexdigest()


# ---------------------------------------------------------------- android

def test_the_android_sample_is_a_samsung_gallery_of_real_camera_jpegs(tmp_path):
    gallery = sample.write_sample(tmp_path, ANCHOR, "android")
    assert gallery == tmp_path / "Samsung Gallery"
    camera = sorted((gallery / "DCIM" / "Camera").iterdir())
    assert len(camera) == len(persona.PHOTOS)
    for f in camera:
        data = f.read_bytes()
        assert data[:4] == b"\xff\xd8\xff\xe1" and data[6:12] == b"Exif\x00\x00"
        assert data[-2:] == b"\xff\xd9"
        assert len(f.stem) == 15 and f.stem[8] == "_"          # 20220927_101210
    (shot,) = (gallery / "DCIM" / "Screenshots").iterdir()
    assert shot.name.startswith("Screenshot_") and shot.name.endswith("_Chrome.jpg")


def test_the_android_sample_tells_the_persona_story(cfg, tmp_path):
    scan = photos.scan(sample.write_sample(tmp_path, ANCHOR, "android"), cfg)
    records = scan["records"]
    assert len(records) == len(persona.PHOTOS) + 1
    cameras = [r for r in records if _detail(r)["kind"] == "photo"]
    assert {(_detail(r)["make"], _detail(r)["model"]) for r in cameras} == \
        {("samsung", "SM-S921U")}
    assert all(_detail(r)["time_source"] == "exif" for r in cameras)

    tacos = [r for r in records if _near(r, "veracruz")]
    assert [r["journal_date"] for r in tacos] == [str(persona.day(ANCHOR, -38))]
    assert tacos[0]["utc_offset_sec"] == -5 * 3600

    trip = {str(d) for d in persona.bled_trip_days(ANCHOR)}
    slovenia = [r for r in records
                if any(_near(r, k) for k in ("bled", "bled_castle", "ljubljana"))]
    assert len(slovenia) == sum(1 for p in persona.PHOTOS if p.offset is None) == 6
    assert {r["journal_date"] for r in slovenia} <= trip
    assert all(r["utc_offset_sec"] == 7200 for r in slovenia)
    assert all(r["journal_date"].startswith(str(ANCHOR.year - 4)) for r in slovenia)

    (shot,) = [r for r in records if _detail(r)["kind"] == "screenshot"]
    assert _detail(shot)["time_source"] == "filename"
    assert shot["journal_date"] == str(persona.day(ANCHOR, -2))


# ---------------------------------------------------------------- iphone

def test_the_iphone_sample_is_a_photos_library_with_the_same_story(cfg, tmp_path):
    lib = sample.write_sample(tmp_path, ANCHOR, "iphone")
    assert lib.name == "Photos Library.photoslibrary"
    assert (lib / "database" / "Photos.sqlite").is_file()
    scan = photos.scan(lib, cfg)
    assert scan["schema"] == "macOS 11+"
    assert scan["account"] == f"photos-library:{sample.LIBRARY_UUID}"
    records = scan["records"]
    assert len(records) == len(persona.PHOTOS) + 2              # + the video, the screenshot

    by_body = {r["body"]: r for r in records if r["body"]}
    assert set(by_body) == {p.caption for p in persona.PHOTOS if p.caption} | {
        "Jake rowing us out to the island"}
    island = by_body["The island church from the shore"]
    assert _detail(island)["favorite"] is True and island["utc_offset_sec"] == 7200
    assert _detail(island)["model"] == persona.DEVICE["iphone"]["phone"]

    video = by_body["Jake rowing us out to the island"]
    assert _detail(video)["kind"] == "video" and video["ended_at"] > video["started_at"]
    assert video["journal_date"] == str(persona.bled_trip_days(ANCHOR)[2])

    tacos = [r for r in records if _near(r, "veracruz")]
    assert len(tacos) == 1                                       # the blurry one is trashed
    assert set(scan["skipped"]) == {
        "1 × hidden in Photos (set [connectors.photos] include_hidden = true to import)",
        "1 × in Recently Deleted (removed from the archive if it was imported)"}
    titles = sorted(r["title"] for r in records)
    assert titles[0].startswith("IMG_1") and titles[-1].startswith("IMG_7")


def test_a_winter_anchor_puts_the_trip_in_central_european_winter_time(cfg, tmp_path):
    anchor = date(2027, 1, 20)                                    # the trip: late December
    for combo in ("android", "iphone"):
        scan = photos.scan(sample.write_sample(tmp_path / combo, anchor, combo), cfg)
        bled = [r for r in scan["records"] if _near(r, "bled")]
        assert bled and all(r["utc_offset_sec"] == 3600 for r in bled), combo


# ---------------------------------------------------------------- the generator contract

@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_a_sample_applies_once_reapplies_identical_and_doctor_stays_green(
        combo, cfg, conn, tmp_path):
    target = sample.write_sample(tmp_path, ANCHOR, combo)
    scan = photos.scan(target, cfg)
    first = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert first["added"] == len(scan["records"])
    again = photos.scan(target, cfg)
    plan = obs.build_plan(conn, again)
    assert plan["new"] == [] and plan["updates"] == [] and plan["conflicts"] == []
    assert plan["removed"] == [] and plan["identical"] == len(scan["records"])
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_samples_are_deterministic_for_an_anchor_and_combo(cfg, tmp_path):
    a = sample.write_sample(tmp_path / "a", ANCHOR, "android")
    b = sample.write_sample(tmp_path / "b", ANCHOR, "android")
    assert _tree_digest(a) == _tree_digest(b)
    c = sample.write_sample(tmp_path / "c", ANCHOR + timedelta(days=1), "android")
    assert _tree_digest(a) != _tree_digest(c)
    x = photos.scan(sample.write_sample(tmp_path / "x", ANCHOR, "iphone"), cfg)
    y = photos.scan(sample.write_sample(tmp_path / "y", ANCHOR, "iphone"), cfg)
    assert x["records"] == y["records"] and x["source_hash"] == y["source_hash"]


def test_rewriting_the_iphone_sample_in_place_replaces_the_library(cfg, tmp_path):
    sample.write_sample(tmp_path, ANCHOR, "iphone")
    lib = sample.write_sample(tmp_path, ANCHOR, "iphone")
    assert len(photos.scan(lib, cfg)["records"]) == len(persona.PHOTOS) + 2


def test_a_combo_without_this_source_raises():
    with pytest.raises(ValueError, match="photos has no sample for combo 'windows'"):
        sample.write_sample(Path("unused"), ANCHOR, "windows")


def test_the_jpeg_writer_refuses_a_size_it_cannot_encode():
    with pytest.raises(ValueError):
        sample.jpeg_bytes(10, 8, [(0, 0, 0)])
