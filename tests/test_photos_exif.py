"""Photo and video metadata readers (connectors/_exif.py), format by format.

Every container here is built byte by byte from its specification — JPEG
segments, a TIFF/EXIF structure, ISO-BMFF boxes for HEIF and QuickTime, PNG
and RIFF chunks — so the tests pin the layouts real cameras write rather
than whatever a library happens to produce. The JPEG and EXIF writers are
the demo generator's (lifecrm/demo/sources/photos.py); a real decoder
(ffmpeg, Windows' GDI+) was used once to confirm those files are valid.

The contract under test: the right values come out, a damaged file yields a
partial result instead of an exception, and a reader never reads more than
the metadata — a 100k-photo library must not be loaded to be listed.
"""

import io
import struct
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from lifecrm.connectors import _exif
from lifecrm.demo.sources.photos import exif_tiff, jpeg_bytes

TAKEN = datetime(2022, 9, 27, 10, 12, 10)
UTC = TAKEN - timedelta(hours=2)


def _tiff(**kw) -> bytes:
    base = dict(make="samsung", model="SM-S921U", taken=TAKEN, offset="+02:00",
                subsec="123", width=4000, height=3000, orientation=6,
                lat=46.3625, lon=14.0936, alt=476.2,
                gps_utc=UTC.replace(tzinfo=timezone.utc))
    base.update(kw)
    return exif_tiff(**base)


def _read(data: bytes, **kw) -> dict:
    return _exif.read(io.BytesIO(data), len(data), **kw)


class _Counting(io.BytesIO):
    """Counts the bytes a reader actually pulled out of the file."""

    def __init__(self, data):
        super().__init__(data)
        self.taken = 0

    def read(self, n=-1):
        out = super().read(n)
        self.taken += len(out)
        return out


# ---------------------------------------------------------------- ISO-BMFF builders

