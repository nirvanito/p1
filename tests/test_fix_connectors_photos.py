"""Google Photos Takeouts made in another language.

A Takeout repeats a photo in every album it is in and keeps one record, the
copy in its year folder. The year folder was recognised only by its English
name ('Photos from 2022'), so in a German Takeout ('Fotos von 2022') every
copy looked like an album copy: the kept copy — the photo's id — was whichever
path was shortest, often the album's, and taking the photo out of the album
later gave it a second id and a second record. Edited copies were recognised
only by the English '-edited', so 'IMG_1-bearbeitet.jpg' became a photo of
its own.
"""

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from lifecrm.connectors import photos
from lifecrm.demo.sources.photos import exif_tiff, jpeg_bytes
from lifecrm.domain import observations as obs

TAKEN = datetime(2022, 9, 27, 8, 12, 10, tzinfo=timezone.utc)


def _jpeg() -> bytes:
    tiff = exif_tiff(make="samsung", model="SM-S921U", taken=datetime(2022, 9, 27, 10, 12, 10),
                     offset="+02:00", subsec="482", width=96, height=72, orientation=1,
                     lat=46.3625, lon=14.0936, alt=476.2, gps_utc=None)
    return jpeg_bytes(96, 72, [(120, 170, 220), (40, 100, 140)], exif=tiff)


def _sidecar(title: str) -> bytes:
    stamp = str(int(TAKEN.timestamp()))
    return json.dumps({
        "title": title, "description": "",
        "creationTime": {"timestamp": stamp},
        "photoTakenTime": {"timestamp": stamp},
        "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
    }).encode("utf-8")


def _put(root: Path, folder: str, name: str, *, sidecar: bool = True) -> None:
    d = root / "Takeout" / "Google Fotos" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(_jpeg())
    if sidecar:
        (d / f"{name}.supplemental-metadata.json").write_bytes(_sidecar(name))


def _ids_and_albums(scan) -> dict:
    return {r["external_id"]: json.loads(r["value_json"]).get("albums")
            for r in scan["records"]}


def test_a_german_year_folder_is_the_kept_copy_and_not_an_album(cfg, tmp_path):
    root = tmp_path / "t1"
    _put(root, "Fotos von 2022", "IMG_1.jpg")
    _put(root, "Urlaub", "IMG_1.jpg")
    assert _ids_and_albums(photos.scan(root, cfg)) == {
        "takeout:Fotos von 2022/IMG_1.jpg": ["Urlaub"]}


def test_a_photo_taken_out_of_an_album_keeps_its_one_record(cfg, conn, tmp_path):
    first, later = tmp_path / "t1", tmp_path / "t2"
    _put(first, "Fotos von 2022", "IMG_1.jpg")
    _put(first, "Urlaub", "IMG_1.jpg")
    _put(later, "Fotos von 2022", "IMG_1.jpg")
    for root in (first, later):
        scan = photos.scan(root, cfg)
        obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
        conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE stream='photo' "
                        "AND status='active'").fetchone()["c"] == 1


def test_a_year_folder_in_an_unlisted_language_still_gives_a_stable_id(cfg, tmp_path):
    """A wording the list does not know reads as an album, but the copy in the
    folder ending in the photo's own year is the one kept — whatever albums
    the photo is in, and however short their names."""
    with_album, without = tmp_path / "t1", tmp_path / "t2"
    _put(with_album, "Fotky z roku 2022", "IMG_1.jpg")
    _put(with_album, "Léto", "IMG_1.jpg")
    _put(without, "Fotky z roku 2022", "IMG_1.jpg")
    a = set(_ids_and_albums(photos.scan(with_album, cfg)))
    b = set(_ids_and_albums(photos.scan(without, cfg)))
    assert a == b == {"takeout:Fotky z roku 2022/IMG_1.jpg"}


def test_an_album_named_with_a_year_keeps_its_label(cfg, tmp_path):
    root = tmp_path / "t1"
    _put(root, "Fotos von 2022", "IMG_1.jpg")
    _put(root, "Bled 2022", "IMG_1.jpg")
    assert _ids_and_albums(photos.scan(root, cfg)) == {
        "takeout:Fotos von 2022/IMG_1.jpg": ["Bled 2022"]}


def test_a_localized_edited_copy_is_not_a_second_photo(cfg, tmp_path):
    root = tmp_path / "t1"
    _put(root, "Fotos von 2022", "IMG_1.jpg")
    _put(root, "Fotos von 2022", "IMG_1-bearbeitet.jpg", sidecar=False)
    # a Mac writes accented names decomposed
    _put(root, "Fotos von 2022", unicodedata.normalize("NFD", "IMG_1-modifié.jpg"),
         sidecar=False)
    scan = photos.scan(root, cfg)
    assert set(_ids_and_albums(scan)) == {"takeout:Fotos von 2022/IMG_1.jpg"}
    assert "2 × edited copy (the original is imported)" in scan["skipped"]
