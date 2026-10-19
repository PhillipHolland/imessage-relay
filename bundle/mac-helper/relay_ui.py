"""v1.4.6: fast, phone-visible feedback through the owner's own X tab (the browser extension).

The watcher (chat.db) and the host (per-request, under Chrome) leave small BODY-FREE action records here;
the extension polls the host for them and performs them in X Chat's web UI as the owner:
  - react  {uuid, emoji}  : put an emoji reaction on one of the OWNER'S OWN messages in the relay chat
                            (✅ sent, 👀 read, ❓ confirm?, or a mirrored iMessage tapback).
Every record holds only ids, an emoji and timestamps: never message text, names or handles.

Also here:
  - the id map: iMessage message guid (chat.db) <-> X message uuid of the owner's reply, recorded when a
    relayed reply is proven in chat.db, so an incoming tapback / read receipt finds HIS X message.
  - deferred notifications: a routine webhook that is only posted if the extension could not show the
    same thing first (notBefore), cancelled by the extension's ack.
"""
import os
import re
import time
import uuid as _uuid

import relay_common as rc

UI_DIR = os.path.join(rc.APP_DIR, 'ui')
QUEUE_DIR = os.path.join(UI_DIR, 'queue')
MAP_PATH = os.path.join(UI_DIR, 'idmap.json')
MAP_TTL_S = 7 * 86400
MAP_MAX = 2000
ACTION_TTL_S = 120            # an action nobody performed in 2 minutes is dropped (fallbacks cover it)
TAKE_LEASE_S = 15             # a taken action is re-offered if not acked within this
UI_FALLBACK_S = 10.0          # deferred routine copy of a UI-shown prompt is posted after this

UUID_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z-]{7,63}$')
REACTIONS = ('\u2705', '\U0001f440', '\u2753', '\u2764\ufe0f', '\U0001f44d', '\U0001f44e', '\U0001f602',
             '\u203c\ufe0f', '\U0001f5d1\ufe0f')
_PICT = re.compile('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u203C\u2049\u2122\u2139\u3030\u303D\u3297\u3299]')


def valid_uuid(u):
    return bool(UUID_RE.match(str(u or '')))


def clean_emoji(e):
    """Only a single short emoji string ever leaves here (never free text)."""
    s = str(e or '').strip()
    if s in REACTIONS:
        return s
    if 0 < len(s) <= 8 and _PICT.search(s) and not re.search(r'[A-Za-z0-9<>"\'\\/]', s):
        return s
    return ''


def uuid_from_key(key):
    """The extension's item key is '<conversation>|id:uuid=<uuid>'."""
    m = re.search(r'id:uuid=([0-9A-Za-z-]{8,64})$', str(key or ''))
    return m.group(1) if m else ''


# ---------- actions ----------
def enqueue_react(x_uuid, emoji, purpose='', at=None, qdir=None):
    e = clean_emoji(emoji)
    if not valid_uuid(x_uuid) or not e:
        return ''
    at = time.time() if at is None else float(at)
    rec = {'id': _uuid.uuid4().hex, 'action': 'react', 'uuid': str(x_uuid), 'emoji': e,
           'purpose': re.sub(r'[^a-z_]', '', str(purpose))[:20], 'at': at}
    rc.write_json_atomic(os.path.join(qdir or QUEUE_DIR, rec['id'] + '.json'), rec, mode=0o600)
    return rec['id']


def _load(qdir):
    out = []
    try:
        names = sorted(os.listdir(qdir))
    except OSError:
        return out
    for n in names:
        if n.endswith('.json'):
            r = rc.read_json(os.path.join(qdir, n))
            if isinstance(r, dict) and r.get('id'):
                out.append(r)
    return out


def _remove(qdir, i):
    try:
        os.remove(os.path.join(qdir, re.sub(r'[^0-9a-f]', '', str(i)) + '.json'))
    except OSError:
        pass


