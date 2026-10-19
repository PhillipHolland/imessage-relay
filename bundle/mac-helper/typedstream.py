"""
Best-effort text extraction from chat.db `message.attributedBody` (an NSAttributedString
archived with NeXTSTEP "typedstream"). Newer macOS versions often leave `message.text` NULL
and keep the text only here.

Approach (same idea as imessage-exporter's streamtyped parser): find the NSString class
record, then the C-string value that follows it, introduced by the type tag `0x84 0x01 '+'`,
and read its typedstream-encoded length:
    0x00-0x7f   length in that byte
    0x81 xx xx  16-bit little-endian length
    0x82 xx*4   32-bit little-endian length
Fallback (imessage-exporter's legacy method): the bytes between `0x01 '+'` and `0x86 0x84`,
minus the length prefix. Returns None when nothing readable is found. Never raises.
"""

NSSTRING_MARKERS = (b'NSString', b'NSMutableString')
VALUE_TAG = b'\x84\x01+'
LEGACY_START = b'\x01+'
LEGACY_END = b'\x86\x84'
OBJECT_REPLACEMENT = '\ufffc'   # stands in for an attachment inside the text


def _read_len(b, p):
    if p >= len(b):
        return None, p
    x = b[p]
    if x < 0x80:
        return x, p + 1
    if x == 0x81 and p + 3 <= len(b):
        return int.from_bytes(b[p + 1:p + 3], 'little'), p + 3
    if x == 0x82 and p + 5 <= len(b):
        return int.from_bytes(b[p + 1:p + 5], 'little'), p + 5
    return None, p


def _clean(s):
    if s is None:
        return None
    s = s.replace(OBJECT_REPLACEMENT, '').strip()
    return s or None


def _modern(b):
    for marker in NSSTRING_MARKERS:
        i = b.find(marker)
        if i < 0:
            continue
        j = b.find(VALUE_TAG, i + len(marker))
        if j < 0:
            continue
        n, p = _read_len(b, j + len(VALUE_TAG))
        if n is None or p + n > len(b):
            continue
        return b[p:p + n].decode('utf-8', errors='replace')
    return None


def _legacy(b):
    i = b.find(LEGACY_START)
    if i < 0:
        return None
    rest = b[i + len(LEGACY_START):]
    j = rest.find(LEGACY_END)
    if j >= 0:
        rest = rest[:j]
    if not rest:
        return None
    skip = 3 if rest[0] == 0x81 else 5 if rest[0] == 0x82 else 1
    return rest[skip:].decode('utf-8', errors='replace')


def decode_attributed_body(blob):
    """bytes|memoryview|None -> str|None"""
    if not blob:
        return None
    try:
        b = bytes(blob)
        if b'streamtyped' not in b[:32] and not any(m in b for m in NSSTRING_MARKERS):
            return None
        return _clean(_modern(b)) or _clean(_legacy(b))
    except Exception:
        return None


def message_text(text, attributed_body, has_attachments=False):
    """Text for a chat.db row: `text` column first, then attributedBody, then a placeholder
    for attachment-only messages. None = nothing to relay."""
    t = _clean(text) if isinstance(text, str) else None
    if not t:
        t = decode_attributed_body(attributed_body)
    if not t and has_attachments:
        t = '[attachment]'
    return t
