#!/usr/bin/python3
"""v1.4.18: conversation headers on the Mac. Same algorithm as routine/relay_post.py.

The watcher is stdlib Python 3.9 and must not import the bot helper. Keep _hdr_meta, header_text,
add_headers and _message_line in lockstep with relay_post.py (tests compare them). State on disk is
only {"k": sha256("imr-hdr:"+who)[:24], "at": epoch}. Never a name and never message text.
"""
import hashlib
import json
import os
import re
import time

import relay_common as rc

GROUP_SEP = ' \u00b7 '
UNKNOWN_TAG = '\u26a0\ufe0f Unknown \u00b7 '
TAGS = {'spam': '\U0001f6ab Spam \u00b7 ', 'promotion': '\U0001f3f7\ufe0f Promo \u00b7 ',
        'transaction': '\U0001f9fe '}
HEADER_ICON = '\U0001f4ac'
HEADER_GAP_DEFAULT_MIN = 30
_PHOTO_PLACEHOLDER = re.compile(r'\U0001f4f7 (?:Photo|\d+ photos)$')


def state_path():
    return os.path.join(rc.APP_DIR, 'header_state.json')


def _label(p, default='someone'):
    return str(p.get('label') or p.get('contactName') or default).strip() or default


def _hdr_meta(m):
    """Body-free facts for a conversation header: who (group label, else handle/label), when."""
    g = m.get('group') or {}
    gl = str(g.get('label') or g.get('name') or '').strip()
    who = ('g:' + gl) if gl else ('h:' + str(m.get('handle') or _label(m)))
    try:
        import datetime as _dt
        at = _dt.datetime.fromisoformat(str(m.get('receivedAt') or '').replace('Z', '+00:00')).timestamp()
    except Exception:
        at = None
    t = re.sub(r'[^0-9: APM]', '', str(m.get('localTime') or ''))[:8].strip()
    return {'key': who, 'label': gl or _label(m), 'at': at, 'time': t, 'reaction': bool(m.get('reaction'))}


def header_text(meta):
    return HEADER_ICON + ' ' + meta['label'] + ((GROUP_SEP + meta['time']) if meta['time'] else '')


def _hkey(who):
    return hashlib.sha256(('imr-hdr:' + who).encode('utf-8')).hexdigest()[:24]


def add_headers(lines, gap_min, state, now=None):
    """Insert a silent header line before a relayed text from a different person than the previous one,
    or from the same person after gap_min minutes (0 = off). `state` is {"k": key hash, "at": epoch}
    (no names, no text); returns (lines, new_state)."""
    try:
        gap = max(0.0, float(gap_min)) * 60.0
    except (TypeError, ValueError):
        gap = HEADER_GAP_DEFAULT_MIN * 60.0
    if not gap:
        return lines, state
    now = time.time() if now is None else now
    st = dict(state or {})
    out = []
    for ln in lines:
        meta = ln.get('hdr')
        if meta and not meta['reaction']:
            k, at = _hkey(meta['key']), meta['at'] or now
            if st.get('k') != k or at - float(st.get('at') or 0) >= gap:
                out.append({'id': ln['id'] + '#hdr', 'text': header_text(meta), 'body': False, 'header': True})
            st = {'k': k, 'at': at}
        out.append(ln)
    return out, st


def _message_line(m):
    """`<label>: <text>` or `<group.label> · <label>: <text>`. A tapback joins with a space, not a colon."""
    label = _label(m)
    text = str(m.get('text') or '')
    g = m.get('group') or {}
    gl = str(g.get('label') or g.get('name') or '').strip()
    head = (gl + GROUP_SEP if gl else '') + label
    tag = TAGS.get(str(m.get('tag') or '')) if not gl else None
    if tag:
        head = tag + head
    elif m.get('unknown') and not gl:
        head = UNKNOWN_TAG + head
    if (m.get('quiet') and not gl and not m.get('reaction') and not tag and not m.get('unknown')
            and not m.get('media') and not _PHOTO_PLACEHOLDER.fullmatch(text.strip())):
        return text
    sep = ' ' if m.get('reaction') else ': '
    if not text.strip() and not m.get('reaction'):
        return head + ':'
    return head + sep + text


def plan(messages, gap_min, state, now=None):
    """One header decision for the messages the routine would see. Returns (lines, new_state)."""
    lines = []
    for i, m in enumerate(messages or []):
        mid = str(m.get('id') or ('m#%d' % i))
        lines.append({'id': mid, 'text': _message_line(m), 'body': True, 'hdr': _hdr_meta(m)})
    return add_headers(lines, gap_min, state, now=now)


def public_state(st):
    """The webhook snapshot: a key hash and a time, or {} when there is no header yet."""
    if not isinstance(st, dict) or not st.get('k'):
        return {}
    return {'k': st.get('k'), 'at': st.get('at')}


def load_state(path=None):
    path = path or state_path()
    try:
        with open(path, encoding='utf-8') as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or not d.get('k'):
        return {}
    return {'k': d.get('k'), 'at': d.get('at')}


def save_state(st, path=None):
    """Monotonic on `at`: a late older snapshot does not rewind a newer one. Mode 600. Only k and at."""
    if not isinstance(st, dict) or not st.get('k'):
        return False
    try:
        new_at = float(st.get('at') or 0)
    except (TypeError, ValueError):
        return False
    path = path or state_path()
    cur = load_state(path)
    try:
        cur_at = float(cur.get('at') or 0) if cur.get('k') else None
    except (TypeError, ValueError):
        cur_at = None
    if cur.get('k') and cur_at is not None and cur_at > new_at:
        return False
    folder = os.path.dirname(path)
    tmp = path + '.tmp'
    try:
        if folder:
            os.makedirs(folder, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump({'k': st.get('k'), 'at': st.get('at')}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
    return True
