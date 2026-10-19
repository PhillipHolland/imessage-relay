"""
iMessage Relay v1.3.9: guided-setup helpers shared by relayctl and the watcher.

  detect_owner_handles(con, cc)   the owner's OWN iMessage/SMS addresses, read from chat.db rows the
                                  owner sent (message.account / destination_caller_id). Handles only;
                                  never a message body.
  merge_owner_handles(found)      add them to config ownerHandles (and ownerHandle if empty) under the
                                  shared config lock; never removes one; skips any that is a contact.
  apply_setup(values, token)      the whole non-secret guided config in ONE locked edit (relayctl setup).

Standing defaults are never changed here: dryRun stays on, quiet hours stay null, contacts stay
opt-in. relay-all "on" is only honored once ownerXUserId and an owner handle exist; until then it is
recorded as relayAllPending and the watcher turns it on right after it detects the owner's handles.
"""
import re

import relay_common as rc

_PREFIX_RE = re.compile(r'^(?:[EP]:|tel:|mailto:)', re.I)


def _norm(raw, cc):
    h = _PREFIX_RE.sub('', str(raw or '').strip())
    h = rc.normalize_handle(h, cc) if h else ''
    return h if rc.valid_handle(h) else ''


def detect_owner_handles(con, cc='1'):
    """Distinct own addresses from is_from_me rows, most-used first. Columns differ across macOS
    versions, so only the ones present are read."""
    cols = {r[1] for r in con.execute('PRAGMA table_info(message)').fetchall()}
    counts = {}
    for col in ('account', 'destination_caller_id'):
        if col not in cols:
            continue
        rows = con.execute('SELECT %s, COUNT(*) FROM message WHERE is_from_me = 1 AND %s IS NOT NULL '
                           "AND %s != '' GROUP BY %s" % (col, col, col, col)).fetchall()
        for raw, n in rows:
            h = _norm(raw, cc)
            if h:
                counts[h] = counts.get(h, 0) + int(n or 0)
    return [h for h, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def merge_owner_handles(found, only_if_empty=False):
    """Returns {'ok', 'added', 'skippedContacts', 'ownerHandles', 'ownerHandle', 'relayAll'}."""
    out = {}

    def _do():
        raw = rc.read_raw_config() or {}
        cfg = rc.sanitize_config(dict(raw))
        cc = cfg['defaultCountryCode']
        cur = [x for x in (raw.get('ownerHandles') or []) if isinstance(x, str)]
        have = {rc.normalize_handle(x, cc) for x in cur}
        if only_if_empty and (have or raw.get('ownerHandle')):
            out.update(ok=True, added=[], skippedContacts=[], note='owner handles already set')
            return
        added, skipped = [], []
        for h in found:
            if h in have:
                continue
            if rc.contact_by_handle(cfg, h):
                skipped.append(h)            # never silently turn an opted-in contact into "the owner"
                continue
            cur.append(h)
            have.add(h)
            added.append(h)
        raw['ownerHandles'] = cur
        if not raw.get('ownerHandle') and added:
            raw['ownerHandle'] = added[0]
        if raw.get('relayAllPending') and cur and raw.get('ownerXUserId'):
            raw['relayAll'] = True
            raw.pop('relayAllPending', None)
            out['relayAllEnabled'] = True
        if added or out.get('relayAllEnabled'):
            rc.write_json_atomic(rc.CONFIG_PATH, raw)
        out.update(ok=True, added=added, skippedContacts=skipped)

    rc.with_host_lock(_do)
    cfg = rc.load_config()
    out.update(ownerHandles=sorted(rc.owner_handle_set(cfg)), ownerHandle=cfg.get('ownerHandle', ''),
               relayAll=cfg['relayAll'])
    return out


class SetupError(Exception):
    pass


def validate_setup(v):
    """Check the guided values before anything is written. Returns a cleaned dict."""
    c = {}
    for k in ('ownerXHandle', 'senderXHandle'):
        if v.get(k):
            h = str(v[k]).strip().lstrip('@')
            if not re.match(r'^[A-Za-z0-9_]{1,15}$', h):
                raise SetupError('%s should be an X handle like @name (letters, digits, _)' % k)
            c[k] = h
    if c.get('ownerXHandle') and c.get('senderXHandle') and c['ownerXHandle'].lower() == c['senderXHandle'].lower():
        raise SetupError('the sender account must be a different X account from yours')
    if v.get('ownerXUserId'):
        uid = re.sub(r'[^0-9]', '', str(v['ownerXUserId']))
        if not uid:
            raise SetupError('ownerXUserId is the numeric X user id (digits only)')
        c['ownerXUserId'] = uid
    if v.get('relayConversationId'):
        cid = str(v['relayConversationId']).strip()
        if not rc.CONV_RE.match(cid):
            raise SetupError('relayConversationId looks wrong (it is "<id>-<id>" from x.com/i/chat/<id>)')
        c['relayConversationId'] = cid
    if v.get('webhookUrl'):
        u = str(v['webhookUrl']).strip()
        if not u.startswith('https://'):
            raise SetupError('webhookUrl must start with https://')
        c['webhookUrl'] = u
    if v.get('relayAll') is not None:
        c['relayAll'] = bool(v['relayAll'])
    return c


def apply_setup(values, token=None):
    """One locked config edit. Never touches dryRun, quietHours, contacts, mute or the roster."""
    c = validate_setup(values)
    out = {}

    def _do():
        raw = rc.read_raw_config() or {}
        if not raw:
            import json
            raw = json.loads(json.dumps(rc.DEFAULT_CONFIG))
        for k in ('ownerXHandle', 'senderXHandle', 'ownerXUserId', 'relayConversationId', 'webhookUrl'):
            if k in c:
                raw[k] = c[k]
        if token:
            raw['webhookToken'] = token
        if 'relayAll' in c:
            if not c['relayAll']:
                raw['relayAll'] = False
                raw.pop('relayAllPending', None)
            else:
                cfg = rc.sanitize_config(dict(raw))
                if raw.get('ownerXUserId') and rc.owner_handle_set(cfg):
                    raw['relayAll'] = True
                    raw.pop('relayAllPending', None)
                else:
                    raw['relayAll'] = False
                    raw['relayAllPending'] = True
                    out['relayAllPending'] = True
        rc.write_json_atomic(rc.CONFIG_PATH, raw)

    rc.with_host_lock(_do)
    return out