def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def full(typ: bytes, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return box(typ, bytes([version]) + flags.to_bytes(3, "big") + payload)


def heic(tiff: bytes, *, iloc_version: int = 1, in_idat: bool = False,
         width: int = 4032, height: int = 3024, marker: bool = True) -> bytes:
    """An HEIC as an iPhone lays it out: ftyp, meta (hdlr, pitm, iinf with an
    'hvc1' image item and an 'Exif' item, iloc, iprp with ispe), mdat."""
    payload = (struct.pack(">I", 6) + b"Exif\x00\x00" + tiff) if marker \
        else struct.pack(">I", 0) + tiff
    image = b"\x00\x00\x00\x10" + b"\x26\x01" * 8          # stand-in HEVC data
    ftyp = box(b"ftyp", b"heic" + b"\x00\x00\x00\x00" + b"mif1heicmiaf")
    hdlr = full(b"hdlr", b"\x00" * 4 + b"pict" + b"\x00" * 12 + b"\x00")
    pitm = full(b"pitm", struct.pack(">H", 1))
    iinf = full(b"iinf", struct.pack(">H", 2)
                + full(b"infe", struct.pack(">HH", 1, 0) + b"hvc1\x00", version=2)
                + full(b"infe", struct.pack(">HH", 2, 0) + b"Exif\x00", version=2))
    iprp = box(b"iprp", box(b"ipco", full(b"ispe", struct.pack(">II", width, height)))
               + full(b"ipma", struct.pack(">I", 1) + struct.pack(">HB", 1, 1) + b"\x81"))
    idat = box(b"idat", payload) if in_idat else b""

    def iloc(image_at: int, exif_at: int) -> bytes:
        wide = ">I" if iloc_version == 2 else ">H"       # version 2: 32-bit ids and count

        def item(item_id, method, offset, length):
            head = struct.pack(wide, item_id)
            if iloc_version in (1, 2):
                head += struct.pack(">H", method)
            return head + struct.pack(">HHII", 0, 1, offset, length)
        body = bytes([0x44, 0x00]) + struct.pack(wide, 2) \
            + item(1, 0, image_at, len(image)) \
            + item(2, 1 if in_idat else 0, exif_at, len(payload))
        return full(b"iloc", body, version=iloc_version)

    def meta(image_at, exif_at):
        return full(b"meta", hdlr + pitm + iinf + iloc(image_at, exif_at) + iprp + idat)

    head = len(ftyp) + len(meta(0, 0)) + 8           # where mdat's payload starts
    exif_at = 0 if in_idat else head + len(image)
    mdat = box(b"mdat", image + (b"" if in_idat else payload))
    return ftyp + meta(head, exif_at) + mdat


def quicktime(*, created: datetime, duration: float = 14.53, scale: int = 600,
              xyz: str | None = "+46.3625+014.0936+476.000/", apple: dict | None = None,
              moov_last: bool = False, filler: int = 4096, iso_meta: bool = False,
              width: int = 1920, height: int = 1080) -> bytes:
    secs = int((created - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds())
    mvhd = full(b"mvhd", struct.pack(">IIII", secs, secs, scale, int(duration * scale))
                + b"\x00" * 80)
    tkhd = full(b"tkhd", struct.pack(">IIIII", secs, secs, 1, 0, int(duration * scale))
                + b"\x00" * 16 + b"\x00" * 36
                + struct.pack(">II", width << 16, height << 16), flags=3)
    audio = full(b"tkhd", struct.pack(">IIIII", secs, secs, 2, 0, 0) + b"\x00" * 52
                 + struct.pack(">II", 0, 0), flags=3)
    parts = [mvhd, box(b"trak", tkhd + box(b"mdia", b"")), box(b"trak", audio)]
    if xyz:
        s = xyz.encode()
        parts.append(box(b"udta", box(b"\xa9xyz", struct.pack(">HH", len(s), 0x15C7) + s)))
    if apple:
        keys = list(apple)
        keys_box = full(b"keys", struct.pack(">I", len(keys)) + b"".join(
            struct.pack(">I", 8 + len(k)) + b"mdta" + k.encode() for k in keys))
        ilst = box(b"ilst", b"".join(
            box(struct.pack(">I", i + 1), box(b"data", struct.pack(">II", 1, 0)
                                              + apple[k].encode()))
            for i, k in enumerate(keys)))
        hdlr = full(b"hdlr", b"\x00" * 4 + b"mdta" + b"\x00" * 12 + b"\x00")
        inner = hdlr + keys_box + ilst
        parts.append(full(b"meta", inner) if iso_meta else box(b"meta", inner))
    moov = box(b"moov", b"".join(parts))
    ftyp = box(b"ftyp", b"qt  " + b"\x00\x00\x02\x00" + b"qt  ")
    mdat = box(b"mdat", b"\x00" * filler)
    return ftyp + (mdat + moov if moov_last else moov + mdat)


def png(tiff: bytes | None, *, after_idat: bool = False, prefix: bool = False) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1179, 2556, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00" * 64))
    exif = chunk(b"eXIf", (b"Exif\x00\x00" if prefix else b"") + tiff) if tiff else b""
    body = ihdr + (idat + exif if after_idat else exif + idat)
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")


def webp(tiff: bytes) -> bytes:
    def chunk(t, d):
        return t + struct.pack("<I", len(d)) + d + (b"\x00" if len(d) % 2 else b"")
    body = chunk(b"VP8X", bytes([0x08, 0, 0, 0]) + (1599).to_bytes(3, "little")
                 + (1199).to_bytes(3, "little")) + chunk(b"VP8 ", b"\x00" * 10) \
        + chunk(b"EXIF", tiff)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


# ---------------------------------------------------------------- JPEG / TIFF

def test_a_camera_jpeg_yields_time_zone_place_camera_and_size():
    meta = _read(jpeg_bytes(16, 8, [(10, 20, 30)], exif=_tiff()))
    assert meta["format"] == "jpeg"
    assert meta["taken_local"] == "2022-09-27T10:12:10"
    assert meta["offset_sec"] == 7200
    assert (meta["lat"], meta["lon"], meta["alt"]) == (46.3625, 14.0936, 476.2)
    assert meta["gps_utc"] == "2022-09-27T08:12:10Z"
    assert (meta["make"], meta["model"]) == ("samsung", "SM-S921U")
    assert (meta["width"], meta["height"], meta["orientation"]) == (4000, 3000, 6)


@pytest.mark.parametrize("little_endian", [True, False])
def test_both_byte_orders_and_the_southern_and_western_hemispheres(little_endian):
    meta = _read(jpeg_bytes(8, 8, [(0, 0, 0)], exif=_tiff(
        lat=-33.8568, lon=-70.6483, alt=-12.5, offset="-04:00",
        little_endian=little_endian)))
    assert (meta["lat"], meta["lon"], meta["alt"]) == (-33.8568, -70.6483, -12.5)
    assert meta["offset_sec"] == -4 * 3600


def test_a_jpeg_without_exif_still_gives_its_pixel_size():
    meta = _read(jpeg_bytes(72, 152, [(255, 255, 255)]))
    assert meta == {"format": "jpeg", "width": 72, "height": 152}


def test_the_xmp_segment_and_fill_bytes_before_the_exif_are_stepped_over():
    real = jpeg_bytes(8, 8, [(1, 2, 3)], exif=_tiff())
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta/>"
    seg = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp
    data = real[:2] + seg + b"\xff\xff" + real[2:]
    assert _read(data)["taken_local"] == "2022-09-27T10:12:10"


def test_a_camera_that_writes_no_offset_or_boilerplate_description():
    tiff = _tiff(offset=None, description="OLYMPUS DIGITAL CAMERA")
    meta = _read(jpeg_bytes(8, 8, [(0, 0, 0)], exif=tiff))
    assert "offset_sec" not in meta and "description" not in meta
    meta = _read(jpeg_bytes(8, 8, [(0, 0, 0)], exif=_tiff(description="Rowboat with Jake")))
    assert meta["description"] == "Rowboat with Jake"


def test_a_zero_zero_position_is_no_fix_and_a_blank_date_is_no_date():
    tiff = _tiff(lat=0.0, lon=0.0, alt=None, taken=None, gps_utc=None)
    meta = _read(jpeg_bytes(8, 8, [(0, 0, 0)], exif=tiff))
    assert "lat" not in meta and "taken_local" not in meta
    raw = bytearray(jpeg_bytes(8, 8, [(0, 0, 0)], exif=_tiff()))
    at = raw.find(b"2022:09:27 10:12:10")
    while at >= 0:
        raw[at:at + 19] = b"0000:00:00 00:00:00"
        at = raw.find(b"2022:09:27 10:12:10")
    assert "taken_local" not in _read(bytes(raw))


def test_a_raw_or_dng_file_is_read_as_the_tiff_it_is():
    meta = _read(_tiff())
    assert meta["format"] == "tiff" and meta["model"] == "SM-S921U"


# ---------------------------------------------------------------- HEIF

@pytest.mark.parametrize("iloc_version", [0, 1, 2])
def test_an_iphone_heic_is_read_through_its_exif_item(iloc_version):
    tiff = _tiff(make="Apple", model="iPhone 15 Pro", little_endian=False,
                 width=None, height=None)
    meta = _read(heic(tiff, iloc_version=iloc_version))
    assert meta["format"] == "heif"
    assert (meta["make"], meta["model"]) == ("Apple", "iPhone 15 Pro")
    assert meta["taken_local"] == "2022-09-27T10:12:10" and meta["offset_sec"] == 7200
    assert (meta["lat"], meta["lon"]) == (46.3625, 14.0936)
    assert (meta["width"], meta["height"]) == (4032, 3024)       # from ispe


def test_an_exif_item_stored_inside_the_meta_box_and_one_without_the_marker():
    tiff = _tiff(width=None, height=None)
    assert _read(heic(tiff, in_idat=True))["model"] == "SM-S921U"
    assert _read(heic(tiff, marker=False))["model"] == "SM-S921U"


def test_avif_is_heif_with_another_brand():
    data = heic(_tiff())
    data = data[:8] + b"avif" + data[12:]
    meta = _read(data)
    assert meta["format"] == "avif" and meta["model"] == "SM-S921U"


# ---------------------------------------------------------------- PNG / WebP

@pytest.mark.parametrize("after_idat,prefix", [(False, False), (True, True)])
def test_a_png_screenshot_carries_exif_in_its_exif_chunk(after_idat, prefix):
    meta = _read(png(_tiff(lat=None, lon=None, gps_utc=None), after_idat=after_idat,
                     prefix=prefix))
    assert meta["format"] == "png"
    assert (meta["width"], meta["height"]) == (1179, 2556)     # IHDR wins
    assert meta["taken_local"] == "2022-09-27T10:12:10"


def test_a_webp_carries_exif_in_a_riff_chunk():
    meta = _read(webp(_tiff(width=None, height=None)))
    assert meta["format"] == "webp" and (meta["width"], meta["height"]) == (1600, 1200)
    assert meta["make"] == "samsung"


# ---------------------------------------------------------------- QuickTime / MP4

def test_an_android_video_gives_utc_time_duration_size_and_xyz_location():
    created = datetime(2022, 9, 28, 13, 7, 12, tzinfo=timezone.utc)
    meta = _read(quicktime(created=created))
    assert meta["format"] == "quicktime"
    assert meta["taken_utc"] == "2022-09-28T13:07:12Z"
    assert meta["duration_sec"] == 14.53
    assert (meta["width"], meta["height"]) == (1920, 1080)
    assert (meta["lat"], meta["lon"], meta["alt"]) == (46.3625, 14.0936, 476.0)
    assert "taken_local" not in meta


@pytest.mark.parametrize("iso_meta", [False, True])
def test_an_iphone_video_adds_its_local_time_zone_and_camera(iso_meta):
    created = datetime(2022, 9, 28, 13, 7, 12, tzinfo=timezone.utc)
    meta = _read(quicktime(created=created, xyz=None, iso_meta=iso_meta, apple={
        "com.apple.quicktime.creationdate": "2022-09-28T15:07:12+0200",
        "com.apple.quicktime.location.ISO6709": "+46.3627+014.0933+475.512/",
        "com.apple.quicktime.make": "Apple",
        "com.apple.quicktime.model": "iPhone 15 Pro"}))
    assert meta["taken_local"] == "2022-09-28T15:07:12" and meta["offset_sec"] == 7200
    assert (meta["lat"], meta["lon"]) == (46.3627, 14.0933)
    assert (meta["make"], meta["model"]) == ("Apple", "iPhone 15 Pro")


def test_a_zero_or_unix_epoch_creation_time_is_no_time():
    meta = _read(quicktime(created=datetime(1904, 1, 1, tzinfo=timezone.utc)))
    assert "taken_utc" not in meta and meta["duration_sec"] == 14.53
    meta = _read(quicktime(created=datetime(1970, 1, 1, tzinfo=timezone.utc)))
    assert "taken_utc" not in meta


# ---------------------------------------------------------------- reading only the metadata

def test_a_video_whose_moov_follows_a_huge_mdat_is_read_in_a_few_kilobytes():
    data = quicktime(created=datetime(2026, 1, 2, tzinfo=timezone.utc),
                     moov_last=True, filler=20 << 20)
    f = _Counting(data)
    meta = _exif.read(f, len(data))
    assert meta["taken_utc"] == "2026-01-02T00:00:00Z"
    assert f.taken < 4096


def test_a_large_jpeg_is_read_only_up_to_its_scan_data():
    data = jpeg_bytes(8, 8, [(0, 0, 0)], exif=_tiff())
    data = data[:-2] + b"\x00" * (8 << 20) + b"\xff\xd9"
    f = _Counting(data)
    assert _exif.read(f, len(data))["model"] == "SM-S921U"
    assert f.taken < 64 * 1024


def test_a_seek_limit_stops_a_reader_short_of_a_far_away_box():
    data = quicktime(created=datetime(2026, 1, 2, tzinfo=timezone.utc),
                     moov_last=True, filler=1 << 20)
    assert "taken_utc" not in _read(data, limit=64 * 1024)
    assert "taken_utc" in _read(data)


# ---------------------------------------------------------------- damage

@pytest.mark.parametrize("make", [
    lambda: jpeg_bytes(16, 8, [(9, 9, 9)], exif=_tiff()),
    lambda: heic(_tiff()),
    lambda: quicktime(created=datetime(2022, 9, 28, tzinfo=timezone.utc), apple={
        "com.apple.quicktime.creationdate": "2022-09-28T15:07:12+0200"}),
    lambda: png(_tiff()),
    lambda: webp(_tiff()),
])
def test_a_file_cut_short_anywhere_never_raises(make):
    data = make()
    for cut in range(0, len(data), 7):
        out = _read(data[:cut])
        assert isinstance(out, dict)


def test_an_ifd_pointing_outside_the_file_is_ignored_not_trusted():
    tiff = bytearray(_tiff())
    # IFD0's entry count -> 4000 entries, far past the end of the data.
    struct.pack_into("<H", tiff, 8, 4000)
    out = _exif.parse_tiff(bytes(tiff))
    assert isinstance(out, dict)
    assert _exif.parse_tiff(b"II*\x00\xff\xff\xff\x7f") == {}


def test_an_unknown_file_is_named_as_unknown():
    assert _read(b"not an image at all") == {"format": None}
    assert _exif.sniff(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom") == "quicktime"
    assert _exif.sniff(b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic") == "heif"
