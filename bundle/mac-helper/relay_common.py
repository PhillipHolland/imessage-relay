"""
iMessage Relay v1.4.8: shared helpers for the native host, the inbound watcher and relayctl.

One config file: ~/.imessage-relay/config.json (see config.example.json). Every module reads
it fresh, so edits by the bot apply without restarts.

Privacy rules enforced here:
  - log_event() refuses message bodies (keys text/body/message) and writes counts/metadata only.
  - chat.db is always opened read-only (sqlite URI mode=ro).

Standard library only; written for macOS's /usr/bin/python3 (3.9+).
"""
import fcntl
import hashlib
import hmac
import json
import os
import uuid
import re
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

VERSION = '1.4.13'
EXTENSION_VERSION = '1.4.13'   # v1.4.13: pick answerable 10 min, faster ✅ polling (reload + Cmd+R)
HOME = os.path.expanduser('~')
APP_DIR = os.environ.get('IMESSAGE_RELAY_HOME', os.path.join(HOME, '.imessage-relay'))
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')
ROSTER_PATH = os.path.join(APP_DIR, 'roster.json')   # handles, labels, times only; never bodies
DIGEST_MARKER_PATH = os.path.join(APP_DIR, 'digest_marker.json')  # time of the last quiet-hours digest (item 7)
LEGACY_RECENT_PATH = os.path.join(APP_DIR, 'last_inbound.json')   # v1.1 (migrated by install.sh)
PENDING_DIR = os.path.join(APP_DIR, 'pending')   # one file per host-recorded send awaiting delivery proof; NEVER bodies
ECHO_SECRET_PATH = os.path.join(APP_DIR, 'echo_secret')   # random per-install HMAC key (0600)
HOST_LOCK_PATH = os.path.join(APP_DIR, 'host_state.lock')   # shared config/state lock (host + relayctl, item N5)


