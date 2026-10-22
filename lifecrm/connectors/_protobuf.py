"""Just enough of the protobuf wire format to read an Apple Notes body.

A helper, not a connector (the leading underscore keeps it out of the
registry). Apple Notes stores each note's text as a gzip-compressed protobuf
message, and there is no schema file to compile against — nor a dependency
worth adding for one field. The wire format itself is small and stable:

    message := (key value)*
    key     := varint, (field_number << 3) | wire_type
    wire 0  varint            ints, bools, enums
    wire 1  8 bytes           fixed64 / double
    wire 2  varint length + that many bytes
                              strings, bytes, and nested messages
    wire 5  4 bytes           fixed32 / float

Wire types 3 and 4 (groups) were deprecated before Apple Notes existed and
never appear in it; meeting one means the bytes are not what we think they
are, so it is an error rather than something to skip past.

Only reading is here. Field meanings — which number holds the note text —
belong to the caller (`connectors/notes.py`); this module knows nothing about
notes. Written from the wire-format specification; no third-party parser's
code was consulted.
"""

from __future__ import annotations

VARINT, I64, LEN, I32 = 0, 1, 2, 5

# A varint holds at most 64 bits: ten bytes of seven bits each.
_MAX_VARINT_BYTES = 10


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """(value, position after it). ValueError on a truncated or overlong one."""
    value = shift = 0
    for i in range(_MAX_VARINT_BYTES):
        if pos >= len(buf):
            raise ValueError(f"truncated varint at byte {pos}")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
    raise ValueError(f"varint longer than {_MAX_VARINT_BYTES} bytes at byte {pos}")


def fields(buf: bytes):
    """Yield (field_number, wire_type, value) for every top-level field.

    `value` is an int for varint and fixed-width fields and the raw bytes for
    a length-delimited one — the caller decides whether those bytes are a
    string or a nested message. Raises ValueError on malformed input.
    """
    buf = bytes(buf)
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 0x07
        if number == 0:
            raise ValueError(f"field number 0 at byte {pos}")
        if wire == VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == I64:
            if pos + 8 > len(buf):
                raise ValueError(f"truncated fixed64 field {number}")
            value, pos = int.from_bytes(buf[pos:pos + 8], "little"), pos + 8
        elif wire == LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > len(buf):
                raise ValueError(f"field {number} claims {length} bytes, "
                                 f"{len(buf) - pos} remain")
            value, pos = buf[pos:pos + length], pos + length
        elif wire == I32:
            if pos + 4 > len(buf):
                raise ValueError(f"truncated fixed32 field {number}")
            value, pos = int.from_bytes(buf[pos:pos + 4], "little"), pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire} (field {number})")
        yield number, wire, value


def first(buf: bytes, *path: int) -> bytes | None:
    """The bytes of the first length-delimited field along `path`, descending
    one nested message per step — `first(b, 2, 3, 2)` is field 2's field 3's
    field 2. None when any step is absent. Raises ValueError on malformed
    input, so a corrupt body is never mistaken for an empty one.
    """
    current = bytes(buf)
    for number in path:
        hit = next((v for n, w, v in fields(current) if n == number and w == LEN), None)
        if hit is None:
            return None
        current = hit
    return current
