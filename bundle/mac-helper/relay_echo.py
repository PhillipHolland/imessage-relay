"""iMessage Relay v1.4.0 -- echo guard + bot-output detection.
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
    '\u23f3',        # ⏳  catching up
    '\U0001f504',    # 🔄  resynced
    '\u2139',        # ℹ  generic status
    '\U0001f6ab',    # 🚫  muted
    '\u2705',        # ✅  unmuted
    '\u231b',        # ⌛  not sent (guess expired)
    '\U0001f4ec',    # 📬  quiet-hours digest header
    '\u2753',        # ❓  needs_confirm (directive / guessed reply) prompt
    '\U0001f9ea',    # 🧪  v1.3.8 /test report ("🧪 relay test #tag …")
)
BOT_LINE_PHRASES = (
    "Who's this for?", "Who\u2019s this for?",   # needs_target (straight and curly apostrophe)
    'What should I send to',                      # needs_target (directive named a contact but no body)
    'Who do you want to',                        # needs_target (block/unblock)
    'To stop relaying',                          # block confirmation prompt
    'Nothing to confirm',                        # stale confirmation
)
BOT_FORMAT_RE = re.compile(
    r'^\s*(?:' + '|'.join(list(BOT_LINE_SIGILS) + [re.escape(p) for p in BOT_LINE_PHRASES]) + r')', re.I)
# Item 13: the host's routing replies ("Can't send to <who>: they ...", "Group replies aren't
# supported ...") and the bare-command hint ("Did you mean /block ...") are structured, not fixed
# prefixes, so they get their own matcher. Derived from the exact wording route_reply() / the host
# post with, so the watcher never relays one of these back out as an iMessage.
BOT_STRUCTURED_RE = re.compile(
    r"^\s*(?:can[\u2019']t send to\b.*:\s*(?:they|you)\b"
    r"|group replies aren[\u2019']t supported"
    r"|did you mean\s*/)", re.I)


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
