"""Apple typedstream (NSArchiver) — enough of it to read message text.

Most iMessage text is not in `message.text`. On current iOS it lives in
`message.attributedBody`: an NSAttributedString serialized with NSArchiver's
"typedstream" format, a NeXT-era binary encoding with no public spec. A reader
that only looks at the text column silently loses a large share of messages,
and nothing tells it so.

This is a small, general reader for the format, written for this project from
the format itself. The layout was cross-checked against `crabstep`, the parser
behind imessage-exporter (GPL-3.0). No code was taken from it; its real-world
test blobs were used to validate this reader locally and are not vendored.

Format, briefly
---------------
header    04 | 0B "streamtyped" | 81 E8 03     version 4, signature, system 1000
integer   one byte, or 81 + 2 / 82 + 4 / 87 + 8 bytes, little-endian
83        DECIMAL: an IEEE float or double follows
84        START: a new object, class or shared string follows
85        EMPTY: nil, or the end of a class chain
86        END: the end of an object's fields
92..      a reference: index = byte - 0x92, into one of the two tables below

Two tables grow in archive order, and every reference points into one of them:

  shared strings   type descriptors AND class names — one table for both
  objects          objects, classes and C-string slots — an object's slot is
                   taken *before* its class chain is read, which is what makes
                   the indices line up

An attributed string's first field is its plain string: an NSString (or
NSMutableString) object whose single field has type '+', a length-prefixed
UTF-8 byte run. That is the message text.

A heuristic byte-scan (find b"NSString", skip five bytes, read a length) gets
the common case right, but it cannot tell when it is wrong and it cannot read
anything else. This reader validates the header, follows references, and
returns None rather than plausible garbage when the bytes do not fit.
"""

from __future__ import annotations

import struct

I_16, I_32, DECIMAL, START, EMPTY, END, I_64 = 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87
REFERENCE_TAG = 0x92
_INT_SIZES = {I_16: 2, I_32: 4, I_64: 8}

_SIGNED = frozenset("cilqs")
_UNSIGNED = frozenset("CILQSB")
_SCALARS = frozenset("@#:%*+fd") | _SIGNED | _UNSIGNED
_QUALIFIERS = frozenset("rnNoORV")
_MAX_SLOTS = 4096          # a descriptor expanding past this is hostile, not real

__all__ = ["TypedStreamError", "attributed_text", "plain_strings", "parse_descriptor"]


class TypedStreamError(ValueError):
    """The bytes are not a typedstream this reader can follow."""


class Class:
    __slots__ = ("name", "version", "parent")

    def __init__(self, name: str, version: int, parent: int | None = None):
        self.name, self.version, self.parent = name, version, parent


class Object:
    __slots__ = ("cls", "fields")

    def __init__(self, cls: int, fields: list[list]):
        self.cls, self.fields = cls, fields


class Ref:
    """A field that points at another object-table slot."""
    __slots__ = ("index",)

    def __init__(self, index: int):
        self.index = index


# ---------------------------------------------------------------- descriptors

def parse_descriptor(text: str) -> list:
    """Type descriptor -> one slot per value, in order.

    'iI' -> ['i', 'I'];  '[16c]' -> [('bytes', 16)];  structs flatten to their
    leaves: '{CGSize=dd}' -> ['d', 'd'].
    """
    out: list = []
    pos = 0
    while pos < len(text):
        pos = _parse_one(text, pos, out)
    if len(out) > _MAX_SLOTS:
        raise TypedStreamError(f"type descriptor expands to {len(out)} slots")
    return out


