"""Home-screen icons, drawn in code.

"Add to Home Screen" wants raster icons: iOS ignores an SVG apple-touch-icon
(it falls back to a screenshot of the page) and Android launchers want 192
and 512 px PNGs in the manifest. Shipping binary files in the package would
be the only non-text asset in the repo, so the PNGs are rendered here with
zlib + struct — an indigo field with a white ring and dot, anti-aliased by
pixel coverage — and cached after the first request. The same mark as SVG
serves as the browser favicon.

The PNGs are full-bleed (no transparent corners): iOS and Android's maskable
icons cut their own shape, and transparent corners would come out black.
"""

from __future__ import annotations

import math
import struct
import zlib
from functools import lru_cache

TOP = (99, 102, 241)        # indigo-500
BOTTOM = (124, 58, 237)     # violet-600
WHITE = (255, 255, 255)

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
       '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
       '<stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#7c3aed"/>'
       '</linearGradient></defs>'
       '<rect width="512" height="512" rx="112" fill="url(#g)"/>'
       '<circle cx="256" cy="256" r="148" fill="none" stroke="#fff" '
       'stroke-width="34" opacity="0.95"/>'
       '<circle cx="256" cy="256" r="50" fill="#fff"/></svg>')


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


@lru_cache(maxsize=8)
def png(size: int) -> bytes:
    """A size x size RGB PNG of the app mark.

    Pure Python is slow per pixel, so only the pixels the ring and dot can
    touch are computed: the background gradient depends on x+y alone, so
    each row is a slice of one precomputed strip."""
    c = (size - 1) / 2
    ring_r, ring_half = size * 148 / 512, size * 17 / 512
    dot_r = size * 50 / 512
    strip = b"".join(bytes(_mix(TOP, BOTTOM, i / (2 * (size - 1))))
                     for i in range(2 * size - 1))
    outer, inner = ring_r + ring_half + 1, ring_r - ring_half - 1
    rows = []
    for y in range(size):
        row = bytearray(strip[3 * y:3 * (y + size)])
        dy2 = (y - c) ** 2
        spans = []
        if dy2 <= outer * outer:
            ox = math.sqrt(outer * outer - dy2)
            ix = math.sqrt(inner * inner - dy2) if dy2 < inner * inner else 0.0
            spans += [(c - ox, c - ix), (c + ix, c + ox)]
        if dy2 <= (dot_r + 1) ** 2:
            dx = math.sqrt((dot_r + 1) ** 2 - dy2)
            spans.append((c - dx, c + dx))
        for lo, hi in spans:
            for x in range(max(0, int(lo)), min(size, int(hi) + 2)):
                d = math.sqrt((x - c) ** 2 + dy2)
                cover = max(_clamp(ring_half - abs(d - ring_r) + 0.5) * 0.95,
                            _clamp(dot_r - d + 0.5))
                if cover:
                    i = 3 * x
                    bg = (strip[3 * (x + y)], strip[3 * (x + y) + 1],
                          strip[3 * (x + y) + 2])
                    row[i:i + 3] = bytes(_mix(bg, WHITE, cover))
        rows.append(b"\x00" + bytes(row))             # filter type: none

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))
