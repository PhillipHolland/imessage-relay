"""iMessage Relay v1.4.8 -- echo guard + bot-output detection.
Split out of relay_common.py so each file stays under 1000 lines. Always reached through relay_common
(rc.is_echoed_inbound, rc.looks_like_bot_output, rc.record_relayed_text, ...); relay_common re-exports
these names lazily. Do not import relay_echo before relay_common."""
import hashlib
import hmac
import os
import re
import time
import relay_common as rc


# ---------- echo guard: stop the owner's reply being a relayed message bounced straight back ----------
# Keyed HMAC with a random per-install secret: stored values reveal nothing about the text and cannot be forged.
def _echo_secret():
    try:
        with open(rc.ECHO_SECRET_PATH, 'rb') as f:
            b = f.read()
        if len(b) >= 16:
            return b
    except OSError:
        pass
    b = os.urandom(32)
    os.makedirs(rc.APP_DIR, exist_ok=True)
    fd = os.open(rc.ECHO_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(b)
    return b


def _echo_norm(text):
    s = re.sub(r'\s+', ' ', str(text or '')).strip().lower()
    # ignore trailing punctuation / ellipsis so "... saturday." echoes "... saturday" (nit)
    return re.sub(r'[\s.!?,;:\u2026]+$', '', s)


def echo_hmac(text):
    return hmac.new(_echo_secret(), _echo_norm(text).encode('utf-8'), hashlib.sha256).hexdigest()


def record_relayed_text(text, at=None, path=None, handle=''):
    """Watcher: remember an HMAC of each relayed inbound body (keys only, never the body) so the host
    can refuse if the owner's reply is that same text bounced straight back to the SAME sender.
    Only substantial bodies (>= ECHO_MIN_LEN chars) are recorded, so a stranger texting a short word
    ('ok', 'yes') can never block the owner from replying with that word."""
    norm = _echo_norm(text)
    if len(norm) < rc.ECHO_MIN_LEN:
        return
    at = time.time() if at is None else at
    p = path or rc.ECHO_STORE_PATH
    items = [e for e in (rc.read_json(p, {}) or {}).get('entries', [])
             if isinstance(e, dict) and at - float(e.get('at', 0)) < rc.ECHO_TTL_S]
    items.append({'h': echo_hmac(text), 'at': float(at), 'g': rc.normalize_handle(handle) if handle else ''})
    rc.write_json_atomic(p, {'entries': items[-500:]}, mode=0o600)


# Item 13: one shared list of the leading sigils / phrases of EVERY line the routine (relay_post.py)
# can post into X Chat — message/digest lines carry no such prefix, but every status, sent, failure,
# mute, needs-target, wrong-account and refused line does. looks_like_bot_output() uses it so the
# watcher never relays one of the bot's own status lines back out as an iMessage. Keep this in sync
# with relay_post.format_event().
BOT_LINE_SIGILS = (
    '\u2192',        # →  sent / dry-run
    '\u26a0',        # ⚠  warnings: send_failed, wrong_account, refused, messages_not_running, fda_missing
    '\u23f3',        # ⏳  older catching-up lines
    '\U0001f504',    # 🔄  older resynced lines
    '\u2139',        # ℹ  older status lines
    '\U0001f6ab',    # 🚫  spam tag, spam count, older mute lines
    '\u2705',        # ✅  /test pass rows, older unmute lines
    '\u231b',        # ⌛  older guess-expired lines
    '\U0001f319',    # 🌙  older digest headers
    '\U0001f4ec',    # 📬  older digest headers
    '\u2753',        # ❓  older confirm prompts
    '\U0001f9ea',    # 🧪  /test report ("🧪 relay test #tag …")
)
BOT_LINE_PHRASES = (
    "Who's this for?", "Who\u2019s this for?", 'Who is this for?',
    'What should I send to',
    'Who do you want to',
    'To stop relaying', 'Stop relaying',
    'Nothing to confirm', 'Nothing is waiting',
    'Catching up. Nothing is dropped.',
    'Older messages were skipped after a gap.',
    'Watching Messages again.',
    'Chrome hasn\u2019t checked in', "Chrome hasn't checked in",
    'Chrome is open again.',
    'Relaying is back on.',
    'Snoozing until ',
    'Say /snooze',
    'Replies go to ',
    'Replies are unclear.',
    'No one to reply to yet.',
    'Muted ', 'Unmuted ', 'Dry run.', 'Added ',
    'Cancelled.',
    'That isn\u2019t a choice', "That isn't a choice",
    'That choice expired',
    'More than one ',
    'No recent unknown number',
    'Couldn\u2019t tell whose', "Couldn't tell whose",
    'That doesn\u2019t look like a phone', "That doesn't look like a phone",
    'That\u2019s your own number', "That's your own number",
    'That doesn\u2019t match the', "That doesn't match the",
    'Paused. Texts that arrive while paused are not kept.',
    'Not sent. The question timed out.',
    '/status \u00b7 /who',
    'On \u00b7 v', 'Paused \u00b7 v',
)
BOT_FORMAT_RE = re.compile(
    r'^\s*(?:' + '|'.join(list(BOT_LINE_SIGILS) + [re.escape(p) for p in BOT_LINE_PHRASES]) + r')', re.I)
# Routing replies and confirm prompts are structured, not one fixed prefix. Old colon wording stays
# matched so a quoted line from before v1.4.22 is still not texted out.
BOT_STRUCTURED_RE = re.compile(
    r"^\s*(?:can[\u2019']t send to\b(?:"
    r".*:\s*(?:they|you)\b"
    r"|.*\.\s+(?:They|No text|They[\u2019']ve|Group)"
    r")"
    r"|can[\u2019']t reach the\b"
    r"|group replies aren[\u2019']t supported"
    r"|did you mean\s*/"
    r"|(?:reply to|send to|text|did you mean|add|stop relaying)\s+.+\?\s*reply yes within 2 min"
    r"|text\s+.+\bat which\?"
    r"|reply\s+1[\u2013-]\d+,\s*or no"
    r"|not sent to\s+\S.*\bthe question timed out"
    r"|while you were away\s+\u00b7\s+\d+\s+texts?"
    r")", re.I)


def looks_like_bot_output(text):
    """A body in one of the bot's own relay-chat status formats must never be sent as an iMessage.
    Covers every non-message line relay_post.format_event() can emit (see BOT_LINE_SIGILS/PHRASES)
    plus the host's structured routing replies (BOT_STRUCTURED_RE, item 13)."""
    t = str(text or '')
    return bool(BOT_FORMAT_RE.match(t) or BOT_STRUCTURED_RE.match(t))


def is_echoed_inbound(text, prefix_stripped=None, at=None, path=None, handle=''):
    """True only if a SUBSTANTIAL body (>= ECHO_MIN_LEN) the owner is sending matches one recently
    relayed in FROM THE SAME SENDER (`handle`). Keying by sender means one person's text can never
    block the owner's replies to anyone else, and the length floor means short words are never echoes."""
    at = time.time() if at is None else at
    g = rc.normalize_handle(handle) if handle else ''
    hs = set()
    if len(_echo_norm(text)) >= rc.ECHO_MIN_LEN:
        hs.add(echo_hmac(text))
    if prefix_stripped is not None and len(_echo_norm(prefix_stripped)) >= rc.ECHO_MIN_LEN:
        hs.add(echo_hmac(prefix_stripped))
    if not hs:
        return False
    for e in (rc.read_json(path or rc.ECHO_STORE_PATH, {}) or {}).get('entries', []):
        if not isinstance(e, dict) or at - float(e.get('at', 0)) >= rc.ECHO_TTL_S:
            continue
        if e.get('h') in hs and (not g or not e.get('g') or e.get('g') == g):
            return True
    return False


# ---------- v1.4.6 quiet names: route a native quote of an UNPREFIXED relayed line ----------
# X's quote preview may show the whole line or only its start, so a few keyed HMACs of the normalized
# text's prefixes are stored (never the text). A quote matches when one of its own prefixes equals one.
QUOTE_PREFIXES = (12, 20, 32, 48, 80)


def _quote_norm(text):
    s = re.sub(r'\s+', ' ', str(text or '')).strip().lower()
    s = re.sub(r'(?:\u2026|\.\.\.)\s*$', '', s).rstrip()
    return s


def _quote_hashes(norm):
    out = set()
    for n in QUOTE_PREFIXES:
        if len(norm) >= n:
            out.add(hmac.new(_echo_secret(), ('q%d:' % n + norm[:n]).encode('utf-8'), hashlib.sha256).hexdigest()[:32])
    if norm:
        out.add(hmac.new(_echo_secret(), ('qall:' + norm).encode('utf-8'), hashlib.sha256).hexdigest()[:32])
    return out


def record_quote_keys(text, route, at=None, path=None):
    norm = _quote_norm(text)
    if len(norm) < 4 or not route:
        return
    at = time.time() if at is None else at
    p = path or rc.QUOTE_KEYS_PATH
    items = [e for e in (rc.read_json(p, {}) or {}).get('entries', [])
             if isinstance(e, dict) and at - float(e.get('at', 0)) < rc.QUOTE_KEYS_TTL_S]
    items.append({'k': sorted(_quote_hashes(norm)), 'r': str(route)[:140], 'at': float(at)})
    rc.write_json_atomic(p, {'entries': items[-1000:]}, mode=0o600)


def lookup_quote(quoted, cfg=None, at=None, path=None):
    """Routes (handles / group keys) whose recorded quiet line matches the quote. The quote may start with
    the bot account's name; that is dropped first, as in parse_quote."""
    q = re.sub(r'\s+', ' ', str(quoted or '')).strip()
    sender = ((cfg or {}).get('senderXHandle') or '').lstrip('@')
    for drop in ('@' + sender, sender):
        if sender and q.lower().startswith(drop.lower()):
            q = q[len(drop):].lstrip(' \u00b7-')
            break
    norm = _quote_norm(q)
    if len(norm) < 4:
        return set()
    full = hmac.new(_echo_secret(), ('qall:' + norm).encode('utf-8'), hashlib.sha256).hexdigest()[:32]
    pref = set()
    for n in QUOTE_PREFIXES:
        if len(norm) >= n:
            pref.add(hmac.new(_echo_secret(), ('q%d:' % n + norm[:n]).encode('utf-8'), hashlib.sha256).hexdigest()[:32])
    at = time.time() if at is None else at
    entries = [e for e in (rc.read_json(path or rc.QUOTE_KEYS_PATH, {}) or {}).get('entries', [])
               if isinstance(e, dict) and at - float(e.get('at', 0)) < rc.QUOTE_KEYS_TTL_S]
    exact = {e['r'] for e in entries if full in set(e.get('k') or [])}
    if exact:
        return exact
    # a truncated preview: the LONGEST shared prefix decides
    for n in sorted(QUOTE_PREFIXES, reverse=True):
        if len(norm) < n:
            continue
        h = hmac.new(_echo_secret(), ('q%d:' % n + norm[:n]).encode('utf-8'), hashlib.sha256).hexdigest()[:32]
        hits = {e['r'] for e in entries if h in set(e.get('k') or [])}
        if hits:
            return hits
    return set()