def with_host_lock(fn):
    """Run fn() while holding the shared config/state lock (fcntl flock on HOST_LOCK_PATH), so the
    native host and relayctl never clobber each other's config writes (item N5). The host's send path
    and relayctl config edits all serialize on this one lock file."""
    os.makedirs(APP_DIR, exist_ok=True)
    with open(HOST_LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return fn()
ECHO_STORE_PATH = os.path.join(APP_DIR, 'relayed_echo.json')   # HMACs of recently relayed inbound text; never bodies
QUOTE_KEYS_PATH = os.path.join(APP_DIR, 'quote_keys.json')     # v1.4.6: HMACs of unprefixed (quiet) lines -> route; never bodies
QUOTE_KEYS_TTL_S = 2 * 86400
ECHO_TTL_S = 3600
ECHO_MIN_LEN = 20   # only guard against bouncing back a substantial body; short replies ('ok') are never echoes
PENDING_TTL_S = 600   # a pending send the watcher never confirms is dropped after ~10 min
MAX_ROSTER = 2000
LOG_PATH = os.environ.get('IMESSAGE_RELAY_LOG', os.path.join(HOME, 'Library', 'Logs', 'imessage-relay.log'))
CHAT_DB = os.environ.get('IMESSAGE_RELAY_CHAT_DB', os.path.join(HOME, 'Library', 'Messages', 'chat.db'))
BIN_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(BIN_DIR, 'send_imessage.applescript')
OSASCRIPT = '/usr/bin/osascript'
OPEN = '/usr/bin/open'
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds
SELFTEST_TEXT = 'Relay is set up \u2705'

DEFAULT_CONFIG = {
    'paused': False,
    'dryRun': True,
    'quietHours': None,
    'contacts': [],
    'relayConversationId': '',
    'replyWindowMinutes': 120,
    'ambiguousSeconds': 60,
    'confirmSends': True,
    'confirmGuessed': 'risky',
    'redactCodes': True,
    'relayAll': False,
    'rosterDays': 30,
    'mute': [],
    'skipShortCodes': True,
    'batchWebhooks': True,         # v1.4.5: texts waiting together share one webhook (one routine run)
    'uiReactions': True,           # v1.4.6: \u2705 / \U0001f440 / \u2753 / tapback mirrors done by the extension in the owner's X tab
    'uiPrompts': True,             # v1.4.6: prompts shown by the extension first; the routine copy only if that fails
    'readReceipts': True,          # v1.4.6: \u2705 -> \U0001f440 when a 1:1 reply shows read (only if they share receipts)
    'mirrorTapbacks': True,        # v1.4.6: their tapback on a relayed reply -> that reaction on HIS X message
    'quietNamesSeconds': 600,      # v1.4.6: "Name:" only when the sender changes within this window (0 = always)
    'undoSeconds': 5,              # v1.4.6: \U0001f5d1\ufe0f on his own reply within this cancels it (0-10; 0 = off)
    'stripOptOut': True,           # v1.4.6: drop "Reply STOP to opt out" footers on texts tagged spam / unknown
    'relayAttachments': True,      # v1.4.6: 1:1 photos both ways (in-flight temp files only, never stored)
    'footer': '',
    'deliveryCheck': True,
    'maxSendsPerMinute': 6,
    'webhookUrl': '',
    'webhookToken': '',
    'dropUrl': '',                 # v1.4.7: encrypted photo drop (https://…); empty = photos arrive as "📷 Photo"
    'dropKey': '',                 # v1.4.7: the drop's PUT/DELETE key (never logged, never in argv)
    'dropWrapKey': '',             # v1.4.8: seals each photo's key inside the drop blob (shared with the bot only)
    'ownerHandle': '',
    'ownerXUserId': '',
    'ownerHandles': [],
    'ownerXHandle': '',
    'senderXHandle': '',
    'autoReopenMessages': False,
    'pollSeconds': 10,
    'defaultCountryCode': '1',
    'spamMode': 'follow-messages', # v1.4.6: follow-messages (mirror Messages' own filtering) | tag (v1.4.5) | off
    'confirmNewContacts': 'exact-skip',   # v1.4.1: exact-skip (an exact full name, one card, one number: send) | always
}

E164_RE = re.compile(r'^\+[1-9][0-9]{6,14}$')
EMAIL_RE = re.compile(r'^[^\s@"\\]+@[^\s@"\\]+\.[^\s@"\\]+$')
CONV_RE = re.compile(r'^[A-Za-z0-9:_.-]{3,120}$')
HHMM_RE = re.compile(r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$')
LABEL_BAD_RE = re.compile(r'[:$`"\\\x00-\x1f\x7f]')   # strip risky metachars + controls; apostrophes kept (labels are argv-only)
MAX_LABEL = 24

# v1.3.9 defense in depth: X Chat renders the bubble time as plain spans inside the message text, and
# an older extension appended it to the reply ("hi\n9:07 AM\n\n9:07 AM"). A chip line is only a clock
# time ("9:07 AM", "21:07", "9:07 p.m.") and/or a status word (Edited/Sent/Seen/Delivered). Only
# TRAILING chip lines are removed (any number, blank lines between); clock times inside the message
# are never touched, and a message that is nothing but a time keeps its first line.
_CHIP_LINE_RE = re.compile(
    r'^(?:(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?\s?m\.?)?|edited|sent|seen|delivered)'
    r'(?:\s*[\u00b7\u2022,|\-\u2013\u2014]?\s*)?)+$', re.IGNORECASE)
_SPACE_TRANS = {ord(c): ' ' for c in '\u00a0\u202f\u2009\u200a\u2007'}
_SPACE_TRANS.update({ord(c): None for c in '\u200b\u200c\u200d\u2060\u200e\u200f\ufeff'})


def _is_chip_line(line):
    v = ' '.join(str(line or '').translate(_SPACE_TRANS).split())
    return bool(v) and bool(_CHIP_LINE_RE.match(v))


def strip_trailing_chips(text):
    """Remove trailing X Chat time/status chip lines from an outgoing reply (see _CHIP_LINE_RE)."""
    if not isinstance(text, str) or '\n' not in text:
        return text
    lines = text.split('\n')
    end = len(lines)
    while end > 1 and (not lines[end - 1].strip() or _is_chip_line(lines[end - 1])):
        end -= 1
    if end == len(lines):
        return text
    kept = lines[:end]
    if not any(l.strip() and not _is_chip_line(l) for l in kept):
        return next((l for l in kept if l.strip()), text)
    return '\n'.join(kept).rstrip('\n')


# ---------- handles ----------
def normalize_handle(raw, default_cc='1'):
    """E.164 for phone numbers, lowercase for emails. Same rules as background.js:
    strip spaces/dashes/dots/parentheses; 00 -> +; 10 digits -> +<default_cc>; 11 digits
    starting with 1 -> +1...; other bare digit strings are assumed to carry a country code."""
    h = str(raw if raw is not None else '').strip()
    if not h:
        return ''
    if h.lower().startswith('mailto:'):
        h = h[7:]
    if '@' in h:
        return h.strip().lower()
    if h.lower().startswith('tel:'):
        h = h[4:]
    cc = re.sub(r'[^0-9]', '', str(default_cc or '1')) or '1'
    plus = h.startswith('+')
    d = re.sub(r'[^0-9]', '', h)
    if not d:
        return ''
    if plus:
        return '+' + d
    if d.startswith('00'):
        return '+' + d[2:]
    if len(d) == 10:
        return '+' + cc + d
    if len(d) == 11 and d[0] == '1':
        return '+' + d
    return '+' + d


def is_short_code(raw):
    """5-6 digit senders (banks, 2FA, carriers)."""
    raw = str(raw or '')
    return '@' not in raw and 5 <= len(re.sub(r'[^0-9]', '', raw)) <= 6 and not re.search(r'[A-Za-z]', raw)


def normalize_sender(raw, cc='1'):
    """normalize_handle, except 5-6 digit short codes stay bare digits (no country code)."""
    if is_short_code(raw):
        return re.sub(r'[^0-9]', '', str(raw))
    return normalize_handle(raw, cc)


def valid_handle(h):
    return bool(E164_RE.match(h or '') or EMAIL_RE.match(h or ''))


def owner_handle_set(cfg):
    """Every handle that is the owner's own (ownerHandle + ownerHandles aliases), for is-from-me skips."""
    return set(cfg.get('ownerHandles') or ([cfg['ownerHandle']] if cfg.get('ownerHandle') else []))


# ---------- config ----------
SPAM_MODES = ('follow-messages', 'tag', 'off')


def _clamp_int(v, default, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def truthy(v, default):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ('0', 'false', 'off', 'no'):
            return False
        if s in ('1', 'true', 'on', 'yes'):
            return True
    return default


def sanitize_config(user):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if not k.startswith('_')})
    cc = re.sub(r'[^0-9]', '', str(cfg.get('defaultCountryCode') or '1')) or '1'
    cfg['defaultCountryCode'] = cc
    cfg['paused'] = truthy(cfg.get('paused'), False)
    cfg['dryRun'] = truthy(cfg.get('dryRun'), True)               # stays ON unless explicitly off
    cfg['deliveryCheck'] = truthy(cfg.get('deliveryCheck'), True)
    cfg['autoReopenMessages'] = truthy(cfg.get('autoReopenMessages'), False)
    for k in ('footer', 'webhookUrl', 'webhookToken', 'ownerXHandle', 'senderXHandle', 'dropUrl', 'dropKey', 'dropWrapKey'):
        if not isinstance(cfg.get(k), str):
            cfg[k] = ''
    cfg['ownerHandle'] = normalize_handle(cfg.get('ownerHandle'), cc) if isinstance(cfg.get('ownerHandle'), str) else ''
    uid = re.sub(r'[^0-9]', '', str(cfg.get('ownerXUserId') or ''))[:30]
    cfg['ownerXUserId'] = uid
    owner_h = []
    for a in ([cfg['ownerHandle']] + (cfg.get('ownerHandles') if isinstance(cfg.get('ownerHandles'), list) else [])):
        h = normalize_handle(a, cc) if isinstance(a, str) else ''
        if valid_handle(h) and h not in owner_h:
            owner_h.append(h)
    cfg['ownerHandles'] = owner_h
    try:
        cfg['maxSendsPerMinute'] = max(1, min(30, int(cfg.get('maxSendsPerMinute'))))
    except Exception:
        cfg['maxSendsPerMinute'] = DEFAULT_CONFIG['maxSendsPerMinute']
    try:
        cfg['pollSeconds'] = max(2, min(300, int(cfg.get('pollSeconds'))))
    except Exception:
        cfg['pollSeconds'] = DEFAULT_CONFIG['pollSeconds']
    qh = cfg.get('quietHours')
    if not (isinstance(qh, dict) and HHMM_RE.match(str(qh.get('start', ''))) and HHMM_RE.match(str(qh.get('end', '')))):
        qh = None
    else:
        tz = str(qh.get('tz') or 'local')
        if tz != 'local' and _tz(tz) is None:     # a typo'd zone must not silently fall back to local time
            qh = None
        else:
            qh = {'start': qh['start'], 'end': qh['end'], 'tz': tz}
    cfg['quietHours'] = qh
    contacts = []
    seen = set()
    for c in cfg.get('contacts') if isinstance(cfg.get('contacts'), list) else []:
        if not isinstance(c, dict):
            continue
        h = normalize_handle(c.get('handle'), cc)
        if not valid_handle(h) or h in seen:
            continue
        seen.add(h)
        name = str(c.get('name') or '').strip()[:80] or h
        contacts.append({'name': name, 'handle': h, 'label': clean_label(c.get('label'))})
    assign_labels(contacts)
    cfg['contacts'] = contacts
    rel = str(cfg.get('relayConversationId') or '').strip()
    if not rel:                                   # migrate the v1.1/1.2 top-level name on load
        legacy = str(cfg.get('xConversationId') or '').strip()
        if CONV_RE.match(legacy):
            rel = legacy
    cfg['relayConversationId'] = rel if CONV_RE.match(rel) else ''
    cfg.pop('xConversationId', None)
    cfg['confirmSends'] = truthy(cfg.get('confirmSends'), True)
    cg = str(cfg.get('confirmGuessed') or 'risky').strip().lower()
    cfg['confirmGuessed'] = cg if cg in ('off', 'risky', 'always') else 'risky'
    cfg['redactCodes'] = truthy(cfg.get('redactCodes'), True)
    cfg['relayAll'] = truthy(cfg.get('relayAll'), False)
    cfg['skipShortCodes'] = truthy(cfg.get('skipShortCodes'), True)
    cfg['batchWebhooks'] = truthy(cfg.get('batchWebhooks'), True)
    for k in ('uiReactions', 'uiPrompts', 'readReceipts', 'mirrorTapbacks', 'stripOptOut', 'relayAttachments'):
        cfg[k] = truthy(cfg.get(k), True)
    cfg['quietNamesSeconds'] = _clamp_int(cfg.get('quietNamesSeconds'), 600, 0, 3600)
    cfg['undoSeconds'] = _clamp_int(cfg.get('undoSeconds'), 5, 0, 10)
    cn = str(cfg.get('confirmNewContacts') or 'exact-skip').strip().lower()   # (v1.4.2: also nickname/typo rules)
    cfg['confirmNewContacts'] = cn if cn in ('exact-skip', 'always') else 'exact-skip'
    sm = str(cfg.get('spamMode') or 'follow-messages').strip().lower()
    cfg['spamMode'] = sm if sm in SPAM_MODES else 'follow-messages'
    mute = []
    for m in cfg.get('mute') if isinstance(cfg.get('mute'), list) else []:
        if not isinstance(m, str) or not m.strip():
            continue
        if m.startswith('group:'):
            # a muted group chat is stored un-normalized as group:<chat id>; keep it verbatim (only
            # strip characters that could never appear in a chat key). Never run it through
            # normalize_sender, which would mangle it into junk like "+01" (item N4).
            h = re.sub(r'[^A-Za-z0-9;:+@._-]', '', m)[:120]
            if len(h) <= len('group:'):
                h = ''
        else:
            h = normalize_sender(m, cc)
            if not (valid_handle(h) or is_short_code(m)):   # drop junk (e.g. a previously-mangled "+01")
                h = ''
        if h and h not in mute and len(h) <= 120:
            mute.append(h)
    cfg['mute'] = mute
    for k, lo, hi in (('replyWindowMinutes', 1, 7 * 24 * 60), ('ambiguousSeconds', 0, 3600), ('rosterDays', 1, 365)):
        try:
            cfg[k] = max(lo, min(hi, int(cfg.get(k))))
        except Exception:
            cfg[k] = DEFAULT_CONFIG[k]
    return cfg


# ---------- labels: the short name shown before each relayed message ("Sam: running late") ----------
def ago(seconds):
    """A short relative-time string for owner-facing summaries ("3m", "2h", "5d", "just now"). Never
    exposes a timestamp, only coarse buckets."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return '?'
    if seconds < 45:
        return 'just now'
    m = seconds / 60.0
    if m < 60:
        return '%dm ago' % round(m)
    h = m / 60.0
    if h < 24:
        return '%dh ago' % round(h)
    return '%dd ago' % round(h / 24.0)


def mask_handle(h):
    """A privacy-safe rendering of a handle for a prompt the owner sees in X Chat: a phone number keeps
    its last 4 digits ("+1 \u2022\u2022\u20220123"), an email keeps its first char and domain
    ("k\u2022\u2022\u2022@example.com"). Never shows the full number."""
    h = str(h or '')
    if '@' in h:
        local, _, dom = h.partition('@')
        return (local[:1] + '\u2022\u2022\u2022@' + dom) if local else h
    digits = re.sub(r'[^0-9]', '', h)
    if len(digits) >= 4:
        cc = ('+' + digits[:len(digits) - 10] + ' ') if len(digits) > 10 else ''
        return cc + '\u2022\u2022\u2022' + digits[-4:]
    return h


def is_person_handle(h):
    """v1.4.3: a normal person's iMessage handle -- an email, or a phone number in plain E.164 (a +1 number
    is exactly 10 digits with a valid area code and exchange). Apple Business Chat ids (urn:biz:..., or
    over-long "+1577..." numbers), 5-6 digit short codes and anything else are NOT, so a bare reply is
    never guessed onto one (they can still be reached by quote or name)."""
    h = str(h or '').strip()
    if not h or h.lower().startswith('urn:'):
        return False
    if '@' in h:
        return bool(EMAIL_RE.match(h))
    if not E164_RE.match(h):
        return False
    if h.startswith('+1'):
        return bool(re.match(r'^\+1[2-9][0-9]{2}[2-9][0-9]{6}$', h))
    return 8 <= len(h) - 1 <= 15


def guess_label(contact):
    """v1.4.3: how a guessed recipient is named in a "Reply to ...?" prompt: the Contacts name when known,
    else the masked handle plus a warning (the owner sees at once that it is not someone they know)."""
    lab = str((contact or {}).get('label') or '')
    if _namelike(lab):
        return lab
    return mask_handle((contact or {}).get('handle') or lab) + ' (\u26a0\ufe0f not in Contacts)'


def clean_label(v):
    v = LABEL_BAD_RE.sub(' ', str(v or '')).strip()
    return re.sub(r'\s+', ' ', v)[:MAX_LABEL]


def first_name(name):
    parts = str(name or '').split()
    return parts[0] if parts else ''


def assign_labels(contacts):
    """Sets c['label'] (unique, case-insensitive) on every contact, in place.
    Explicit labels win (a duplicate explicit label falls back to automatic). Automatic: the
    first name if no other contact shares it, else first name + last initial ("Sam E."), else
    first name + last 4 digits of the handle, else the full name, else the handle."""
    used = set()
    for c in contacts:
        lab = c.get('label') or ''
        if lab and lab.lower() not in used:
            used.add(lab.lower())
            c['label'] = lab
        else:
            c['label'] = ''
    firsts = {}
    for c in contacts:
        f = first_name(c['name']).lower()
        firsts[f] = firsts.get(f, 0) + 1
    for c in contacts:
        if c['label']:
            continue
        f = clean_label(first_name(c['name'])) or c['handle']
        parts = c['name'].split()
        digits = re.sub(r'[^0-9]', '', c['handle'])
        cands = []
        if firsts.get(f.lower(), 0) == 1:
            cands.append(f)
        if len(parts) > 1:
            cands.append(clean_label('%s %s.' % (f, parts[-1][0])))
        if len(digits) >= 4:
            cands.append(clean_label('%s %s' % (f, digits[-4:])))
        cands.append(clean_label(c['name']))
        cands += [clean_label('%s %d' % (f, i)) for i in range(2, 100)]
        cands.append(c['handle'][:MAX_LABEL])
        lab = next(x for x in cands if x and x.lower() not in used)
        used.add(lab.lower())
        c['label'] = lab
    return contacts


def _namelike(label):
    """A person's name (not a phone number or email): its first word can be typed as a prefix."""
    return bool(label) and '@' not in label and not re.match(r'^[+()0-9 .\-]+$', label)