def _parse_one(text: str, pos: int, out: list) -> int:
    while pos < len(text) and text[pos] in _QUALIFIERS:
        pos += 1
    if pos >= len(text):
        raise TypedStreamError("type descriptor ends early")
    c = text[pos]
    pos += 1
    if c in _SCALARS:
        out.append(c)
        return pos
    if c == "[":
        start = pos
        while pos < len(text) and "0" <= text[pos] <= "9":
            pos += 1
        if pos == start:
            raise TypedStreamError("array descriptor without a count")
        count = int(text[start:pos])
        if text[pos:pos + 2] in ("c]", "C]"):     # a char array stays whole
            out.append(("bytes", count))
            return pos + 2
        element: list = []
        pos = _parse_one(text, pos, element)
        if pos >= len(text) or text[pos] != "]":
            raise TypedStreamError("unterminated array descriptor")
        if count * len(element) > _MAX_SLOTS:
            raise TypedStreamError(f"array descriptor of {count} elements is too large")
        out.extend(element * count)
        return pos + 1
    if c == "{":
        # The struct's name is skipped, not scanned: the 'S' and 'i' in
        # 'CGSize' are letters of a name, not slots.
        while True:
            if pos >= len(text):
                raise TypedStreamError("unterminated struct descriptor")
            ch = text[pos]
            pos += 1
            if ch == "}":
                return pos
            if ch == "=":
                break
        while True:
            if pos >= len(text):
                raise TypedStreamError("unterminated struct descriptor")
            if text[pos] == "}":
                return pos + 1
            pos = _parse_one(text, pos, out)
    raise TypedStreamError(f"unsupported type '{c}' in descriptor")


# ---------------------------------------------------------------- reader

