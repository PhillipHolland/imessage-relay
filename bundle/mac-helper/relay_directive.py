"""iMessage Relay v1.4.7 -- conversational addressing (directive parsing).
Split out of relay_common.py so each file stays under 1000 lines. Always reached through
relay_common (rc.parse_directive, rc.DIRECTIVE_DICT_WORDS, ...); relay_common re-exports these
names lazily. Do not import relay_directive before relay_common.

A directive is an owner reply that names a recipient conversationally instead of with the plain
"<Name>:" colon prefix:
  verb forms  : "text Sam ...", "tell Sam: ...", "send a message to Sam. ...", "@Sam ...",
                "send to Sam ...", "reply to Sam ...", "message Sam ...", "send Sam ..."
  bare forms  : "Sam, ..."  and  "Sam - ..."   (comma or spaced dash, no verb)
Deterministic, case-insensitive, whole-word. The name must resolve to exactly one directory entry
(its label, full name, or a unique first name). parse_directive returns the FORM and the matched
name so the caller can decide whether to ask the owner to confirm (W1). A verb directive aimed at a
phone/email that has never texted in is refused, never routed to the last sender (W4)."""
import re
import relay_common as rc

# Verb-led directives. Longest alternatives first so "send a message to" wins over "send".
_DIRECTIVE_VERB_RE = re.compile(
    r'^[ \t]*(?:'
    r'send[ \t]+a[ \t]+message[ \t]+to|send[ \t]+a[ \t]+text[ \t]+to|send[ \t]+to|'
    r'reply[ \t]+to|message[ \t]+to|text|tell|send|message|to'
    r')\b[ \t]*', re.I)
_DIRECTIVE_AT_RE = re.compile(r'^[ \t]*@[ \t]*')
_NAME_SEP_CHARS = ':,.\u2013\u2014-'    # may sit right after the name (colon, comma, period, en/em dash, hyphen)
_SEP_STRIP_RE = re.compile(r'^\s*[' + re.escape(_NAME_SEP_CHARS) + r']?\s*')
_BARE_SEP_RE = re.compile(r'^(?:[ \t]*,[ \t]*|[ \t]+[\u2013\u2014-][ \t]+)')

# A leading phone number or email at the start of the text after a verb (W4). The phone run allows the
# usual separators but must hold at least 7 digits, so "5:30" or "at 5" is not mistaken for a number.
_LEAD_EMAIL_RE = re.compile(r'^[ \t]*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})')
_LEAD_PHONE_RE = re.compile(r'^[ \t]*(\+?[0-9][0-9 ().\-]{5,}[0-9])')

# Common given names that are also everyday words. A BARE "Will, ..." / "May - ..." is held for a /yes
# confirmation because the owner may just be starting a sentence, not addressing a contact (W1).
DIRECTIVE_DICT_WORDS = frozenset(w.lower() for w in (
    'Will', 'May', 'Mark', 'Bill', 'Grace', 'Hope', 'Faith', 'Joy', 'June', 'April', 'Rose', 'Jack',
    'Frank', 'Art', 'Sunny', 'Dawn', 'Summer', 'Pat', 'Gene', 'Chase', 'Rich', 'Rob', 'Sue', 'Drew',
    'Lane', 'Max', 'Ray', 'Bob', 'Jean', 'Hunter', 'Carol',
    # a few more everyday-word given names in the same spirit (kept conservative on purpose):
    'Mercy', 'Reed', 'Wade', 'Miles', 'Penny', 'Olive', 'Dale', 'Dean', 'Earl', 'Guy', 'Sandy', 'Ivy'))


def _match_name_prefix(rest, keys):
    """Longest key in `keys` that is a whole-word prefix of `rest` (case-insensitive). Whole-word: the
    char right after the key is end-of-string, whitespace, or a name separator. Returns (key, handles)
    or (None, None)."""
    low = rest.lower()
    best = None
    for key in keys:
        if not key or not low.startswith(key):
            continue
        nxt = rest[len(key):len(key) + 1]
        if nxt == '' or nxt.isspace() or nxt in _NAME_SEP_CHARS:
            if best is None or len(key) > len(best):
                best = key
    if best is None:
        return None, None
    return best, keys[best]


def _lead_handle(after, cc):
    """A leading email or phone number in `after` -> (normalized_handle, rest, raw) or (None, None, None)."""
    m = _LEAD_EMAIL_RE.match(after)
    if m:
        tok = m.group(1)
        return rc.normalize_handle(tok, cc), after[m.end():], tok
    m = _LEAD_PHONE_RE.match(after)
    if m:
        tok = m.group(1)
        if sum(c.isdigit() for c in tok) >= 7:
            return rc.normalize_handle(tok, cc), after[m.end():], tok
    return None, None, None


def parse_directive(text, cfg, directory=None):
    """Conversational addressing -> (handle|None, body, status, form, name).
    status: 'match' | 'ambiguous' | 'empty' | 'unknown_handle' | 'none'.
    form:   'verb' (verb- or @-led) | 'bare' (comma/dash) | 'handle' (verb + phone/email) | ''.
    name:   the lowercased contact key that matched ('' for the handle form), for the dict-word check.
    Deterministic, case-insensitive. The name must resolve to exactly one directory entry. 'empty' = a
    recipient resolved but no message followed; 'unknown_handle' = a verb aimed a phone/email that has
    never texted in (refuse, never fall back to recent); 'none' = not a directive."""
    s = text if isinstance(text, str) else ''
    d = rc.build_directory(cfg, []) if directory is None else directory
    keys = rc.contact_keys(d)
    cc = cfg.get('defaultCountryCode', '1')
    m = _DIRECTIVE_AT_RE.match(s)
    had_verb = bool(m)
    if not m:
        m = _DIRECTIVE_VERB_RE.match(s)
        had_verb = bool(m)
    after = s[m.end():] if m else s
    key, hits = _match_name_prefix(after, keys)
    if key:
        tail = after[len(key):]
        if had_verb:
            body = _SEP_STRIP_RE.sub('', tail, count=1).strip()
            form = 'verb'
        else:
            mm = _BARE_SEP_RE.match(tail)
            if not mm:
                return None, text, 'none', '', ''
            body = tail[mm.end():].strip()
            form = 'bare'
        if len(hits) > 1:
            return None, text, 'ambiguous', form, key
        handle = next(iter(hits))
        if not body:
            return handle, '', 'empty', form, key
        return handle, body, 'match', form, key
    # W4: a verb (or @) followed by a phone/email. Route only to a KNOWN handle; otherwise refuse.
    if had_verb:
        h, rest2, raw = _lead_handle(after, cc)
        if h:
            body = _SEP_STRIP_RE.sub('', rest2, count=1).strip()
            if any(e['handle'] == h for e in d):
                if not body:
                    return h, '', 'empty', 'handle', ''
                return h, body, 'match', 'handle', ''
            if rc.valid_handle(h):
                return h, text, 'unknown_handle', 'handle', ''
    return None, text, 'none', '', ''