def format_handle(h):
    """Display form for a sender without a name: (555) 555-0123 for +1 numbers, else as is."""
    m = re.match(r'^\+1(\d{3})(\d{3})(\d{4})$', h or '')
    return '(%s) %s-%s' % m.groups() if m else (h or '')


def unique_label(name, handle, used, firsts=(), group=False):
    """A label for a new roster sender (or group), unique against `used` (lowercase labels).
    Same rules as contacts: first name if no one else has that first name, else "First L.",
    else "First 1234" (last 4 digits), else the full name, else "First 2..99", else the handle.
    Numbers/emails and group names: as is, else "<name> 2..99"."""
    if group:
        base = clean_label(name) or 'Group'
    else:
        base = clean_label(name) or clean_label(format_handle(handle)) or 'Unknown'
    if group or not _namelike(base):
        cands = [base] + [clean_label('%s %d' % (base, i)) for i in range(2, 100)]
    else:
        f = clean_label(first_name(base))
        parts = base.split()
        digits = re.sub(r'[^0-9]', '', handle or '')
        cands = [f] if f.lower() not in firsts else []
        if len(parts) > 1:
            cands.append(clean_label('%s %s.' % (f, parts[-1][0])))
        if len(digits) >= 4:
            cands.append(clean_label('%s %s' % (f, digits[-4:])))
        cands.append(base)
        cands += [clean_label('%s %d' % (f, i)) for i in range(2, 100)]
    cands.append(clean_label(handle))
    for c in cands:
        if c and c.lower() not in used:
            return c
    return clean_label('%s %d' % (base[:16], len(used) + 1))


