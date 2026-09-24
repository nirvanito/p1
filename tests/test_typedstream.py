"""The typedstream reader, pinned with blobs spelled out byte by byte.

Every blob here is written by hand from the format, with comments, so the
tests double as documentation of it. The two table rules that make references
resolve — one shared-string table for type descriptors AND class names, and an
object table where an object's slot is taken before its class chain — each
have a test that would fail if either rule were wrong.

(The reader was also validated against 75 real-world blobs from crabstep's test
data, which are GPL-3.0 and deliberately not vendored: 74 parse fully with the
right text; the 75th is truncated on purpose and correctly yields None.)
"""

import pytest

from lifecrm.connectors import typedstream as ts

HEADER = bytes([0x04, 0x0B]) + b"streamtyped" + bytes([0x81, 0xE8, 0x03])


def length(n: int) -> bytes:
    """A typedstream integer: one byte below 0x80, else a tagged wider form."""
    if n < 0x80:
        return bytes([n])
    if n < 0x10000:
        return b"\x81" + n.to_bytes(2, "little")
    return b"\x82" + n.to_bytes(4, "little")


def attributed_body(text: str, mutable: bool = False, extra: bytes = b"") -> bytes:
    """An archived NSAttributedString, shaped like iMessage's attributedBody.

    `extra` is spliced in after the string field, where real blobs carry
    their attribute runs.
    """
    raw = text.encode("utf-8")
    out = HEADER
    out += b"\x84\x01@"                          # new type "@"            strings[0]
    out += b"\x84"                               # new object              objects[0]
    if mutable:
        out += b"\x84\x84\x19NSMutableAttributedString\x00"   # class     objects[1]
        out += b"\x84\x84\x12NSAttributedString\x00"          # its super objects[2]
        out += b"\x84\x84\x08NSObject\x00\x85"                # NSObject  objects[3]
        out += b"\x92"                           # field type -> strings[0] "@"
        out += b"\x84"                           # new object              objects[4]
        out += b"\x84\x84\x0fNSMutableString\x01"             # class     objects[5]
        out += b"\x84\x84\x08NSString\x01"                    # its super objects[6]
        out += b"\x95"                           # super -> objects[3] NSObject
    else:
        out += b"\x84\x84\x12NSAttributedString\x00"          # class     objects[1]
        out += b"\x84\x84\x08NSObject\x00\x85"                # NSObject  objects[2]
        out += b"\x92"                           # field type -> strings[0] "@"
        out += b"\x84"                           # new object              objects[3]
        out += b"\x84\x84\x08NSString\x01"                    # class     objects[4]
        out += b"\x94"                           # super -> objects[2] NSObject
    out += b"\x84\x01+"                          # field type "+": raw UTF-8 bytes
    out += length(len(raw)) + raw
    out += b"\x86"                               # end of the NSString
    out += extra
    out += b"\x86"                               # end of the attributed string
    return out


# ---------------------------------------------------------------- text

def test_reads_the_text_of_an_attributed_string():
    assert ts.attributed_text(attributed_body("see you at 6")) == "see you at 6"


def test_mutable_classes_shift_every_reference_and_still_read():
    """NSMutableAttributedString / NSMutableString add two classes to the
    chain, so NSObject is referenced as 0x95 instead of 0x94."""
    blob = attributed_body("edited later", mutable=True)
    assert ts.attributed_text(blob) == "edited later"


def test_long_text_uses_a_two_byte_length():
    text = "x" * 300                              # 0x81 + little-endian 300
    assert ts.attributed_text(attributed_body(text)) == text


def test_length_counts_bytes_not_characters():
    text = "héllo 👋🏽 مرحبا 𝖍𝖊𝖑𝖑𝖔"
    assert ts.attributed_text(attributed_body(text)) == text


def test_an_empty_string_is_text_not_a_failure():
    assert ts.attributed_text(attributed_body("")) == ""


# ---------------------------------------------------------------- tables

def test_shared_strings_hold_type_descriptors_and_class_names_in_one_table():
    """After one NSString, the shared strings are
    ["@", "NSAttributedString", "NSObject", "NSString", "+"] — so the "+"
    descriptor is reference 0x96. With separate tables it would be 0x93."""
    second = (b"\x92"          # field type -> strings[0] "@"
              b"\x84"          # new object                           objects[5]
              b"\x96"          # class -> objects[4], the NSString class
              b"\x96"          # field type -> strings[4] "+"
              b"\x05world"
              b"\x86")
    blob = attributed_body("hello", extra=second)
    assert ts.plain_strings(blob) == ["hello", "world"]
    assert ts.attributed_text(blob) == "hello"


def test_an_objects_slot_is_taken_before_its_class_chain():
    """The object table after the first string is
    [root, NSAttributedString, NSObject, string, NSString]: the NSString
    class is slot 4 only because the string object took slot 3 first."""
    wrong_class = (b"\x92\x84"
                   b"\x95"         # objects[3]: the string OBJECT, not a class
                   b"\x96\x03bad\x86")
    blob = attributed_body("hello", extra=wrong_class)
    assert ts.plain_strings(blob) == ["hello"]   # the bad field is not a string


def test_a_later_field_it_cannot_read_does_not_lose_the_text():
    unsupported = b"\x84\x01v"                    # descriptor "v" (void): unreadable
    blob = attributed_body("still here", extra=unsupported)
    assert ts.attributed_text(blob) == "still here"


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("blob", [
    None, b"",
    b"not a typedstream at all",
    HEADER.replace(b"streamtyped", b"streamtypex") + b"\x84\x01@",
    attributed_body("truncated in the middle")[:-12],
])
def test_returns_none_rather_than_guessing(blob):
    assert ts.attributed_text(blob) is None


def test_a_length_running_past_the_end_is_refused_not_trusted():
    """A length prefix that promises more bytes than exist: a byte-scanner
    would slice what is there and return it as text."""
    raw = b"short"
    blob = attributed_body("short").replace(b"\x05short", b"\x81\x9d\x00short")
    assert raw in blob
    assert ts.attributed_text(blob) is None


# ---------------------------------------------------------------- descriptors

@pytest.mark.parametrize("text,expected", [
    ("@", ["@"]),
    ("iI", ["i", "I"]),
    ("[16c]", [("bytes", 16)]),
    ("[2i]", ["i", "i"]),
    ("{CGSize=dd}", ["d", "d"]),
    ("{CGRect={CGPoint=dd}{CGSize=dd}}", ["d", "d", "d", "d"]),
    ("r*", ["*"]),
    ("{_NSRange=QQ}", ["Q", "Q"]),
])
def test_descriptors(text, expected):
    assert ts.parse_descriptor(text) == expected


@pytest.mark.parametrize("text", ["^i", "v", "[c]", "{CGSize=dd", "[3i", "(u=if)"])
def test_unsupported_descriptors_are_refused(text):
    with pytest.raises(ts.TypedStreamError):
        ts.parse_descriptor(text)


def test_a_hostile_array_count_is_refused():
    with pytest.raises(ts.TypedStreamError):
        ts.parse_descriptor("[999999i]")