class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.strings: list[bytes] = []
        self.objects: list = []
        self.texts: list[str] = []          # plain-string objects, completion order
        self._descriptors: dict[int, list] = {}

    # --- primitives ---------------------------------------------------------

    def _peek(self) -> int:
        if self.pos >= len(self.data):
            raise TypedStreamError("unexpected end of stream")
        return self.data[self.pos]

    def _byte(self) -> int:
        b = self._peek()
        self.pos += 1
        return b

    def _take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise TypedStreamError(f"needs {n} byte(s) at offset {self.pos}, "
                                   f"stream has {len(self.data) - self.pos}")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def _int(self, signed: bool) -> int:
        b = self._byte()
        size = _INT_SIZES.get(b)
        if size:
            return int.from_bytes(self._take(size), "little", signed=signed)
        return b - 256 if signed and b >= 0x80 else b

    def _pointer(self, b: int, table: list) -> int:
        idx = b - REFERENCE_TAG
        if idx < 0 or idx >= len(table):
            raise TypedStreamError(f"reference 0x{b:02x} at offset {self.pos - 1} "
                                   f"points past a table of {len(table)}")
        return idx

    def _raw_string(self) -> bytes:
        return self._take(self._int(signed=False))

    def _shared_string(self) -> int:
        b = self._byte()
        if b == START:
            self.strings.append(self._raw_string())
            return len(self.strings) - 1
        if b == EMPTY:
            raise TypedStreamError("expected a string, found nil")
        return self._pointer(b, self.strings)

    # --- structure ----------------------------------------------------------

    def run(self) -> None:
        version = self._int(signed=False)
        signature = self._raw_string()
        system = self._int(signed=True)
        if (version, signature, system) != (4, b"streamtyped", 1000):
            raise TypedStreamError("not a typedstream (bad header)")
        while self.pos < len(self.data):
            t = self._type()
            if t is not None:
                self._values(t)

    def _type(self) -> int | None:
        b = self._byte()
        if b == START:
            self.strings.append(self._raw_string())
            return len(self.strings) - 1
        if b in (END, EMPTY):
            return None
        return self._pointer(b, self.strings)

    def _descriptor(self, idx: int) -> list:
        slots = self._descriptors.get(idx)
        if slots is None:
            slots = parse_descriptor(self.strings[idx].decode("latin-1"))
            self._descriptors[idx] = slots
        return slots

    def _values(self, type_idx: int) -> list:
        return [self._value(t) for t in self._descriptor(type_idx)]

    def _class(self) -> int | None:
        """A class chain, child first: START name version, repeated, ended by
        EMPTY (no superclass) or a reference to a class already seen."""
        first = prev = None
        while True:
            b = self._byte()
            if b == START:
                name = self.strings[self._shared_string()].decode("utf-8", "replace")
                version = self._int(signed=False)
                idx = len(self.objects)
                self.objects.append(Class(name, version))
                if prev is not None:
                    self.objects[prev].parent = idx
                first = idx if first is None else first
                prev = idx
            elif b == EMPTY:
                parent = None
                break
            else:
                parent = self._pointer(b, self.objects)
                break
        if first is None:
            return parent
        self.objects[prev].parent = parent
        return first

    def _object(self) -> int | None:
        """An object value. Leaves its last byte — END for an inline object,
        or the EMPTY/reference byte itself — for the caller to consume."""
        b = self._peek()
        if b == EMPTY:
            return None
        if b != START:
            return self._pointer(b, self.objects)
        slot = len(self.objects)
        self.objects.append(None)            # the slot is taken before the class
        self.pos += 1
        cls = self._class()
        if cls is None:
            return slot
        fields = []
        while self._peek() != END:
            t = self._type()
            if t is not None:
                fields.append(self._values(t))
        obj = Object(cls, fields)
        self.objects[slot] = obj
        self._note_text(obj)
        return slot

    def _value(self, t):
        if t == "+":
            return self._raw_string()
        if t == "@":
            ref = self._object()
            self.pos += 1
            return None if ref is None else Ref(ref)
        if t == "*":                             # C string: tracked by slot
            b = self._byte()
            if b == EMPTY:
                return None
            if b == START:
                idx = self._shared_string()
                self.objects.append(("cstring", idx))
                return self.strings[idx]
            entry = self.objects[self._pointer(b, self.objects)]
            if not (isinstance(entry, tuple) and entry[0] == "cstring"):
                raise TypedStreamError("C-string reference points at a non-string")
            return self.strings[entry[1]]
        if t == "#":
            return self._class()
        if t in (":", "%"):                      # selector / atom
            if self._peek() == EMPTY:
                self.pos += 1
                return None
            return self.strings[self._shared_string()]
        if t in _SIGNED:
            return self._int(signed=True)
        if t in _UNSIGNED:
            return self._int(signed=False)
        if t in ("f", "d"):
            if self._peek() == DECIMAL:
                self.pos += 1
                size, fmt = (4, "<f") if t == "f" else (8, "<d")
                return struct.unpack(fmt, self._take(size))[0]
            return float(self._int(signed=True))
        if isinstance(t, tuple) and t[0] == "bytes":
            return self._take(t[1])
        raise TypedStreamError(f"unsupported type {t!r}")

    # --- interpretation -----------------------------------------------------

    def class_chain(self, idx: int | None) -> list[str]:
        names, seen = [], set()
        while idx is not None and idx not in seen and 0 <= idx < len(self.objects):
            seen.add(idx)
            node = self.objects[idx]
            if not isinstance(node, Class):
                break
            names.append(node.name)
            idx = node.parent
        return names

    def _note_text(self, obj: Object) -> None:
        chain = self.class_chain(obj.cls)
        if "NSString" not in chain or "NSAttributedString" in chain:
            return
        first = obj.fields[0][0] if obj.fields and obj.fields[0] else None
        if isinstance(first, bytes):
            self.texts.append(first.decode("utf-8", "replace"))


def _read(blob) -> _Reader | None:
    if not blob:
        return None
    reader = _Reader(bytes(blob))
    try:
        reader.run()
    except (TypedStreamError, RecursionError, struct.error):
        # A later field this reader cannot follow does not un-read what came
        # before: every string recorded so far was parsed structurally, whole.
        pass
    return reader


def attributed_text(blob) -> str | None:
    """The plain text of an archived NSAttributedString — the first complete
    string object, which is always the attributed string's own text — or None
    when the blob is empty or not a typedstream this reader can follow."""
    reader = _read(blob)
    if reader is None or not reader.texts:
        return None
    return reader.texts[0]


def plain_strings(blob) -> list[str]:
    """Every plain-string object in the stream, in the order they completed."""
    reader = _read(blob)
    return list(reader.texts) if reader else []