def build_directory(cfg, roster=()):
    """Everyone a reply can name: contacts (kind "contact", labels from config) plus roster
    entries (kind "roster" for people, "group" for group chats) that are not contacts. Labels are
    unique (case-insensitive); a roster label that collides with a contact's is re-derived.
    Each entry: {kind, name, label, handle, lastInboundAt}."""
    out = [dict(c, kind='contact', lastInboundAt=None) for c in cfg['contacts']]
    by = {e['handle']: e for e in out}
    used = {e['label'].lower() for e in out}
    firsts = {first_name(x).lower() for e in out for x in (e['name'], e['label']) if _namelike(x)}
    for r in roster or ():
        h = r['handle']
        if h in by:
            if by[h]['lastInboundAt'] is None or (r['lastInboundAt'] or 0) > by[h]['lastInboundAt']:
                by[h]['lastInboundAt'] = r['lastInboundAt']
            continue
        group = h.startswith('group:')
        lab = r.get('label') or ''
        if not lab or lab.lower() in used:
            lab = unique_label(lab or ('Group' if group else format_handle(h)), '' if group else h, used, firsts, group)
        used.add(lab.lower())
        if not group and _namelike(lab):
            firsts.add(first_name(lab).lower())
        e = {'kind': 'group' if group else 'roster', 'name': lab, 'label': lab, 'handle': h,
             'lastInboundAt': r['lastInboundAt']}
        out.append(e)
        by[h] = e
    return out


def contact_keys(entries):
    """lowercase key -> set of handles. Keys: each label (unique, and it always wins), then for
    contacts the full name and first name, for roster people the first word of a name label.
    A first name two people share maps to both handles, so "Alex:" is reported as ambiguous
    instead of being ignored. A name that equals a label is not added (the label decides)."""
    keys = {e['label'].lower(): {e['handle']} for e in entries if e.get('label')}
    labels = set(keys)
    group_labels = {e['label'].lower() for e in entries if e.get('label') and e.get('kind') == 'group'}
    for e in entries:
        kind = e.get('kind', 'contact')
        if kind == 'contact':
            cand = {e['name'].lower(), first_name(e['name']).lower()}
        elif kind == 'roster' and _namelike(e['label']):
            cand = {first_name(e['label']).lower()}
        else:
            continue
        for k in cand:
            if k and k not in labels:
                keys.setdefault(k, set()).add(e['handle'])
            elif k and k in group_labels:
                # v1.4.5: a person's name that is also a group's name is ambiguous ("Who's this for?"),
                # never a silent pick of the group or the person.
                keys[k].add(e['handle'])
    return keys


def load_config(path=None):

    path = path or CONFIG_PATH
    try:
        with open(path, 'r', encoding='utf-8') as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except Exception as e:   # bad JSON: stay safe (dry run, no contacts)
        cfg = sanitize_config({})
        cfg['_configError'] = str(e)[:200]
        return cfg
    return sanitize_config(user)


def read_raw_config(path=None):
    try:
        with open(path or CONFIG_PATH, 'r', encoding='utf-8') as f:
            v = json.load(f)
            return v if isinstance(v, dict) else {}
    except FileNotFoundError:
        return {}


def write_json_atomic(path, obj, mode=0o600):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp.%d' % os.getpid()
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def contact_by_handle(cfg, handle):
    h = normalize_handle(handle, cfg.get('defaultCountryCode', '1'))
    return next((c for c in cfg.get('contacts', []) if c['handle'] == h), None)


# ---------- webhook event ids (v1.3.5, item B) ----------
def ensure_event_id(payload):
    """Give every outgoing webhook payload a unique, stable `id` so the routine's dedupe can catch a
    retried delivery of the SAME payload. Message/digest events already carry a chat.db-derived id and
    keep it; status/signal/sent/needs_target/refused events had none, so a retried webhook double-posted
    (item B). The id is built once per payload (the id stays the same across the poster's own HTTP
    retries, since the payload bytes do), and a new logical event gets a fresh id (uuid) so a later,
    distinct episode is never wrongly suppressed. Mutates and returns the dict."""
    if isinstance(payload, dict) and not payload.get('id'):
        kind = str(payload.get('type') or 'event')
        extra = str(payload.get('status') or '')
        payload['id'] = '%s%s:%s' % (kind, (':' + extra) if extra else '', uuid.uuid4().hex)
    return payload


def write_digest_marker(at):
    """Watcher: record when the last quiet-hours digest was posted, so the host can make the owner's
    FIRST bare reply afterward name a person (item 7). Time only, never any text."""
    try:
        write_json_atomic(DIGEST_MARKER_PATH, {'at': float(at)}, mode=0o600)
    except Exception:
        pass


