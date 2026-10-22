"""Photo and video metadata from container headers — never the pixels.

A helper for the photos connector (the leading underscore keeps it out of the
registry). Pillow is not a dependency, and would be the wrong tool anyway: it
decodes images, and the connector needs a dozen tags from the first few
kilobytes of each file across a library of 100k. These readers walk each
container by seeking, read only the bytes that hold metadata, and give up
quietly — a partial result flagged `damaged` — on anything malformed: one odd
file must never fail an import of thousands.

  JPEG        APP1 'Exif' segment -> TIFF; SOFn for the pixel size
  HEIF/AVIF   ISO-BMFF: meta -> iinf (which item is 'Exif') -> iloc (where
              its bytes are) -> TIFF; ispe for the pixel size. The iPhone's
              default format.
  PNG         eXIf chunk -> TIFF; IHDR for the pixel size
  WebP        RIFF 'EXIF' chunk -> TIFF; VP8X for the pixel size
  TIFF        the file is the TIFF (DNG and most raw formats)
  QuickTime   moov/mvhd creation time (UTC) and duration, tkhd for the frame
  / MP4       size, udta/©xyz (Android) and Apple's mdta keys for location
              and the local capture time with its offset

What comes back is plain data, every key optional:

  format        'jpeg' | 'heif' | 'avif' | 'png' | 'webp' | 'tiff' | 'quicktime'
  taken_local   'YYYY-MM-DDTHH:MM:SS' wall-clock capture time, no zone
  offset_sec    UTC offset of taken_local, when the camera wrote one
  taken_utc     'YYYY-MM-DDTHH:MM:SSZ' when the container states UTC
  gps_utc       'YYYY-MM-DDTHH:MM:SSZ' the GPS receiver's clock
  lat, lon, alt, make, model, width, height, orientation, description,
  duration_sec, damaged

Sub-second time (SubSecTimeOriginal) is deliberately ignored: the observed
tier stores whole seconds, and two reads of one file must compare equal.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta, timezone

HEADER_CAP = 256 * 1024          # bytes read from one file, at most
_MAX_ENTRIES = 1024              # IFD entries per directory
_MAX_BOX = 4 << 20               # largest metadata box loaded whole (HEIF meta)
_MAX_OFFSET_SEC = 14 * 3600      # the widest real UTC offset

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
              11: 4, 12: 8}

# What some cameras put in ImageDescription when the owner wrote nothing.
_BOILERPLATE = {"", "OLYMPUS DIGITAL CAMERA", "SONY DSC", "DIGITAL CAMERA",
                "EXIF_JPEG_PICTURE", "DEFAULT", "SAMSUNG", "IMAGE", "PICTURE",
                "MINOLTA DIGITAL CAMERA", "KODAK DIGITAL STILL CAMERA"}

_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1",
                b"msf1", b"avif", b"avis"}
_QT_FIRST_BOXES = {b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}
_MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


class _Source:
    """Positioned reads from a seekable binary file, within two bounds.

    `budget` caps the bytes actually read; `limit` caps how far a read may
    reach. A zip member is decompressed to reach any position, so it gets a
    limit; a real file seeks for free and gets none — finding a QuickTime
    'moov' at the end of a 2 GB video costs a dozen reads, not 2 GB.
    """

    def __init__(self, f, size: int, limit: int | None = None,
                 budget: int = HEADER_CAP):
        self.f, self.size = f, size
        self.end = size if limit is None else min(size, limit)
        self.budget = budget

    def read_at(self, pos: int, n: int) -> bytes:
        if pos < 0 or n <= 0 or pos >= self.end or self.budget <= 0:
            return b""
        n = min(n, self.end - pos, self.budget)
        self.f.seek(pos)
        data = self.f.read(n)
        self.budget -= len(data)
        return data


# ---------------------------------------------------------------- dispatch

def sniff(head: bytes) -> str | None:
    """The container format from the first bytes, whatever the extension
    says — a phone that saved HEIF under '.jpg' is still HEIF."""
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        major = head[8:12]
        end = min(len(head), int.from_bytes(head[:4], "big"))
        brands = {head[i:i + 4] for i in range(16, end - 3, 4)}
        if major in (b"avif", b"avis"):
            return "avif"
        if major in _HEIF_BRANDS or (major != b"qt  " and brands & _HEIF_BRANDS
                                     and not brands & {b"isom", b"mp41", b"mp42"}):
            return "heif"
        return "quicktime"
    if head[4:8] in _QT_FIRST_BOXES:
        return "quicktime"
    return None


def read(f, size: int, *, limit: int | None = None) -> dict:
    """Metadata of one file (see the module docstring). Never raises on a
    malformed file; I/O errors from `f` itself propagate."""
    src = _Source(f, size, limit)
    fmt = sniff(src.read_at(0, 64))
    out: dict = {"format": fmt}
    parser = {"jpeg": _jpeg, "png": _png, "tiff": _tiff_file, "webp": _webp,
              "heif": _heif, "avif": _heif, "quicktime": _quicktime}.get(fmt)
    if parser is None:
        return out
    try:
        parser(src, out)
    except (struct.error, ValueError, IndexError, OverflowError,
            ZeroDivisionError, KeyError):
        out["damaged"] = True
    return out


# ---------------------------------------------------------------- TIFF / EXIF

def parse_tiff(data: bytes) -> dict:
    """The tags the connector uses, from a TIFF structure (an EXIF payload
    without its 'Exif\\0\\0' prefix). Partial on damage, never raises."""
    out: dict = {}
    try:
        _parse_tiff(bytes(data), out)
    except (struct.error, ValueError, IndexError, OverflowError,
            ZeroDivisionError):
        out["damaged"] = True
    return out


def _parse_tiff(data: bytes, out: dict) -> None:
    if len(data) < 8:
        return
    e = {b"II": "<", b"MM": ">"}.get(data[:2])
    if e is None:
        return
    magic, first = struct.unpack_from(e + "HI", data, 2)
    if magic != 42:
        return
    ifd0 = _ifd(data, first, e)
    exif = _ifd(data, _int(ifd0.get(0x8769)), e) if 0x8769 in ifd0 else {}
    gps = _ifd(data, _int(ifd0.get(0x8825)), e) if 0x8825 in ifd0 else {}

    for key, tag in (("make", 0x010F), ("model", 0x0110)):
        text = _text(ifd0.get(tag))
        if text:
            out[key] = text
    desc = _text(ifd0.get(0x010E))
    if desc and desc.upper() not in _BOILERPLATE \
            and desc not in (out.get("make"), out.get("model")):
        out["description"] = desc
    orientation = _int(ifd0.get(0x0112))
    if orientation and 1 <= orientation <= 8:
        out["orientation"] = orientation

    # The original capture time, then the digitized one (a scan's), each with
    # its own offset tag; DateTime (0x0132) is the last edit and is not used.
    for when_tag, offset_tag in ((0x9003, 0x9011), (0x9004, 0x9012)):
        local = _exif_datetime(_text(exif.get(when_tag)))
        if local:
            out["taken_local"] = local
            offset = _offset(_text(exif.get(offset_tag))) \
                if offset_tag in exif else _offset(_text(exif.get(0x9010)))
            if offset is not None:
                out["offset_sec"] = offset
            break

    width = _int(exif.get(0xA002)) or _int(ifd0.get(0x0100))
    height = _int(exif.get(0xA003)) or _int(ifd0.get(0x0101))
    if width and height:
        out["width"], out["height"] = width, height

    if gps:
        _gps(gps, out)


def _gps(gps: dict, out: dict) -> None:
    lat, lon = _dms(gps.get(0x0002)), _dms(gps.get(0x0004))
    if lat is not None and lon is not None:
        if (_text(gps.get(0x0001)) or "N").upper().startswith("S"):
            lat = -lat
        if (_text(gps.get(0x0003)) or "E").upper().startswith("W"):
            lon = -lon
        # 0,0 is what a camera writes when it never had a fix.
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0):
            out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
            alt = _rational(_first(gps.get(0x0006)))
            if alt is not None:
                ref = gps.get(0x0005)
                below = (ref[:1] == b"\x01") if isinstance(ref, bytes) \
                    else _int(ref) == 1
                out["alt"] = round(-alt if below else alt, 1)
    stamp, clock = _text(gps.get(0x001D)), gps.get(0x0007)
    if stamp and isinstance(clock, tuple) and len(clock) == 3:
        hms = [_rational(x) for x in clock]
        m = re.match(r"^(\d{4}):(\d{2}):(\d{2})$", stamp)
        if m and all(x is not None for x in hms):
            try:
                dt = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
                dt += timedelta(seconds=int(hms[0] * 3600 + hms[1] * 60 + hms[2]))
                out["gps_utc"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, OverflowError):
                pass


def _ifd(data: bytes, offset, e: str) -> dict:
    """One image file directory: {tag: decoded value}. Entries whose values
    point outside the data are skipped, not trusted."""
    if not isinstance(offset, int) or offset < 8 or offset + 2 > len(data):
        return {}
    (count,) = struct.unpack_from(e + "H", data, offset)
    tags = {}
    for i in range(min(count, _MAX_ENTRIES)):
        p = offset + 2 + 12 * i
        if p + 12 > len(data):
            break
        tag, typ, n = struct.unpack_from(e + "HHI", data, p)
        size = _TYPE_SIZE.get(typ)
        if size is None or n == 0 or n > 1 << 16:
            continue
        total = size * n
        if total <= 4:
            raw = data[p + 8:p + 8 + total]
        else:
            (at,) = struct.unpack_from(e + "I", data, p + 8)
            if at + total > len(data):
                continue
            raw = data[at:at + total]
        tags[tag] = _decode(typ, n, raw, e)
    return tags


def _decode(typ: int, n: int, raw: bytes, e: str):
    if typ == 2:
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    if typ in (1, 7):
        return raw
    if typ in (5, 10):
        nums = struct.unpack(e + ("I" if typ == 5 else "i") * (2 * n), raw)
        return tuple(zip(nums[0::2], nums[1::2]))
    code = {3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d"}[typ]
    return struct.unpack(e + code * n, raw)


def _first(v):
    return v[0] if isinstance(v, tuple) and v else v


def _int(v) -> int | None:
    v = _first(v)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _text(v) -> str | None:
    if isinstance(v, bytes):
        v = v.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    return v if isinstance(v, str) and v else None


def _rational(v) -> float | None:
    if isinstance(v, tuple) and len(v) == 2 and v[1]:
        return v[0] / v[1]
    return None


def _dms(v) -> float | None:
    if not isinstance(v, tuple) or not v:
        return None
    parts = [_rational(x) for x in v[:3]]
    if any(p is None for p in parts):
        return None
    parts += [0.0] * (3 - len(parts))
    return parts[0] + parts[1] / 60 + parts[2] / 3600


def _exif_datetime(text: str | None) -> str | None:
    """'YYYY:MM:DD HH:MM:SS' -> 'YYYY-MM-DDTHH:MM:SS'; the all-zero
    placeholder and impossible dates are None."""
    if not text:
        return None
    m = re.match(r"^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", text)
    if not m:
        return None
    try:
        dt = datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None
    if dt.year < 1900:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _offset(text: str | None) -> int | None:
    """'+02:00' / '-0500' -> seconds east of UTC."""
    if not text:
        return None
    m = re.match(r"^\s*([+-])(\d{2}):?(\d{2})\s*$", text)
    if not m:
        return None
    sec = int(m[2]) * 3600 + int(m[3]) * 60
    sec = -sec if m[1] == "-" else sec
    return sec if abs(sec) <= _MAX_OFFSET_SEC else None


def _merge(out: dict, found: dict) -> None:
    """Fill what is not yet known; never overwrite."""
    for k, v in found.items():
        out.setdefault(k, v)


# ---------------------------------------------------------------- JPEG

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD,
        0xCE, 0xCF}


def _jpeg(src: _Source, out: dict) -> None:
    pos, frame, exif_read = 2, None, False
    for _ in range(256):
        head = src.read_at(pos, 4)
        if len(head) < 4 or head[0] != 0xFF:
            break
        marker = head[1]
        if marker == 0xFF:                      # fill byte before a marker
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:   # no length field
            pos += 2
            continue
        if marker in (0xD9, 0xDA):              # EOI / start of scan: no more metadata
            break
        (length,) = struct.unpack(">H", head[2:4])
        if length < 2:
            break
        if marker == 0xE1 and not exif_read:          # the first APP1 that is EXIF, not XMP
            payload = src.read_at(pos + 4, length - 2)
            if payload[:4] == b"Exif":
                exif_read = True
                _merge(out, parse_tiff(payload[6:]))
        elif marker in _SOF and frame is None:
            seg = src.read_at(pos + 4, 5)
            if len(seg) == 5:
                height, width = struct.unpack(">HH", seg[1:5])
                frame = (width, height)
        pos += 2 + length
    if frame and frame[0] and frame[1] and "width" not in out:
        out["width"], out["height"] = frame


# ---------------------------------------------------------------- TIFF file

def _tiff_file(src: _Source, out: dict) -> None:
    _merge(out, parse_tiff(src.read_at(0, HEADER_CAP)))


# ---------------------------------------------------------------- PNG / WebP

def _png(src: _Source, out: dict) -> None:
    pos = 8
    for _ in range(100_000):
        head = src.read_at(pos, 8)
        if len(head) < 8:
            break
        length, ctype = struct.unpack(">I4s", head)
        if ctype == b"IHDR":
            width, height = struct.unpack(">II", src.read_at(pos + 8, 8))
            out.setdefault("width", width)
            out.setdefault("height", height)
        elif ctype == b"eXIf":
            data = src.read_at(pos + 8, min(length, 1 << 16))
            if data[:6] == b"Exif\x00\x00":
                data = data[6:]
            found = parse_tiff(data)
            # the chunk's EXIF size beats IHDR only if IHDR was absent
            found.pop("width", None)
            found.pop("height", None)
            _merge(out, found)
        elif ctype == b"IEND":
            break
        pos += 12 + length


def _webp(src: _Source, out: dict) -> None:
    pos = 12
    for _ in range(1024):
        head = src.read_at(pos, 8)
        if len(head) < 8:
            break
        ctype, length = struct.unpack("<4sI", head)
        if ctype == b"VP8X":
            d = src.read_at(pos + 8, 10)
            if len(d) == 10:
                out.setdefault("width", 1 + int.from_bytes(d[4:7], "little"))
                out.setdefault("height", 1 + int.from_bytes(d[7:10], "little"))
        elif ctype == b"EXIF":
            data = src.read_at(pos + 8, min(length, 1 << 16))
            if data[:6] == b"Exif\x00\x00":
                data = data[6:]
            _merge(out, parse_tiff(data))
        pos += 8 + length + (length & 1)


# ---------------------------------------------------------------- ISO-BMFF

def _walk(read_at, start: int, end: int, limit: int = 4096):
    """(type, payload_start, payload_end) for each box in [start, end)."""
    pos = start
    for _ in range(limit):
        if pos + 8 > end:
            return
        head = read_at(pos, 16)
        if len(head) < 8:
            return
        size, typ = struct.unpack(">I4s", head[:8])
        header = 8
        if size == 1:
            if len(head) < 16:
                return
            (size,) = struct.unpack(">Q", head[8:16])
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield typ, pos + header, min(pos + size, end)
        pos += size


def _bytes_reader(data: bytes):
    return lambda pos, n: data[pos:pos + n]


def _uint(data: bytes, pos: int, size: int) -> int:
    return int.from_bytes(data[pos:pos + size], "big") if size else 0


def _heif(src: _Source, out: dict) -> None:
    meta = next(((s, e) for typ, s, e in _walk(src.read_at, 0, src.size, 64)
                 if typ == b"meta"), None)
    if meta is None or meta[1] - meta[0] > _MAX_BOX:
        return
    data = src.read_at(meta[0], meta[1] - meta[0])
    if len(data) < meta[1] - meta[0]:
        return
    rd = _bytes_reader(data)
    boxes = {typ: (s, e) for typ, s, e in _walk(rd, 4, len(data))}   # FullBox: skip version/flags

    types = _iinf(data, *boxes[b"iinf"]) if b"iinf" in boxes else {}
    places = _iloc(data, *boxes[b"iloc"]) if b"iloc" in boxes else {}
    exif_id = next((i for i, t in sorted(types.items()) if t == b"Exif"), None)
    if exif_id is not None and exif_id in places:
        method, extents = places[exif_id]
        if method == 1 and b"idat" in boxes:
            s, _ = boxes[b"idat"]
            payload = b"".join(data[s + off:s + off + ln] for off, ln in extents)
        elif method == 0:
            payload = b"".join(src.read_at(off, min(ln, 1 << 16)) for off, ln in extents)
        else:
            payload = b""
        tiff = _exif_payload(payload)
        if tiff:
            _merge(out, parse_tiff(tiff))

    if "width" not in out and b"iprp" in boxes:
        size = _primary_size(data, boxes)
        if size:
            out["width"], out["height"] = size


def _exif_payload(payload: bytes) -> bytes:
    """An 'Exif' item starts with a 4-byte offset to the TIFF header (usually
    past an 'Exif\\0\\0' marker). Some writers omit the offset; search a few
    bytes for the TIFF magic instead."""
    if len(payload) >= 4:
        (skip,) = struct.unpack(">I", payload[:4])
        tiff = payload[4 + skip:]
        if tiff[:4] in (b"II*\x00", b"MM\x00*"):
            return tiff
    for magic in (b"II*\x00", b"MM\x00*"):
        at = payload.find(magic, 0, 64)
        if at >= 0:
            return payload[at:]
    return b""


def _iinf(data: bytes, s: int, e: int) -> dict[int, bytes]:
    version = data[s]
    pos = s + 4 + (2 if version == 0 else 4)
    items = {}
    for typ, cs, ce in _walk(_bytes_reader(data), pos, e):
        if typ != b"infe" or ce - cs < 12:
            continue
        v = data[cs]
        if v < 2:
            continue
        if v == 2:
            (item,) = struct.unpack_from(">H", data, cs + 4)
            at = cs + 8
        else:
            (item,) = struct.unpack_from(">I", data, cs + 4)
            at = cs + 10
        items[item] = data[at:at + 4]
    return items


def _iloc(data: bytes, s: int, e: int) -> dict[int, tuple[int, list]]:
    version = data[s]
    pos = s + 4
    offset_size, length_size = data[pos] >> 4, data[pos] & 15
    base_size = data[pos + 1] >> 4
    index_size = (data[pos + 1] & 15) if version in (1, 2) else 0
    pos += 2
    if version < 2:
        (count,) = struct.unpack_from(">H", data, pos)
        pos += 2
    else:
        (count,) = struct.unpack_from(">I", data, pos)
        pos += 4
    places = {}
    for _ in range(min(count, 1 << 16)):
        if pos >= e:
            break
        if version < 2:
            (item,) = struct.unpack_from(">H", data, pos)
            pos += 2
        else:
            (item,) = struct.unpack_from(">I", data, pos)
            pos += 4
        method = 0
        if version in (1, 2):
            method = struct.unpack_from(">H", data, pos)[0] & 0xF
            pos += 2
        pos += 2                                         # data_reference_index
        base = _uint(data, pos, base_size)
        pos += base_size
        (n,) = struct.unpack_from(">H", data, pos)
        pos += 2
        extents = []
        for _ in range(n):
            pos += index_size
            off = _uint(data, pos, offset_size)
            pos += offset_size
            length = _uint(data, pos, length_size)
            pos += length_size
            extents.append((base + off, length))
        places[item] = (method, extents)
    return places


def _primary_size(data: bytes, boxes: dict) -> tuple[int, int] | None:
    """Width x height of the primary item, from its 'ispe' property."""
    primary = None
    if b"pitm" in boxes:
        s, _ = boxes[b"pitm"]
        primary = (struct.unpack_from(">H", data, s + 4)[0] if data[s] == 0
                   else struct.unpack_from(">I", data, s + 4)[0])
    s, e = boxes[b"iprp"]
    inner = list(_walk(_bytes_reader(data), s, e))
    ipco = next(((cs, ce) for t, cs, ce in inner if t == b"ipco"), None)
    ipma = next(((cs, ce) for t, cs, ce in inner if t == b"ipma"), None)
    if ipco is None:
        return None
    props = list(_walk(_bytes_reader(data), *ipco))

    def ispe(index: int):
        if 1 <= index <= len(props) and props[index - 1][0] == b"ispe":
            ps = props[index - 1][1]
            return struct.unpack_from(">II", data, ps + 4)
        return None

    if primary is not None and ipma is not None:
        ms, _ = ipma
        version, flags = data[ms], int.from_bytes(data[ms + 1:ms + 4], "big")
        (entries,) = struct.unpack_from(">I", data, ms + 4)
        pos = ms + 8
        for _ in range(min(entries, 1 << 16)):
            if version < 1:
                (item,) = struct.unpack_from(">H", data, pos)
                pos += 2
            else:
                (item,) = struct.unpack_from(">I", data, pos)
                pos += 4
            n = data[pos]
            pos += 1
            for _ in range(n):
                if flags & 1:
                    index = struct.unpack_from(">H", data, pos)[0] & 0x7FFF
                    pos += 2
                else:
                    index = data[pos] & 0x7F
                    pos += 1
                if item == primary and ispe(index):
                    return ispe(index)
    sizes = [ispe(i + 1) for i in range(len(props)) if props[i][0] == b"ispe"]
    sizes = [x for x in sizes if x]
    return max(sizes, key=lambda wh: wh[0] * wh[1]) if sizes else None


# ---------------------------------------------------------------- QuickTime / MP4

_ISO6709 = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?")
_APPLE_DATE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})$")


def _quicktime(src: _Source, out: dict) -> None:
    moov = next(((s, e) for typ, s, e in _walk(src.read_at, 0, src.size, 256)
                 if typ == b"moov"), None)
    if moov is None:
        return
    frames = []
    for typ, s, e in _walk(src.read_at, *moov, limit=512):
        if typ == b"mvhd":
            _mvhd(src.read_at(s, 32), out)
        elif typ == b"trak":
            for t2, s2, e2 in _walk(src.read_at, s, e, 32):
                if t2 == b"tkhd":
                    d = src.read_at(s2, min(e2 - s2, 128))
                    if len(d) >= 84:
                        w, h = struct.unpack_from(">II", d, len(d) - 8)
                        if w >> 16 and h >> 16:
                            frames.append((w >> 16, h >> 16))
                    break
        elif typ == b"udta" and e - s <= 1 << 16:
            _udta(src.read_at(s, e - s), out)
        elif typ == b"meta" and e - s <= 1 << 18:
            _apple_keys(src.read_at(s, e - s), out)
    if frames and "width" not in out:
        out["width"], out["height"] = max(frames, key=lambda wh: wh[0] * wh[1])


def _mvhd(d: bytes, out: dict) -> None:
    if len(d) < 24:
        return
    if d[0] == 1:
        if len(d) < 32:
            return
        created, _, scale, duration = struct.unpack_from(">QQIQ", d, 4)
    else:
        created, _, scale, duration = struct.unpack_from(">IIII", d, 4)
    if created:
        dt = _MAC_EPOCH + timedelta(seconds=created)
        if 1995 <= dt.year <= 2100:         # 0 or a Unix-epoch value is no time
            out["taken_utc"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if scale and duration and duration != 0xFFFFFFFF:
        out["duration_sec"] = round(duration / scale, 2)


def _location(text: str, out: dict) -> None:
    m = _ISO6709.match(text.strip())
    if not m:
        return
    lat, lon = float(m[1]), float(m[2])
    if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0):
        out.setdefault("lat", round(lat, 6))
        out.setdefault("lon", round(lon, 6))
        if m[3]:
            out.setdefault("alt", round(float(m[3]), 1))


def _udta(data: bytes, out: dict) -> None:
    for typ, s, e in _walk(_bytes_reader(data), 0, len(data), 256):
        if typ == b"\xa9xyz" and e - s > 4:
            (n,) = struct.unpack_from(">H", data, s)
            _location(data[s + 4:s + 4 + n].decode("utf-8", "replace"), out)


def _apple_keys(data: bytes, out: dict) -> None:
    """Apple's 'mdta' metadata: a 'keys' list and an 'ilst' of values whose
    box types are 1-based key indexes. QuickTime's meta is a plain box; the
    ISO one is a FullBox — tell them apart by where 'hdlr' sits."""
    start = 4 if data[4:8] != b"hdlr" and data[8:12] == b"hdlr" else 0
    rd = _bytes_reader(data)
    keys: list[str] = []
    values: dict[int, str] = {}
    for typ, s, e in _walk(rd, start, len(data), 64):
        if typ == b"keys":
            (n,) = struct.unpack_from(">I", data, s + 4)
            pos = s + 8
            for _ in range(min(n, 1024)):
                (size,) = struct.unpack_from(">I", data, pos)
                if size < 8:
                    break
                keys.append(data[pos + 8:pos + size].decode("utf-8", "replace"))
                pos += size
        elif typ == b"ilst":
            for t2, s2, e2 in _walk(rd, s, e, 1024):
                index = int.from_bytes(t2, "big")
                for t3, s3, e3 in _walk(rd, s2, e2, 8):
                    if t3 == b"data" and e3 - s3 > 8:
                        kind = int.from_bytes(data[s3 + 1:s3 + 4], "big")
                        if kind == 1:                      # UTF-8
                            values[index] = data[s3 + 8:e3].decode("utf-8", "replace")
    named = {keys[i - 1]: v for i, v in values.items() if 1 <= i <= len(keys)}
    loc = named.get("com.apple.quicktime.location.ISO6709")
    if loc:
        _location(loc, out)
    for key, field in (("com.apple.quicktime.make", "make"),
                       ("com.apple.quicktime.model", "model")):
        if named.get(key, "").strip():
            out.setdefault(field, named[key].strip())
    m = _APPLE_DATE.match(named.get("com.apple.quicktime.creationdate", "").strip())
    if m:
        try:
            local = datetime(*(int(x) for x in m.groups()[:6]))
        except ValueError:
            return
        out.setdefault("taken_local", local.strftime("%Y-%m-%dT%H:%M:%S"))
        offset = 0 if m[7] == "Z" else _offset(m[7])
        if offset is not None:
            out.setdefault("offset_sec", offset)