def take(limit=5, now=None, qdir=None):
    """Host (extension poll): actions to perform now. A taken action is leased, not deleted. An action not
    performed within ACTION_TTL_S becomes 'expired' (its fallback already covers it); records go after a day."""
    qdir = qdir or QUEUE_DIR
    now = time.time() if now is None else now
    out = []
    for r in sorted(_load(qdir), key=lambda r: float(r.get('at') or 0)):
        age = now - float(r.get('at') or 0)
        if age > 86400:
            _remove(qdir, r['id'])
            continue
        if r.get('state') in ('done', 'failed', 'expired'):
            continue
        if age > ACTION_TTL_S:
            r['state'] = 'expired'
            rc.write_json_atomic(os.path.join(qdir, r['id'] + '.json'), r, mode=0o600)
            rc.log_event('ui_action', status='expired', purpose=r.get('purpose', ''))
            continue
        if r.get('state') == 'taken' and now - float(r.get('takenAt') or 0) < TAKE_LEASE_S:
            continue
        if len(out) >= limit:
            break
        r['state'], r['takenAt'] = 'taken', now
        rc.write_json_atomic(os.path.join(qdir, r['id'] + '.json'), r, mode=0o600)
        out.append({'id': r['id'], 'action': r['action'], 'uuid': r['uuid'], 'emoji': r['emoji']})
    return out


def ack(action_id, ok, step='', now=None, qdir=None):
    qdir = qdir or QUEUE_DIR
    i = re.sub(r'[^0-9a-f]', '', str(action_id or ''))
    if not i:
        return False
    p = os.path.join(qdir, i + '.json')
    r = rc.read_json(p)
    if not isinstance(r, dict):
        return False
    r['state'] = 'done' if ok else 'failed'
    r['doneAt'] = time.time() if now is None else now
    r['step'] = re.sub(r'[^a-z_-]', '', str(step))[:30]
    rc.write_json_atomic(p, r, mode=0o600)
    rc.log_event('ui_action', status=r['state'], purpose=r.get('purpose', ''), step=r['step'],
                 latencyMs=int(max(0, r['doneAt'] - float(r.get('at') or r['doneAt'])) * 1000))
    return True


def state_of(action_id, qdir=None):
    r = rc.read_json(os.path.join(qdir or QUEUE_DIR, re.sub(r'[^0-9a-f]', '', str(action_id or '')) + '.json'))
    return (r or {}).get('state') or ('pending' if isinstance(r, dict) else 'gone')


def forget(action_id, qdir=None):
    _remove(qdir or QUEUE_DIR, action_id)


# ---------- id map (iMessage guid <-> owner's X message uuid) ----------
def _map_load(path, now):
    d = rc.read_json(path, {}) or {}
    ent = d.get('entries') if isinstance(d.get('entries'), dict) else {}
    return {k: v for k, v in ent.items() if isinstance(v, dict) and now - float(v.get('at') or 0) < MAP_TTL_S}


def map_put(msg_guid, x_uuid, rowid=None, one_to_one=True, now=None, path=None):
    g = re.sub(r'[^0-9A-Za-z-]', '', str(msg_guid or ''))[:64]
    if not g or not valid_uuid(x_uuid):
        return False
    path = path or MAP_PATH
    now = time.time() if now is None else now
    ent = _map_load(path, now)
    ent[g] = {'x': str(x_uuid), 'at': now, 'rowid': int(rowid) if rowid is not None else None,
              'dm': bool(one_to_one)}
    if len(ent) > MAP_MAX:
        ent = dict(sorted(ent.items(), key=lambda kv: kv[1]['at'])[-MAP_MAX:])
    rc.write_json_atomic(path, {'entries': ent}, mode=0o600)
    return True


def map_get(msg_guid, now=None, path=None):
    g = str(msg_guid or '')
    if '/' in g:
        g = g.rsplit('/', 1)[-1]          # "p:0/<guid>" (part 0 of the message)
    if ':' in g:
        g = g.split(':')[-1]              # "bp:<guid>"
    g = re.sub(r'[^0-9A-Za-z-]', '', g)[:64]
    if not g:
        return None
    return _map_load(path or MAP_PATH, time.time() if now is None else now).get(g)


def map_update(msg_guid, path=None, now=None, **fields):
    path = path or MAP_PATH
    now = time.time() if now is None else now
    ent = _map_load(path, now)
    if msg_guid in ent:
        ent[msg_guid].update(fields)
        rc.write_json_atomic(path, {'entries': ent}, mode=0o600)


def map_unread(path=None, now=None, max_age_s=86400):
    """1:1 sends still waiting for a read receipt (for the 👀 upgrade), newest first."""
    now = time.time() if now is None else now
    ent = _map_load(path or MAP_PATH, now)
    return [(g, v) for g, v in sorted(ent.items(), key=lambda kv: -kv[1]['at'])
            if v.get('dm') and not v.get('read') and v.get('rowid') is not None and now - v['at'] < max_age_s]