def read_digest_marker():
    d = read_json(DIGEST_MARKER_PATH, {}) or {}
    try:
        return float(d.get('at') or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------- reply routing ----------
PREFIX_RE = re.compile(r'^\s*([^\n:]{1,60}?)\s*:\s*', re.S)
# v1.4.0: a "Name:" / "First Last:" prefix that names nobody we know is NOT ordinary text: it must never
# fall through to the "recent" guess (that would text "Jordan: hey" to whoever wrote last). A name-like
# prefix is 1-3 capitalized words of letters (apostrophes/hyphens/periods allowed), not a common
# "Note:"-style label word.
NAME_PREFIX_RE = re.compile(r"^[A-Z][A-Za-z\u00c0-\u024f'\u2019.-]{0,23}(?: [A-Z][A-Za-z\u00c0-\u024f'\u2019.-]{0,23}){0,2}$")
NOT_A_NAME = frozenset("""
note notes ps pps fyi re fw fwd update updates reminder warning question answer edit edited todo to-do tip
btw fwiw imo imho important heads summary tldr tl;dr p.s. ok okay yes no hi hey hello thanks thank sorry
re: subject topic idea plan step steps option options pro con pros cons day today tomorrow tonight
morning evening update: question: list item items address phone email link time date where when what why
how who which because also and but or so then just quick urgent edit: correction caption title recipe
menu agenda reason result results status total price cost answer: news alert pss
""".split())


def unknown_name_prefix(text):
    """(name, rest) when `text` starts with a name-like "Name:" prefix (see NAME_PREFIX_RE), else None.
    The caller only uses this AFTER the directory found no match."""
    m = PREFIX_RE.match(text or '')
    if not m:
        return None
    word = re.sub(r'\s+', ' ', m.group(1).strip())
    if not NAME_PREFIX_RE.match(word) or word.lower() in NOT_A_NAME or word.split(' ')[0].lower() in NOT_A_NAME:
        return None
    return word, text[m.end():]
# v1.4.2: smart name recognition. A leading "x:" / "x," / "text x ..." / "tell x ..." in ANY case is a
# CANDIDATE name; it only counts as a name when the relay list or the Mac's Contacts know it (exactly,
# by first name / nickname, or as a close typo, which always asks). Otherwise the text is ordinary.
NAME_WORD_RE = re.compile(r"^[A-Za-z\u00c0-\u024f][A-Za-z\u00c0-\u024f'\u2019.-]{0,23}$")
NOT_A_NAME_EXTRA = frozenset("""
me you him her them us it this that these those everyone everybody someone anyone all back later again
sounds good great nice cool sure yeah yep nope lol haha ha omg wow well yo k kk np ty thx pls please love
the a an my your our his their its i i'm im we ok, so, but, and, also, actually anyway honestly
""".split())
_CAND_VERB_RE = re.compile(r'^\s*(?:text|tell|message|msg)\s+(.+)$', re.I | re.S)
_CAND_COMMA_RE = re.compile(r'^\s*([^,\n]{1,60}?)\s*,\s*(\S.*)$', re.S)


def _cand_ok(words):
    if not 1 <= len(words) <= 3 or not all(NAME_WORD_RE.match(w) for w in words):
        return False
    low = [w.lower() for w in words]
    stop = NOT_A_NAME | NOT_A_NAME_EXTRA
    return ' '.join(low) not in stop and low[0] not in stop and low[-1] not in stop


def name_candidates(text):
    """v1.4.2: possible names at the start of a reply, most specific first. Each is
    {'name','body','form','fuzzy','nameLike'}: form colon|comma|verb; `fuzzy` = a close-typo match may be
    offered (only where the name's end is unambiguous); `nameLike` = the old capitalized-name shape (when
    the Contacts check cannot run, only those hold the message instead of sending it as ordinary text)."""
    t = text or ''
    out = []
    m = PREFIX_RE.match(t)
    if m:
        w = re.sub(r'\s+', ' ', m.group(1).strip())
        w = re.sub(r'^(?:text|tell|message|msg|to)\s+', '', w, flags=re.I)
        words = w.split(' ')
        if _cand_ok(words):
            out.append({'name': w, 'body': t[m.end():], 'form': 'colon', 'fuzzy': True,
                        'nameLike': bool(NAME_PREFIX_RE.match(w))})
        return out
    m = _CAND_VERB_RE.match(t)
    if m:
        rest = m.group(1)
        toks = rest.split()
        for n in (3, 2, 1):
            if len(toks) <= n:
                continue                       # the name must leave a message behind it
            words = [x.rstrip(',:.\u2013\u2014-') for x in toks[:n]]
            if not _cand_ok(words):
                continue
            body = re.sub(r'^\s*(?:\S+\s+){%d}' % n, '', rest + ' ', count=1).strip()
            body = re.sub(r'^[\s,:.\u2013\u2014-]+', '', body) if toks[n - 1][-1:] in ',:.' else body
            if body:
                out.append({'name': ' '.join(words), 'body': body, 'form': 'verb', 'fuzzy': False, 'nameLike': False})
        return out
    m = _CAND_COMMA_RE.match(t)
    if m:
        w = re.sub(r'\s+', ' ', m.group(1).strip())
        if _cand_ok(w.split(' ')):
            out.append({'name': w, 'body': m.group(2), 'form': 'comma', 'fuzzy': True, 'nameLike': False})
    return out


def close_name_score(a, b):
    """0..1 similarity of two names (case/space-insensitive); a typo-level match is >= 0.85."""
    import difflib
    a, b = ' '.join(str(a).lower().split()), ' '.join(str(b).lower().split())
    if not a or not b or a == b:
        return 1.0 if a == b and a else 0.0
    if a[0] != b[0] or abs(len(a) - len(b)) > 2 or min(len(a), len(b)) < 4:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


FUZZY_MIN = 0.85
EMAIL_WORD_RE = re.compile(r'^\S+@\S+$')


def looks_like_handle(word):
    """A prefix word is a phone/email handle only when it unambiguously looks like one: an email,
    a leading '+', a parenthesized area code, or at least 10 digits. This keeps date-like prefixes
    ('2026-10-18:', '10.18.2026:') and short local fragments ('555-0123:') from being read as
    numbers that never texted in."""
    word = str(word or '')
    if EMAIL_WORD_RE.match(word):
        return True
    digits = re.sub(r'[^0-9]', '', word)
    return word.strip().startswith('+') or '(' in word or len(digits) >= 10


def parse_prefix(text, cfg, directory=None):
    """(handle|None, rest, status) with status 'match' | 'ambiguous' | 'unknown_handle' | 'none'.
    A leading "<label or name>:" (case-insensitive) that names exactly one entry is a match and
    is stripped; one that names several is ambiguous. "Family · Alex:" (copied from a group
    line) names the group. A phone number or email that matches nobody is 'unknown_handle' (it
    never texted in, so it cannot be a recipient). Anything else ("Note: ...", "5:30",
    "https://...") is not a prefix and the text is left alone."""
    d = build_directory(cfg, []) if directory is None else directory
    m = PREFIX_RE.match(text or '')
    if not m:
        return None, text, 'none'
    word = m.group(1).strip()
    keys = contact_keys(d)
    hits = keys.get(word.lower())
    if not hits and '\u00b7' in word:
        hits = keys.get(word.split('\u00b7')[0].strip().lower())
    if not hits and looks_like_handle(word):
        h = normalize_handle(word, cfg.get('defaultCountryCode', '1'))     # "(555) 555-0123: hi"
        if any(e['handle'] == h for e in d):
            hits = {h}
        elif valid_handle(h):
            return h, text[m.end():], 'unknown_handle'
    if not hits:
        return None, text, 'none'
    if len(hits) > 1:
        return None, text, 'ambiguous'
    return next(iter(hits)), text[m.end():], 'match'


def parse_quote(quoted, cfg, directory=None):
    """Best effort: which entry a quoted (replied-to) relay message came from. The quote may
    start with the sender account's name, so the earliest "<key>:" (or "<group> ·") in its first
    120 characters counts. Returns (handle|None, status) with status 'match'|'ambiguous'|'none'."""
    d = build_directory(cfg, []) if directory is None else directory
    q = re.sub(r'\s+', ' ', str(quoted or '')).strip()[:120]
    if not q:
        return None, 'none'
    sender = (cfg.get('senderXHandle') or '').lstrip('@')
    for drop in ('@' + sender, sender):
        if sender and q.lower().startswith(drop.lower()):
            q = q[len(drop):].lstrip(' \u00b7-')
            break
    keys = contact_keys(d)
    # the bot's own send confirmation, "\u2192 <label> \u2713" (optionally "(not confirmed...)"), names its recipient
    mconf = re.match(r'^\s*\u2192\s*(.+?)\s*(?:\u2713|\(|$)', q)
    if mconf:
        hits = keys.get(mconf.group(1).strip().lower())
        if hits:
            return (None, 'ambiguous') if len(hits) > 1 else (next(iter(hits)), 'match')
    best = None
    for key, handles in keys.items():
        m = re.search(r'(?:^|[\s(\[\u00b7])(%s)\s*[:\u00b7]' % re.escape(key), q, re.I)
        if m:
            pos = m.start(1)
            if best is None or pos < best[0] or (pos == best[0] and len(key) > len(best[1])):
                best = (pos, key, handles)
    if not best:
        return None, 'none'
    if len(best[2]) > 1:
        return None, 'ambiguous'
    return next(iter(best[2])), 'match'


# ---------- roster: everyone relayed since install (handles, labels, times; never text) ----------
_ROSTER_MALFORMED_LOGGED = set()   # (path, shape) already warned about, so a bad roster warns once, not every tick


def load_roster(path=None):
    # Tolerate a malformed roster.json so a bad file can never crash a watcher tick: accept the normal
    # {"version","entries":[...]} object, convert a bare list (old/hand-edited file) by treating it as the
    # entries, and fall back to empty for anything else or for unreadable/invalid JSON. A genuine problem
    # (bad JSON, wrong shape) is logged once per (path, shape); a missing file is normal and silent.
    p = path or ROSTER_PATH
    entries, shape = [], None
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                v = json.load(f)
        except Exception:
            v = None
            shape = 'bad_json'
        if isinstance(v, dict):
            ent = v.get('entries')
            if ent is None:
                entries = []
            elif isinstance(ent, list):
                entries = ent
            else:
                shape = 'entries_not_list'
        elif isinstance(v, list):
            entries, shape = v, 'bare_list'          # convert: a bare list is treated as the entries
        elif v is not None:
            shape = 'not_object'                      # a number/string/bool/null-less value: treat as empty
    if shape:
        key = (p, shape)
        if key not in _ROSTER_MALFORMED_LOGGED:
            _ROSTER_MALFORMED_LOGGED.add(key)
            log_event('roster_malformed', shape=shape)
    out, seen = [], set()
    for r in entries:
        if not isinstance(r, dict):
            continue
        h = str(r.get('handle') or '').strip()
        if not h.startswith('group:'):
            h = normalize_handle(h)
        if not h or h in seen:
            continue
        seen.add(h)
        try:
            at = float(r['lastInboundAt']) if r.get('lastInboundAt') is not None else None
        except (TypeError, ValueError):
            at = None
        out.append({'handle': h, 'label': clean_label(r.get('label')), 'lastInboundAt': at})
    return out


def save_roster(entries, path=None):
    entries = list(entries)
    if len(entries) > MAX_ROSTER:                       # drop the longest-silent entries
        keep = set(id(r) for r in sorted(entries, key=lambda r: -(r['lastInboundAt'] or 0))[:MAX_ROSTER])
        entries = [r for r in entries if id(r) in keep]
    write_json_atomic(path or ROSTER_PATH, {'version': 1, 'entries': [
        {'handle': r['handle'], 'label': r.get('label') or '', 'lastInboundAt': r.get('lastInboundAt')} for r in entries]})


def roster_label(cfg, roster, handle, name, group=False):
    """Watcher, when a sender is seen: the label to show. Contacts keep their configured label;
    anyone else gets (and keeps) a unique roster label from `name` (macOS Contacts), else a
    formatted number or the email. Mutates `roster`; returns (label, changed)."""
    if not group:
        c = contact_by_handle(cfg, handle)
        if c:
            return c['label'], False
    entry = next((r for r in roster if r['handle'] == handle), None)
    if entry is not None and entry['label']:
        d = build_directory(cfg, roster)
        lab = next(e['label'] for e in d if e['handle'] == handle)
        if lab != entry['label']:                       # collided with a newer contact label
            entry['label'] = lab
            return lab, True
        return lab, False
    d = build_directory(cfg, [r for r in roster if r is not entry])
    used = {e['label'].lower() for e in d}
    firsts = {first_name(x).lower() for e in d if e['kind'] != 'group' for x in (e['name'], e['label']) if _namelike(x)}
    lab = unique_label(name or ('Group' if group else format_handle(handle)), '' if group else handle, used, firsts, group)
    if entry is None:
        roster.append({'handle': handle, 'label': lab, 'lastInboundAt': None})
    else:
        entry['label'] = lab
    return lab, True


def record_inbound(handle, at, path=None, label=''):
    """Watcher, after a message reached the owner: remember when this sender (or group) was
    relayed last. Handle + time only, never text."""
    path = path or ROSTER_PATH
    h = handle if str(handle).startswith('group:') else normalize_handle(handle)
    roster = load_roster(path)
    entry = next((r for r in roster if r['handle'] == h), None)
    if entry is None:
        roster.append({'handle': h, 'label': clean_label(label), 'lastInboundAt': float(at)})
    else:
        entry['lastInboundAt'] = float(at)
        if label and not entry['label']:
            entry['label'] = clean_label(label)
    save_roster(roster, path)




def example_label(cfg=None, directory=None):
    """v1.4.2: always a generic example name, never one of the owner's contacts or roster labels."""
    return 'Alex'


def ask_for_name_message(cfg, directory=None):
    # v1.3.5: show both the colon form and a conversational form so the owner knows either works.
    lbl = example_label(cfg, directory)
    return 'Who\'s this for? Start with a name, like "%s: \u2026" or "text %s \u2026".' % (lbl, lbl)


def _maybe_confirm_directive(res, target, form, name, live):
    """W1: a directive route gets by='directive'. Hold for a /yes confirmation when (a) it targets
    someone other than who a bare reply would reach AND that bare-reply person wrote within the reply
    window, or (b) it is a verb-less "Name,"/"Name -" form whose name is a common dictionary word.
    Attaches res['confirm'] = {'reason':'directive','altLabel': <bare target label or None>}; the host
    turns that into a needsConfirm. Label only -- the stripped body is never shown in the prompt."""
    import relay_directive
    alt = live[0] if live else None
    cond_a = alt is not None and alt['handle'] != target['handle']
    cond_b = form == 'bare' and str(name or '').lower() in relay_directive.DIRECTIVE_DICT_WORDS
    if cond_a or cond_b:
        res = dict(res)
        res['confirm'] = {'reason': 'directive', 'altLabel': alt['label'] if cond_a else None}
    return res


def route_reply(text, quoted, cfg, roster, now, allow_empty=False):
    """Decide who an owner reply goes to. Priority: (1) the quoted relay message, (2) a leading
    "<label or name>:" prefix (stripped before sending), (3) the most recent sender within
    replyWindowMinutes unless another wrote within ambiguousSeconds. Recipients: contacts; with
    relayAll also roster people within rosterDays. Never a group, never a muted or never-texted
    number. Returns {'contact','body','by'} or {'error','needsTarget':True,'reason'}.
    v1.4.0: allow_empty (a reply that carries attachments) accepts a name with no text after it; a
    name-like prefix that matches no one returns needsTarget with 'unknownName' and 'body' so the host
    can look the name up in the Mac's Contacts (never a guess)."""
    d = build_directory(cfg, roster or [])
    by_handle = {e['handle']: e for e in d}

    def need(reason, msg=None):
        return {'error': msg or ask_for_name_message(cfg, d), 'needsTarget': True, 'reason': reason}

    def deliver(e, body, by):
        if e['kind'] == 'group':
            # v1.4.5: a group is reached ONLY by its exact name or a quote of its message (never a bare
            # reply), only under relay-all (the same opt-in that relays it), and the host ALWAYS asks first.
            if by not in ('prefix', 'directive', 'quote'):
                return need('a bare reply never goes to a group')
            if not cfg['relayAll']:
                return need('group replies need relay-all',
                            "Can't send to the %s group: group replies need relay-all on." % e['label'])
            at = e['lastInboundAt']
            if at is None or now - at > cfg['rosterDays'] * 86400:
                return need('%s has not texted within rosterDays' % e['label'],
                            "Can't send to the %s group: it hasn't texted you in the last %d days." % (e['label'], cfg['rosterDays']))
            return {'contact': e, 'body': body, 'by': by, 'group': True}
        if e['kind'] == 'roster':
            if not cfg['relayAll']:
                return need('%s is not a contact (relayAll is off)' % e['label'],
                            "Can't send to %s: they aren't one of your relay contacts. Ask me to add them." % e['label'])
            at = e['lastInboundAt']
            if at is None or now - at > cfg['rosterDays'] * 86400:
                return need('%s has not texted within rosterDays' % e['label'],
                            "Can't send to %s: they haven't texted you in the last %d days. Ask me to add them as a contact."
                            % (e['label'], cfg['rosterDays']))
        return {'contact': e, 'body': body, 'by': by}

    muted = set(cfg.get('mute') or ())
    window = cfg['replyWindowMinutes'] * 60

    # v1.4.2: with relay-all, a roster person who just texted is a "last texter" too (a bare reply to them
    # is always confirmed first by the host); before, only configured contacts were, so a reply to a
    # roster-only sender who had just written got "Who's this for?".
    live_kinds = ('contact', 'roster') if cfg['relayAll'] else ('contact',)

    def recent_live():
        return sorted([e for e in d if e['lastInboundAt'] is not None and e['kind'] in live_kinds
                       and e['handle'] not in muted and 0 <= now - e['lastInboundAt'] <= window],
                      key=lambda e: -e['lastInboundAt'])

    q_handle, q_status = parse_quote(quoted, cfg, d)
    if q_status == 'none' and re.sub(r'\s+', '', str(quoted or '')):
        # v1.4.6 quiet names: a quoted line that carried no "Name:" (same sender as the line above it) is
        # found by keyed HMACs of its text, recorded when it was relayed. Several people -> ambiguous.
        import relay_echo
        hits = {h for h in relay_echo.lookup_quote(quoted, cfg, now) if h in by_handle}
        if len(hits) == 1:
            q_handle, q_status = next(iter(hits)), 'match'
        elif len(hits) > 1:
            q_status = 'ambiguous'
    p_handle, rest, p_status = parse_prefix(text, cfg, d)
    # Conversational addressing (v1.3.5, item A): when there is no "<Name>:" colon prefix, try the
    # directive forms ("text Sam ...", "tell Sam: ...", "Sam, ...", "@Sam ..."). A resolved directive
    # behaves exactly like a name prefix from here on (so quote/prefix conflict handling is unchanged);
    # an unresolved or prose "directive" stays 'none' and routing continues as before.
    directive_form = directive_name = None
    if p_status == 'none':
        import relay_directive
        d_handle, d_body, d_status, d_form, d_name = relay_directive.parse_directive(text, cfg, d)
        if d_status == 'empty' and allow_empty:
            d_status, d_body = 'match', ''
        if d_status == 'empty':
            e = by_handle.get(d_handle)
            return need('named a recipient but gave no message',
                        'What should I send to %s?' % (e['label'] if e else 'them'))
        if d_status == 'unknown_handle':
            # W4: a verb aimed at a phone/email that has never texted in. Refuse; never recent.
            p_handle, p_status = d_handle, 'unknown_handle'
        elif d_status == 'match':
            p_handle, rest, p_status = d_handle, d_body, 'match'
            directive_form, directive_name = d_form, d_name
        elif d_status == 'ambiguous':
            p_status = 'ambiguous'
    quoted_present = bool(re.sub(r'\s+', '', str(quoted or '')))
    if p_status == 'unknown_handle':
        return need('that number or email has never texted in',
                    "Can't send to %s: they have never texted you. Ask me to add them as a contact first."
                    % format_handle(p_handle))
    if q_status == 'match':
        if p_status == 'match' and p_handle != q_handle:
            return need('the quoted message and the name prefix point to different people')
        if p_status == 'ambiguous':
            return need('the name prefix matches more than one person')
        return deliver(by_handle[q_handle], rest if p_status == 'match' else text, 'quote')
    if q_status == 'ambiguous':
        return need('the quoted message matches more than one person')
    if p_status == 'ambiguous':
        return need('the name prefix matches more than one person')
    if p_status == 'match':
        if directive_form:
            res = deliver(by_handle[p_handle], rest, 'directive')
            if 'contact' in res:
                res = _maybe_confirm_directive(res, by_handle[p_handle], directive_form, directive_name, recent_live())
            return res
        return deliver(by_handle[p_handle], rest, 'prefix')
    # v1.4.2: a quote we cannot resolve (X changed its reply preview, or the label is gone) falls back to
    # whoever wrote last, but is ALWAYS confirmed first ("Reply to Rory? Reply yes within 2 min.").
    quote_unresolved = quoted_present
    def recent_route():
        # The "recent" fallback: configured contacts, plus (v1.4.2, relay-all) roster people within the window.
        live = recent_live()
        # v1.4.4: if a GROUP wrote most recently, a bare reply is never guessed onto someone older (and
        # never onto the group): the owner was most likely answering the group. Ask for a name.
        grp = [e for e in d if e['kind'] == 'group' and e['lastInboundAt'] is not None and e['handle'] not in muted
               and 0 <= now - e['lastInboundAt'] <= window]
        if grp and (not live or max(g['lastInboundAt'] for g in grp) >= live[0]['lastInboundAt']):
            return need('a group wrote last (group replies are not supported)',
                        'Who\'s this for? Start with a name, like "%s: \u2026" (group replies aren\'t supported yet)'
                        % example_label(cfg, d))
        if not live:
            return need('no one has written within %d minutes' % cfg['replyWindowMinutes'])
        if len(live) > 1 and cfg['ambiguousSeconds'] and live[0]['lastInboundAt'] - live[1]['lastInboundAt'] < cfg['ambiguousSeconds']:
            return need('two people wrote within %d seconds of each other' % cfg['ambiguousSeconds'])
        if not is_person_handle(live[0]['handle']):
            # v1.4.3: the last texter is a business / short code / odd handle: never guessed; ask for a name
            return need('the last texter is a business or short-code handle',
                        'Who\'s this for? Start with a name, like "%s: \u2026"' % example_label(cfg, d))
        return deliver(live[0], text, 'recent')

    cands = name_candidates(text) if p_status == 'none' and not quote_unresolved else []
    if cands:
        # v1.4.2: a possible name nobody on the relay list has (any case). The host asks the Mac's Contacts;
        # only a match makes it a name. No match -> `fallback` (ordinary text, routed as if no name).
        c0 = cands[0]
        res = need('the name prefix matches no one on the relay list',
                   "%s isn't on your relay list yet. Start with a name from it, or /add %s <number>."
                   % (c0['name'], first_name(c0['name'])))
        res.update(unknownName=c0['name'], body=c0['body'], candidates=cands, fallback=recent_route())
        # a close typo of someone on the relay list ("Jordn:" for Jordan): offered, never sent unasked
        ok_kinds = ('contact', 'roster') if cfg['relayAll'] else ('contact',)
        for c in cands:
            if not c['fuzzy']:
                continue
            hits = {}
            for e in d:
                if e['kind'] not in ok_kinds or e['handle'] in muted:
                    continue
                sc = max(close_name_score(c['name'], k) for k in (e['label'], e['name'], first_name(e['name'])) if k)
                if sc >= FUZZY_MIN:
                    hits[e['handle']] = e
            if len(hits) == 1:
                e = next(iter(hits.values()))
                res['dirFuzzy'] = {'handle': e['handle'], 'label': e['label'], 'name': c['name'], 'body': c['body']}
                break
        return res
    res = recent_route()
    if quote_unresolved and 'contact' in res:
        if not _namelike(res['contact'].get('label')):
            # v1.4.4: a quote we could not place never falls back to someone who is not in Contacts
            # (e.g. a spam sender who wrote last): ask for a name instead of offering them.
            return need('a quote could not be placed and the last texter is not in Contacts',
                        'Who\'s this for? Start with a name, like "%s: \u2026"' % example_label(cfg, d))
        res['quoteUnresolved'] = True
    return res


# ---------- webhook ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None     # a 3xx is a failure, not a hop; never silently POST the body to a redirected host


_OPENER = urllib.request.build_opener(_NoRedirect)


def post_json(url, payload, token='', timeout=15):
    """POST JSON; True only on 2xx (https or http://localhost). Redirects not followed; any 3xx+ is failure."""
    if not (url.startswith('https://') or url.startswith('http://127.0.0.1') or url.startswith('http://localhost')):
        return False
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'imessage-relay/' + VERSION})
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


# ---------- quiet hours ----------
def _tz(name):
    if not name or name == 'local':
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def local_now(qh, now=None):
    """`now` may be an aware datetime or a unix timestamp; returns an aware datetime in qh.tz."""
    if now is None:
        now = time.time()
    if isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, timezone.utc)
    tz = _tz((qh or {}).get('tz'))
    return now.astimezone(tz) if tz else now.astimezone()


def in_quiet_hours(qh, now=None):
    """True when `now` falls in [start, end). Wraps past midnight (22:00-07:00).
    start == end or no quietHours = never quiet."""
    if not qh:
        return False
    try:
        sh, sm = [int(x) for x in qh['start'].split(':')]
        eh, em = [int(x) for x in qh['end'].split(':')]
    except Exception:
        return False
    s, e = sh * 60 + sm, eh * 60 + em
    if s == e:
        return False
    t = local_now(qh, now)
    m = t.hour * 60 + t.minute
    return s <= m < e if s < e else (m >= s or m < e)


# ---------- logging (counts + routing method only: never bodies, handles, names or labels) ----------
FORBIDDEN_LOG_KEYS = ('text', 'body', 'message', 'outgoing')   # raise: a body must never be logged
SCRUB_LOG_KEYS = ('handle', 'label', 'name', 'contact', 'contactName', 'detail', 'hint')  # dropped (item 7)


PROBE_MAX = 4000
_PROBE_BAD_RE = re.compile(r'[^A-Za-z0-9#@\[\]{},.:;<>|_\-!+=/ ()]')


def clean_probe(fp):
    """v1.4.3: the extension's text-free quote fingerprint, re-sanitized on the Mac before it is logged:
    a closed character set (no quotes, no emoji, no free text), uuids and long digit runs masked, capped."""
    fp = _PROBE_BAD_RE.sub('', str(fp or ''))
    fp = re.sub(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', '<uuid>', fp)
    fp = re.sub(r'[0-9]{4,}', '<n>', fp)
    return fp[:PROBE_MAX]


def log_event(event, **fields):
    for k in FORBIDDEN_LOG_KEYS:
        if k in fields:
            raise ValueError('never log message bodies (%s)' % k)
    for k in SCRUB_LOG_KEYS:
        fields.pop(k, None)
    line = {'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'event': event}
    line.update(fields)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
    except Exception:
        pass


def watcher_chat_db_state(log_path=None, tail_bytes=262144):
    """Can the watcher (running inside the wrapper app) read chat.db? Decided by the newest of
    its log events: watch_baseline / chat_db_ok = yes, chat_db_unavailable = no (Full Disk Access
    missing for the app). Returns {'state': 'ok'|'denied'|'unknown', 'event', 'at'}."""
    good, bad = ('watch_baseline', 'chat_db_ok'), ('chat_db_unavailable',)
    try:
        with open(log_path or LOG_PATH, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return {'state': 'unknown', 'event': None, 'at': None}
    for line in reversed(lines):
        if '"event"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        name = ev.get('event')
        if name in good or name in bad:
            return {'state': 'ok' if name in good else 'denied', 'event': name, 'at': ev.get('ts')}
    return {'state': 'unknown', 'event': None, 'at': None}


# ---------- chat.db ----------
class ChatDbUnavailable(Exception):
    """chat.db missing or unreadable for an AUTHORIZATION reason (usually: Full Disk Access not
    granted). This is the only chat.db error that should ever raise the 'fda_missing' notice."""


class ChatDbBusy(Exception):
    """chat.db exists and is authorized, but is momentarily locked / busy (Messages is writing, or
    another reader holds an EXCLUSIVE lock). Transient: skip this tick and retry next time. This must
    NEVER be reported as fda_missing (item 11)."""


# sqlite "database is locked" / "database is busy" are transient contention, NOT a permission problem.
_DB_BUSY_RE = re.compile(r'\b(locked|busy)\b', re.I)


def open_chat_db(path=None):
    path = path or CHAT_DB
    if not os.path.exists(path):
        # Without FDA, os.path.exists can still be True; a missing file means no Messages data.
        raise ChatDbUnavailable('chat.db not found')
    try:
        con = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5)
        con.execute('SELECT 1 FROM message LIMIT 1')
        return con
    except sqlite3.OperationalError as e:
        # Distinguish a transient lock/busy (retry next tick) from a real authorization/open failure
        # (Full Disk Access). Only the latter is fda_missing (item 11).
        if _DB_BUSY_RE.search(str(e)):
            raise ChatDbBusy(str(e)[:200])
        raise ChatDbUnavailable(str(e)[:200])
    except sqlite3.Error as e:
        raise ChatDbUnavailable(str(e)[:200])


def apple_date_to_unix(v):
    """chat.db dates: ns since 2001 (modern) or seconds since 2001 (old macOS)."""
    try:
        v = int(v or 0)
    except Exception:
        return 0.0
    if v > 10 ** 12:
        v = v / 1e9
    return APPLE_EPOCH + v


def unix_to_apple_ns(t):
    return int((t - APPLE_EPOCH) * 1e9)


def iso_utc(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


# ---------- delivery proof + Messages.app send ----------
# Split into relay_send.py to keep this file under 1000 lines; re-exported lazily so callers keep
# using rc.send_imessage, rc.check_delivery_once, rc.DELIVERY_SQL, etc. unchanged.
_SEND_NAMES = ('DELIVERY_SQL', 'add_pending_send', 'load_pending', 'remove_pending', 'update_pending',
               'check_delivery_once', 'OSASCRIPT_ERRORS', 'classify_osascript_error', 'error_message',
               'build_osascript_cmd', 'send_imessage', 'messages_running', 'open_messages_hidden')


_DIRECTIVE_NAMES = ('parse_directive', 'DIRECTIVE_DICT_WORDS')
_ECHO_NAMES = ('record_quote_keys', 'lookup_quote', 'is_echoed_inbound', 'looks_like_bot_output', 'record_relayed_text', 'echo_hmac',
               '_echo_secret', '_echo_norm', 'BOT_LINE_SIGILS', 'BOT_LINE_PHRASES', 'BOT_FORMAT_RE',
               'BOT_STRUCTURED_RE')


def __getattr__(name):
    if name in _SEND_NAMES:
        import relay_send
        return getattr(relay_send, name)
    if name in _DIRECTIVE_NAMES:
        import relay_directive
        return getattr(relay_directive, name)
    if name in _ECHO_NAMES:
        import relay_echo
        return getattr(relay_echo, name)
    raise AttributeError("module 'relay_common' has no attribute %r" % name)
