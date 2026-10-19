#!/usr/bin/python3
"""
com.imessage_relay.host: Chrome native messaging host for iMessage Relay v1.4.2.

Chrome starts this process and talks to it over stdin/stdout: a 4-byte length (native byte
order, little-endian on Macs) followed by UTF-8 JSON. Requests:

  {"type":"get_config"}  -> {"ok":true,"version","paused","dryRun","relayConversationId",
                             "contacts":[{name,label,handle}],"ownerXHandle","senderXHandle","pendingSelfTest"}
  {"type":"send","text","quotedText"?,"conversationId","ts"}
                         -> {"ok":bool,"dryRun":bool,"contact":label,"routedBy":"quote"|"prefix"|"recent",
                             "sent"?,"delivered"?,"skipped"?,"error"?,"needsTarget"?}
  {"type":"selftest","id"} -> runs a self-test requested by `relayctl selftest`
  {"type":"ping"}        -> {"ok":true,"version"}

Safety (all re-checked here; the extension is not trusted for any of it):
  - Single source of truth: ~/.imessage-relay/config.json. Only replies in relayConversationId
    count. The recipient is routed here (relay_common.route_reply): the quoted relay message,
    else a leading "<label or name>:", else whoever was relayed last (roster.json, written
    by the watcher) within replyWindowMinutes. With relayAll, roster senders (roster.json: anyone who
    texted in, relayed within rosterDays) are recipients too; groups and numbers that never
    texted in never are. Unclear -> nothing is sent; the answer and a
    body-free "needs_target" webhook event ask the owner to start with a name.
  - confirmSends: after a send (or dry run) a body-free {"type":"sent","label"} event goes to
    the bot's webhook so it can post "-> <label> ✓" in the relay chat.
  - paused -> refuse. dryRun (default ON) -> log only, never call osascript.
  - Owner replies ignore quiet hours (quiet hours only hold INBOUND forwarding).
  - maxSendsPerMinute sliding window; same handle+text within 60 s dropped.
  - 1:1 sends only, text/handle passed to osascript as argv (never spliced into a script).
  - The log records handle and text LENGTH only, never the body.
"""
import fcntl
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_common as rc  # noqa: E402
import relay_test  # noqa: E402  (v1.3.8: the /test end-to-end check)
import uuid  # noqa: E402

STATE_PATH = os.path.join(rc.APP_DIR, 'host_state.json')
LOCK_PATH = os.path.join(rc.APP_DIR, 'host_state.lock')
SELFTEST_DIR = os.path.join(rc.APP_DIR, 'selftest')
DEDUPE_WINDOW_S = 60.0
MAX_TEXT = 4000
MAX_MSG_BYTES = 1024 * 1024
SELFTEST_MAX_AGE_S = 15 * 60
NOTIFY_TIMEOUT_S = 12            # v1.4.0: was 4 s; the webhook trigger often takes ~4 s to answer
NOTIFY_RETRY_DIR = os.path.join(rc.APP_DIR, 'notify_retry')   # body-free events the watcher re-posts
NEW_CONTACT_WINDOW_S = 130.0     # the /yes window for texting someone new from Contacts (2 min + slack)
CONTACTS_WAIT_S = 8.0
CONTACTS_WAIT_QUICK_S = 3.0      # v1.4.2: a lower-case / comma / "text x" candidate waits less (often ordinary text)


# ---------- native messaging framing ----------
def read_message(stream):
    raw_len = stream.read(4)
    if not raw_len:
        return None
    if len(raw_len) < 4:
        raise ValueError('truncated length prefix')
    (length,) = struct.unpack('=I', raw_len)
    if length > MAX_MSG_BYTES:
        raise ValueError('message too large: %d' % length)
    data = stream.read(length)
    if len(data) < length:
        raise ValueError('truncated message body')
    return json.loads(data.decode('utf-8'))


def encode_message(obj):
    data = json.dumps(obj, separators=(',', ':')).encode('utf-8')
    return struct.pack('=I', len(data)) + data


def write_message(stream, obj):
    stream.write(encode_message(obj))
    stream.flush()


# ---------- state ----------
def load_state():
    s = rc.read_json(STATE_PATH, {})
    return s if isinstance(s, dict) else {}


def _ack_digest(t):
    """Clear the item-7 post-digest gate: the owner has now replied once since the last digest."""
    os.makedirs(rc.APP_DIR, exist_ok=True)
    with open(LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = load_state()
        st['digestAckAt'] = float(t)
        rc.write_json_atomic(STATE_PATH, st)


def fingerprint(handle, text):
    # Per-install keyed HMAC (item 15): a stored send fingerprint reveals nothing about the text and
    # cannot be precomputed across installs.
    return hmac.new(rc._echo_secret(), (handle + '\x00' + text).encode('utf-8'), hashlib.sha256).hexdigest()


# ---------- self-test bookkeeping (files written by relayctl) ----------
def pending_selftest(now=time.time):
    req = rc.read_json(os.path.join(SELFTEST_DIR, 'request.json'))
    if not isinstance(req, dict) or not req.get('id'):
        return None
    if now() - float(req.get('createdAt') or 0) > SELFTEST_MAX_AGE_S:
        return None
    res = rc.read_json(os.path.join(SELFTEST_DIR, 'result.json'))
    if isinstance(res, dict) and res.get('id') == req['id']:
        return None
    return {'id': str(req['id'])}


# ---------- handlers ----------
EXT_SEEN_PATH = os.path.join(rc.APP_DIR, 'ext_seen.json')


def note_extension(msg, now=time.time):
    """v1.3.8: remember when the extension last connected and its version (relayctl status/setup show
    a stale, not-reloaded extension). Throttled to one small write per 5 minutes or version change."""
    v = re.sub(r'[^0-9A-Za-z.\-]', '', str(msg.get('extVersion') or ''))[:20]
    if not v:
        return
    cur = rc.read_json(EXT_SEEN_PATH, {}) or {}
    if cur.get('extVersion') == v and now() - float(cur.get('at') or 0) < 300:
        return
    try:
        rc.write_json_atomic(EXT_SEEN_PATH, {'extVersion': v, 'at': now()})
    except OSError:
        pass


def handle_get_config(cfg, now=time.time):
    return {
        'ok': True, 'version': rc.VERSION, 'paused': cfg['paused'], 'dryRun': cfg['dryRun'],
        'relayConversationId': cfg['relayConversationId'], 'relayAll': cfg['relayAll'],
        'contacts': [{'name': c['name'], 'label': c['label'], 'handle': c['handle']} for c in cfg['contacts']],
        'ownerXHandle': cfg.get('ownerXHandle', ''), 'ownerXUserId': cfg.get('ownerXUserId', ''),
        'senderXHandle': cfg.get('senderXHandle', ''),
        'pendingSelfTest': pending_selftest(now),
        'configError': cfg.get('_configError', ''),
    }


def notify(cfg, poster, payload):
    """Body-free status event to the bot's webhook (sent confirmations, "who is this for?")."""
    if not cfg.get('webhookUrl'):
        return False
    rc.ensure_event_id(payload)   # every status event gets a unique id so a retried webhook dedupes (item B)
    ok = bool(poster(cfg['webhookUrl'], payload, cfg.get('webhookToken', ''), timeout=NOTIFY_TIMEOUT_S))
    queued = False
    if not ok:
        # v1.4.0: a slow or failed webhook used to drop the owner's "Who's this for?" (the host gave up
        # after 4 s). Queue the body-free event; the watcher re-posts it with the SAME id (so the routine
        # dedupes a POST that did land) for up to 10 minutes.
        try:
            rc.write_json_atomic(os.path.join(NOTIFY_RETRY_DIR, uuid.uuid4().hex + '.json'),
                                 {'queuedAt': time.time(), 'payload': payload}, mode=0o600)
            queued = True
        except OSError:
            pass
    rc.log_event('notify', type=payload.get('type'), ok=ok, **({'queuedRetry': True} if queued else {}))
    return ok


# ---------- explicit block / mute commands (item 5) ----------
# Muting is a control action, never triggered by ordinary prose. A command MUST be an explicit
# slash command ("/block", "/mute <name>", "/unblock <name>") or carry a "relay:" prefix
# ("relay: block"). Muting also takes a confirmation: "/block" asks, and "/block yes" within
# CONFIRM_WINDOW_S actually mutes. This way "mute dad", "Mute the TV", "Block party?", bare "block",
# and "Taylor: block" are all relayed as normal messages and never silence anyone.
CONFIRM_WINDOW_S = 120.0
_CMD_RE = re.compile(r'^/(block|unblock|mute|unmute)\b\s*(.*)$', re.I | re.S)
_CMD_RELAY_RE = re.compile(r'^relay:\s*/?(block|unblock|mute|unmute)\b\s*(.*)$', re.I | re.S)


def parse_command(text):
    """Return {'action':'block'|'unblock','target':str,'confirm':bool} for an EXPLICIT command, else
    None. Only a leading '/verb' or 'relay: verb' counts; ordinary sentences never match."""
    s = (text or '').strip()
    if not s:
        return None
    m = _CMD_RE.match(s) or _CMD_RELAY_RE.match(s)
    if not m:
        return None
    verb = m.group(1).lower()
    rest = (m.group(2) or '').strip()
    action = 'unblock' if verb in ('unblock', 'unmute') else 'block'
    confirm = False
    low = rest.lower()
    if low == 'yes':
        confirm, rest = True, ''
    elif low.endswith(' yes'):
        confirm, rest = True, rest[:-4].strip()
    return {'action': action, 'target': rest, 'confirm': confirm}


_BARE_BLOCK_RE = re.compile(r'^(block|unblock)(?:\s+(.+))?$', re.I)


def bare_command_hint(text, cfg):
    """A whole message that is exactly "block"/"unblock" (optionally followed by a KNOWN recipient's
    name) is almost certainly a control attempt with the slash missing, not a text to send. Return the
    verb so the caller can nudge the owner and send NOTHING; the bot's own blocked/unblocked lines say
    'Say "/unblock <name>"', so a nervous owner retyping "unblock Sam" must not text "unblock Sam" to
    Sam (item N1). Ordinary prose ("Block party tonight?", "mute dad", "unblock the drain") returns
    None and is relayed. Only block/unblock qualify (never mute/unmute), so "mute"/"Mute the TV" send."""
    s = (text or '').strip()
    if not s or '\n' in s:
        return None
    m = _BARE_BLOCK_RE.match(s)
    if not m:
        return None
    verb = m.group(1).lower()
    name = (m.group(2) or '').strip()
    if not name:
        return verb                              # bare "block" / "unblock"
    d = rc.build_directory(cfg, rc.load_roster())
    _h, _rest, status = rc.parse_prefix(name + ':', cfg, d)
    return verb if status == 'match' else None   # "block Sam" (known contact) -> hint; "block Robin" (unknown) -> send


def resolve_mute_target(arg, quoted, cfg, now):
    """Who a block/unblock applies to, using the normal routing precedence but WITHOUT the relayAll /
    rosterDays gating (you may mute anyone who texted in). Returns {'handle','label'} or
    {'needsTarget':True,'reason'}."""
    d = rc.build_directory(cfg, rc.load_roster())
    by = {e['handle']: e for e in d}
    if arg:
        h, _rest, status = rc.parse_prefix(arg + ':', cfg, d)
        if status == 'match':
            return {'handle': h, 'label': by[h]['label']}
        if status == 'ambiguous':
            return {'needsTarget': True, 'reason': 'the name matches more than one person'}
        if status == 'unknown_handle':
            return {'handle': h, 'label': rc.format_handle(h)}
        hh = rc.normalize_sender(arg, cfg.get('defaultCountryCode', '1'))
        if rc.valid_handle(hh):
            e = by.get(hh)
            return {'handle': hh, 'label': e['label'] if e else rc.format_handle(hh)}
        return {'needsTarget': True, 'reason': 'no one by that name has texted in'}
    qh, qs = rc.parse_quote(quoted, cfg, d)
    if qs == 'match':
        return {'handle': qh, 'label': by[qh]['label']}
    if qs == 'ambiguous':
        return {'needsTarget': True, 'reason': 'the quoted message matches more than one person'}
    if re.sub(r'\s+', '', str(quoted or '')):
        return {'needsTarget': True, 'reason': 'could not tell which relayed message was quoted'}
    live = sorted([e for e in d if e['kind'] in ('contact', 'roster') and e['lastInboundAt'] is not None],
                  key=lambda e: -e['lastInboundAt'])
    if not live:
        return {'needsTarget': True, 'reason': 'no recent sender to act on'}
    return {'handle': live[0]['handle'], 'label': live[0]['label']}


def _with_host_lock(fn):
    """Run fn() while holding the shared config/state lock, so config and host_state writes are atomic
    against a concurrent host process AND against relayctl config edits (item N5). Delegates to the
    shared rc.with_host_lock so every writer serializes on the one lock file."""
    return rc.with_host_lock(fn)


def toggle_mute(handle, on):
    """Add/remove a handle in config.mute under the host lock, preserving every other key verbatim."""
    def fn():
        raw = rc.read_raw_config() or {}
        cc = str(raw.get('defaultCountryCode') or '1')

        def norm(x):
            return x if str(x).startswith('group:') else rc.normalize_sender(x, cc)
        cur = [x for x in (raw.get('mute') or []) if isinstance(x, str) and x and norm(x) != handle]
        if on:
            cur.append(handle)
        raw['mute'] = cur
        rc.write_json_atomic(rc.CONFIG_PATH, raw)
        return cur
    return _with_host_lock(fn)


def _store_pending_block(handle, label, at):
    def fn():
        state = load_state()
        state['pendingBlock'] = {'handle': handle, 'label': label, 'at': float(at)}
        rc.write_json_atomic(STATE_PATH, state)
    _with_host_lock(fn)


def _take_pending_block(now_t):
    """Pop a pending block if it is fresh enough to confirm; returns the record or None."""
    def fn():
        state = load_state()
        pend = state.get('pendingBlock')
        state.pop('pendingBlock', None)
        rc.write_json_atomic(STATE_PATH, state)
        if isinstance(pend, dict) and pend.get('handle') and now_t - float(pend.get('at') or 0) <= CONFIRM_WINDOW_S:
            return pend
        return None
    return _with_host_lock(fn)


def handle_command(cmd, quoted, cfg, now, poster):
    base = {'dryRun': cfg['dryRun']}
    action = cmd['action']
    if action == 'block':
        if cmd['confirm']:
            pend = _take_pending_block(now())
            if not pend:
                detail = 'Nothing to confirm \u2014 send "/block <name>" first (confirmations expire after 2 minutes).'
                return dict(base, ok=False, error=detail)
            if cmd['target']:
                # "/block Bob yes" must confirm only the staged target, never a different one (item N2).
                tgt = resolve_mute_target(cmd['target'], '', cfg, now())
                if tgt.get('handle') and tgt['handle'] != pend['handle']:
                    _store_pending_block(pend['handle'], pend['label'], pend['at'])   # keep the staged one
                    detail = ('That name doesn\u2019t match the block waiting on %s. Reply yes to '
                              'mute %s, or "/block %s" to start over.' % (pend['label'], pend['label'], tgt['label']))
                    return dict(base, ok=False, needsConfirm=True, label=pend['label'], error=detail)
            if not cfg['dryRun']:
                toggle_mute(pend['handle'], True)      # dry run previews only; never mutates config
            rc.log_event('muted', routedBy='command', dryRun=cfg['dryRun'])
            notify(cfg, poster, {'type': 'blocked', 'label': pend['label'], 'dryRun': cfg['dryRun'], 'at': rc.iso_utc(now())})
            if cfg['dryRun']:
                return dict(base, ok=True, muted=False, label=pend['label'], action='would_block')
            return dict(base, ok=True, muted=True, label=pend['label'], action='blocked')
        tgt = resolve_mute_target(cmd['target'], quoted, cfg, now())
        if tgt.get('needsTarget'):
            detail = 'Who do you want to block? Reply to their message, or say "/block <name>".'
            rc.log_event('needs_target', reason=tgt['reason'], action='block')
            notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': tgt['reason'], 'at': rc.iso_utc(now())})
            return dict(base, ok=False, needsTarget=True, error=detail, reason=tgt['reason'])
        _store_pending_block(tgt['handle'], tgt['label'], now())
        _note_prompt(now())
        detail = 'To stop relaying %s, reply yes within 2 min (no to cancel).' % tgt['label']
        notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'confirm-block', 'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsConfirm=True, label=tgt['label'], error=detail)
    # unblock: safe, no confirmation
    tgt = resolve_mute_target(cmd['target'], quoted, cfg, now())
    if tgt.get('needsTarget'):
        detail = 'Who do you want to unblock? Say "/unblock <name>".'
        rc.log_event('needs_target', reason=tgt['reason'], action='unblock')
        notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': tgt['reason'], 'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsTarget=True, error=detail, reason=tgt['reason'])
    if not cfg['dryRun']:
        toggle_mute(tgt['handle'], False)              # dry run previews only; never mutates config
    rc.log_event('unmuted', routedBy='command', dryRun=cfg['dryRun'])
    notify(cfg, poster, {'type': 'unblocked', 'label': tgt['label'], 'at': rc.iso_utc(now())})
    return dict(base, ok=True, muted=False, label=tgt['label'], action='unblocked')


# ---------- owner commands (item 2): /who /pause /resume /help /add /test, run even while paused ----------
# These act only in the relay conversation, which is the owner's own 1:1 with the sender account, so
# reaching here already means the owner typed them. They are parsed BEFORE the paused check so the owner
# can /resume. None of them ever sends an iMessage, so they are safe while paused.
HELP_TEXT = ('Commands: /who (who a bare reply goes to) \u00b7 /pause and /resume relaying \u00b7 '
             '/add <Name> (save the last unknown number as a contact) \u00b7 /block and /unblock <Name> '
             '\u00b7 /test (check every step; /test send also texts your own number) '
             '\u00b7 /help. To text someone, start with their name: "Sam: \u2026" or "text Sam \u2026".')
_OWNER_CMD_RE = re.compile(r'^/(who|pause|resume|help|add|test)\b\s*(.*)$', re.I | re.S)
ADD_WINDOW_S = 120.0


def parse_owner_command(text):
    """Return {'cmd',...} for /who /pause /resume /help /add, else None. /add takes a name and an
    optional 'yes' confirmation ("/add Kent", then "/add yes")."""
    s = (text or '').strip()
    m = _OWNER_CMD_RE.match(s)
    if not m:
        return None
    cmd = m.group(1).lower()
    rest = (m.group(2) or '').strip()
    if cmd == 'add':
        confirm = False
        if rest.lower() == 'yes':
            confirm, rest = True, ''
        elif rest.lower().endswith(' yes'):
            confirm, rest = True, rest[:-4].strip()
        m = re.match(r'^(.*?)\s+(\+?[0-9][0-9().\s-]{5,}[0-9]|[^\s@]+@[^\s@]+\.[^\s@]+)$', rest)
        if m and m.group(1).strip():
            # v1.4.0: "/add Jordan +1 555 555 0123" adds that person directly (no text is sent).
            return {'cmd': 'add', 'name': m.group(1).strip()[:80], 'handle': m.group(2).strip(), 'confirm': False}
        return {'cmd': 'add', 'name': rest[:80], 'confirm': confirm}
    if cmd == 'test':                         # "/test" or "/test send" (own handle only); never routed
        return {'cmd': 'test', 'send': rest.lower() == 'send'}
    return {'cmd': cmd}


def set_paused(on):
    def fn():
        raw = rc.read_raw_config() or {}
        raw['paused'] = bool(on)
        rc.write_json_atomic(rc.CONFIG_PATH, raw)
    _with_host_lock(fn)


def who_summary(cfg, now_t):
    """A body-free summary for /who: who a bare (unprefixed) reply would go to right now, plus the most
    recent senders by label and relative time. Never includes any message text."""
    d = rc.build_directory(cfg, rc.load_roster())
    muted = set(cfg.get('mute') or ())
    window = cfg['replyWindowMinutes'] * 60
    live = sorted([e for e in d if e['lastInboundAt'] is not None and e['kind'] == 'contact'
                   and e['handle'] not in muted and 0 <= now_t - e['lastInboundAt'] <= window],
                  key=lambda e: -e['lastInboundAt'])
    if not live:
        target = 'no one yet \u2014 start a reply with a name, like "Sam: \u2026"'
    elif len(live) > 1 and cfg['ambiguousSeconds'] and live[0]['lastInboundAt'] - live[1]['lastInboundAt'] < cfg['ambiguousSeconds']:
        target = 'unclear (two people wrote close together) \u2014 start with a name'
    else:
        target = live[0]['label'] + ' (' + rc.ago(now_t - live[0]['lastInboundAt']) + ')'
    recents = sorted([e for e in d if e['lastInboundAt'] is not None and e['kind'] in ('contact', 'roster')],
                     key=lambda e: -e['lastInboundAt'])[:5]
    recent = ', '.join('%s %s' % (e['label'], rc.ago(now_t - e['lastInboundAt'])) for e in recents) or 'none'
    return 'A bare reply goes to: %s. Recent texters: %s.' % (target, recent)


def last_unknown_texter(cfg):
    """The most recent roster entry that is not already a contact and not a group \u2014 the natural
    target of /add. Returns {'handle','label'} or None."""
    contacts = {c['handle'] for c in cfg['contacts']}
    cands = [r for r in rc.load_roster()
             if r.get('lastInboundAt') is not None and not str(r['handle']).startswith('group:')
             and r['handle'] not in contacts]
    if not cands:
        return None
    r = max(cands, key=lambda r: r['lastInboundAt'])
    return {'handle': r['handle'], 'label': r.get('label') or rc.format_handle(r['handle'])}


def _store_pending_add(handle, name, at):
    def fn():
        state = load_state()
        state['pendingAdd'] = {'handle': handle, 'name': name, 'at': float(at)}
        rc.write_json_atomic(STATE_PATH, state)
    _with_host_lock(fn)


def _take_pending_add(now_t):
    def fn():
        state = load_state()
        pend = state.get('pendingAdd')
        state.pop('pendingAdd', None)
        rc.write_json_atomic(STATE_PATH, state)
        if isinstance(pend, dict) and pend.get('handle') and now_t - float(pend.get('at') or 0) <= ADD_WINDOW_S:
            return pend
        return None
    return _with_host_lock(fn)


def add_contact(handle, name):
    """Append a contact under the host lock, preserving every other key verbatim."""
    def fn():
        raw = rc.read_raw_config() or {}
        contacts = raw.get('contacts') if isinstance(raw.get('contacts'), list) else []
        if not any(isinstance(c, dict) and rc.normalize_handle(c.get('handle'), raw.get('defaultCountryCode', '1')) == handle
                   for c in contacts):
            contacts.append({'name': name, 'handle': handle})
        raw['contacts'] = contacts
        rc.write_json_atomic(rc.CONFIG_PATH, raw)
    _with_host_lock(fn)


def handle_add(oc, cfg, now, poster):
    base = {'dryRun': cfg['dryRun']}
    if oc.get('handle'):
        name = rc.clean_label(oc.get('name')) or ''
        h = rc.normalize_handle(oc['handle'], cfg['defaultCountryCode'])
        detail = None
        if not name:
            detail = 'Say "/add <Name> <number>".'
        elif not rc.valid_handle(h):
            detail = 'That doesn\u2019t look like a phone number or email. Say "/add %s +1 555 555 0123".' % name
        elif h in rc.owner_handle_set(cfg):
            detail = 'That\u2019s your own number; it can\u2019t be a relay contact.'
        if detail:
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, command='add', error=detail)
        if cfg['dryRun']:
            detail = '(dry run) would add %s as %s. Nothing changed.' % (rc.mask_handle(h), name)
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=True, command='add', dryRun=True, added=name)
        add_contact(h, name)
        rc.log_event('contact_added', via='command-handle')
        detail = 'Added %s (%s). Start a reply with "%s: \u2026" to text them.' % (name, rc.mask_handle(h), name)
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='add', added=name)
    if oc.get('confirm'):
        pend = _take_pending_add(now())
        if not pend:
            detail = 'Nothing to add \u2014 say "/add <Name>" first (the offer expires after 2 minutes).'
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, command='add', error=detail)
        # Nit: "/add <Name> yes" must name the SAME person that was staged; a mismatch is refused (and the
        # pending offer is kept so the owner can retry with the right name within its window).
        cname = (oc.get('name') or '').strip()
        if cname and cname.lower() != str(pend['name']).lower():
            _store_pending_add(pend['handle'], pend['name'], float(pend.get('at') or now()))
            detail = 'That name doesn\u2019t match the pending add (%s). Say "/add yes" or "/add %s yes".' % (pend['name'], pend['name'])
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, command='add', error=detail)
        if cfg['dryRun']:
            # Nit: dry run never writes config.
            detail = '(dry run) would add %s as %s. Nothing changed.' % (rc.mask_handle(pend['handle']), pend['name'])
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=True, command='add', dryRun=True, added=pend['name'])
        add_contact(pend['handle'], pend['name'])
        rc.log_event('contact_added', via='command')
        detail = 'Added %s. Their texts now relay, and you can reply by name.' % pend['name']
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='add', added=pend['name'])
    name = oc.get('name') or ''
    if not name:
        detail = 'Who do you want to add? Say "/add <Name>".'
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=False, command='add', needsTarget=True, error=detail)
    who = last_unknown_texter(cfg)
    if not who:
        detail = 'No recent unknown number to add. (/add saves the last person who texted you who isn\u2019t a contact yet.)'
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=False, command='add', needsTarget=True, error=detail)
    _store_pending_add(who['handle'], name, now())
    _note_prompt(now())
    detail = 'Add %s as %s? Reply yes within 2 min.' % (rc.mask_handle(who['handle']), name)
    notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
    return dict(base, ok=False, command='add', needsConfirm=True, label=name)


def handle_owner_command(oc, quoted, cfg, now, poster):
    base = {'dryRun': cfg['dryRun']}
    c = oc['cmd']
    if c == 'pause':
        if not cfg['paused']:
            set_paused(True)
        rc.log_event('paused', via='command')
        notify(cfg, poster, {'type': 'status', 'status': 'paused',
                             'detail': 'Paused. I won\u2019t relay texts or send replies until you say /resume.',
                             'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='pause', paused=True)
    if c == 'resume':
        if cfg['paused']:
            set_paused(False)
        rc.log_event('resumed', via='command')
        notify(cfg, poster, {'type': 'status', 'status': 'resumed',
                             'detail': 'Resumed. Relaying is back on.', 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='resume', paused=False)
    if c == 'help':
        notify(cfg, poster, {'type': 'status', 'detail': HELP_TEXT, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='help')
    if c == 'who':
        notify(cfg, poster, {'type': 'status', 'detail': who_summary(cfg, now()), 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='who')
    if c == 'add':
        return handle_add(oc, cfg, now, poster)
    return dict(base, ok=False, error='unknown command')


def guess_is_risky(contact, cfg, now_t):
    """A 'recent'-routed reply is risky when the guess is shaky (item 3): the last text was over 10
    minutes ago, OR another contact also texted within the last 15 minutes (so who "last" is, is
    contestable), OR the target is a roster-only person under relayAll. Returns bool."""
    at = contact.get('lastInboundAt')
    if at is None or (now_t - at) > 600:
        return True
    d = rc.build_directory(cfg, rc.load_roster())
    for e in d:
        if (e['handle'] != contact['handle'] and e['kind'] == 'contact'
                and e['lastInboundAt'] is not None and 0 <= now_t - e['lastInboundAt'] <= 900):
            return True
    if cfg['relayAll'] and contact.get('kind') == 'roster':
        return True
    return False


# ---------- v1.4.0: a reply to someone on no relay list ("Jordan Rivera: hey") ----------
def contacts_lookup(name, sleeper, now, wait_s=CONTACTS_WAIT_S, fuzzy=None):
    """Ask the watcher (it can read Contacts) for exact / unique name matches. None when it does not
    answer in time. The job result holds handles, so it is deleted the moment it is read."""
    jobs = os.path.join(rc.APP_DIR, 'jobs')
    jid = 'contacts' + uuid.uuid4().hex[:10]
    job_p, res_p = os.path.join(jobs, jid + '.job.json'), os.path.join(jobs, jid + '.result.json')
    job = {'id': jid, 'type': 'contacts_lookup', 'createdAt': now()}
    if isinstance(name, (list, tuple)):          # v1.4.2: several candidate names in one job
        job.update(names=[str(n)[:80] for n in name][:4], fuzzy=[bool(x) for x in (fuzzy or [])][:4])
    else:
        job['name'] = str(name)[:80]
    rc.write_json_atomic(job_p, job)
    deadline = now() + wait_s
    while True:
        res = rc.read_json(res_p)
        if isinstance(res, dict):
            try:
                os.unlink(res_p)
            except OSError:
                pass
            return res if res.get('ok') else None
        if now() >= deadline:
            break
        sleeper(0.5)
    for p in (job_p, res_p):
        try:
            os.unlink(p)
        except OSError:
            pass
    return None


def _store_pending_new(handle, name, at, prefix='', existing=False):
    def fn():
        state = load_state()
        state['pendingNewContact'] = {'handle': handle, 'name': name, 'at': float(at)}
        if prefix:
            state['pendingNewContact']['prefix'] = str(prefix)[:80]     # which candidate name was offered
        if existing:
            state['pendingNewContact']['existing'] = True               # a typo of someone already on the list
        rc.write_json_atomic(STATE_PATH, state)
    _with_host_lock(fn)


def _peek_pending_new(handle, now_t):
    p = load_state().get('pendingNewContact')
    if isinstance(p, dict) and p.get('handle') == handle and now_t - float(p.get('at') or 0) <= NEW_CONTACT_WINDOW_S:
        return p
    return None


def _finish_new_contact(handle, name, cfg):
    """After the confirmed first text went out: they become a relay contact (same as /add), so the
    owner's later "Jordan: ..." replies and Jordan's texts flow without asking again. Name + handle only."""
    def fn():
        state = load_state()
        state.pop('pendingNewContact', None)
        rc.write_json_atomic(STATE_PATH, state)
    _with_host_lock(fn)
    if not rc.contact_by_handle(cfg, handle):
        add_contact(handle, rc.clean_label(name) or rc.format_handle(handle))
        rc.log_event('contact_added', via='contacts-yes')


def handle_unknown_name(route, cfg, sleeper, now, poster, base):
    """The reply starts with a possible name nobody on the relay list has. Look it up in Contacts and ask;
    never send on a guess. Names only in the prompts (a unique match shows the number's last 4 digits).
    v1.4.2: any case. Only a Contacts (or relay-list typo) match makes it a name: with no match at all the
    result is {'_fallback': True} and the caller routes the text as ordinary (no name)."""
    cands = route.get('candidates') or [{'name': route['unknownName'], 'body': route.get('body', ''),
                                         'fuzzy': False, 'nameLike': True}]
    name_like = any(c.get('nameLike') for c in cands)
    res = contacts_lookup([c['name'] for c in cands], sleeper, now,
                          wait_s=CONTACTS_WAIT_S if name_like else CONTACTS_WAIT_QUICK_S,
                          fuzzy=[c.get('fuzzy') for c in cands])
    results = (res or {}).get('results')
    if results is None and res is not None:      # an older watcher answers one name
        results = [{'matches': res.get('matches') or []}]
    results = results or []
    chosen, matches = cands[0], []
    for i, c in enumerate(cands):                # exact / first name / nickname, most specific candidate first
        ms = [m for m in ((results[i] if i < len(results) else {}).get('matches') or []) if not m.get('fuzzy')]
        if ms:
            chosen, matches = c, ms
            break
    fuzzy = []
    if not matches:
        for i, c in enumerate(cands):
            ms = [m for m in ((results[i] if i < len(results) else {}).get('matches') or []) if m.get('fuzzy')]
            if ms:
                chosen, fuzzy = c, ms
                break
    name = chosen['name']
    first = rc.first_name(name)
    rc.log_event('needs_target', reason='unknown-name', contactsMatches=len(matches), fuzzyMatches=len(fuzzy),
                 dirFuzzy=bool(route.get('dirFuzzy')), candidates=len(cands), contactsChecked=res is not None)
    if not matches and route.get('dirFuzzy'):
        # a close typo of someone already on the relay list ("Jordn:" for Jordan): ask, never send unasked
        df = route['dirFuzzy']
        _store_pending_new(df['handle'], df['label'], now(), prefix=df['name'], existing=True)
        _note_prompt(now())
        detail = '\u2753 Did you mean %s? Reply yes within 2 min to send it to %s (no to cancel).' % (df['label'], df['label'])
        notify(cfg, poster, {'type': 'needs_confirm', 'label': df['label'], 'reason': 'new_contact', 'detail': detail,
                             'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsConfirm=True, label=df['label'], handle=df['handle'], reason='new_contact',
                    contact=df['label'], routedBy='contacts')
    if not matches and len(fuzzy) == 1:
        m = fuzzy[0]
        _store_pending_new(m['handle'], m['name'], now(), prefix=name)
        _note_prompt(now())
        detail = ('\u2753 Did you mean %s? Reply yes within 2 min to text the %s in your Contacts (%s), or no to '
                  'cancel.' % (m['name'], m['name'], rc.mask_handle(m['handle'])))
        notify(cfg, poster, {'type': 'needs_confirm', 'label': m['name'], 'reason': 'new_contact', 'detail': detail,
                             'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsConfirm=True, label=m['name'], handle=m['handle'], reason='new_contact',
                    contact=m['name'], routedBy='contacts')
    if not matches and fuzzy:
        matches = fuzzy                          # several close names: list them (names only), send nothing
    if not matches and not (res is None and any(c.get('nameLike') and c.get('form', 'colon') == 'colon' for c in cands)):
        return dict(base, _fallback=True)        # not a name anyone knows: ordinary text
    if len(matches) == 1:
        m = matches[0]
        hs = m.get('handles') or [{'handle': m['handle'], 'label': ''}]
        if cfg['confirmNewContacts'] == 'exact-skip' and m.get('exact') and len(hs) == 1:
            # v1.4.1: "Jordan Rivera: hey" names exactly one Contacts card, by its full name, with one
            # number/email: no question to ask. The caller sends it and adds them as a contact.
            rc.log_event('new_contact_direct')
            return dict(base, _direct={'handle': m['handle'], 'name': m['name'], 'body': chosen['body']})
        _store_pending_new(m['handle'], m['name'], now(), prefix=name)
        _note_prompt(now())
        if len(hs) > 1:
            def opt(x):
                return rc.mask_handle(x['handle']) + ((' ' + x['label']) if x.get('label') else '')
            others = ', '.join(opt(x) for x in hs if x['handle'] != m['handle'])
            detail = ('\u2753 %s has %d numbers in your Contacts. Reply yes within 2 min to text %s, or /add %s '
                      '<number> to use another (%s).' % (m['name'], len(hs), opt(next(x for x in hs if x['handle'] == m['handle'])),
                                                          first, others))
        else:
            detail = ('\u2753 %s isn\u2019t on your relay list yet. Reply yes within 2 min to text the %s in your '
                      'Contacts (%s), or /add %s <number>.' % (first, m['name'], rc.mask_handle(m['handle']), first))
        notify(cfg, poster, {'type': 'needs_confirm', 'label': m['name'], 'reason': 'new_contact', 'detail': detail,
                             'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsConfirm=True, label=m['name'], handle=m['handle'], reason='new_contact',
                    contact=m['name'], routedBy='contacts')
    if matches:
        names = ', '.join(x['name'] for x in matches[:3]) + (', \u2026' if len(matches) > 3 else '')
        detail = ('%s isn\u2019t on your relay list yet, and your Contacts have more than one: %s. Start with the full '
                  'name ("%s: \u2026"), or /add %s <number>.' % (first, names, matches[0]['name'], first))
    elif res is None:
        detail = ('%s isn\u2019t on your relay list yet (I couldn\u2019t check your Contacts just now). Reply /add %s '
                  '<number>.' % (first, first))
    else:
        detail = ('%s isn\u2019t on your relay list yet, and I didn\u2019t find "%s" in your Contacts. Reply /add %s '
                  '<number>.' % (first, name, first))
    notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'unknown-name', 'at': rc.iso_utc(now())})
    return dict(base, ok=False, needsTarget=True, error=detail, reason='unknown-name')


# ---------- v1.4.1: a plain "yes" / "no" answers a pending confirmation ----------
CONFIRM_WORDS = frozenset(['yes', 'y', 'yep', 'yeah', 'yes please', 'ok', 'okay', 'send', 'send it', 'do it'])
CANCEL_WORDS = frozenset(['no', 'n', 'nope', 'cancel', 'cancel it', 'dont', "don't", 'dont send', "don't send"])
PROMPT_GRACE_S = 600.0     # a stray "yes" this soon after any prompt is never sent as a text


def bare_confirm_word(text):
    """'yes' / 'no' when the WHOLE reply is a confirm or cancel word (any case, punctuation or emoji
    around it: "Yes!", "ok 👍", "Do it."), else None."""
    t = re.sub(r"[^a-z' ]+", ' ', str(text or '').lower())
    t = ' '.join(t.split())
    if t in CONFIRM_WORDS:
        return 'yes'
    if t in CANCEL_WORDS:
        return 'no'
    return None


def _note_prompt(at):
    def fn():
        state = load_state()
        state['lastPromptAt'] = float(at)
        rc.write_json_atomic(STATE_PATH, state)
    _with_host_lock(fn)


def _fresh(state, key, now_t, window):
    p = state.get(key)
    return isinstance(p, dict) and now_t - float(p.get('at') or 0) <= window


def handle_bare_confirm(word, quoted, cfg, now, poster):
    """The extension answers a pending reply confirmation itself (it holds the text). Anything that
    reaches here is a host-side prompt (block, add) or a word that came too late. Returns a response,
    or None when no prompt is anywhere near (then "ok" is just a reply)."""
    base = {'dryRun': cfg['dryRun']}
    t = now()
    state = load_state()
    block = _fresh(state, 'pendingBlock', t, CONFIRM_WINDOW_S)
    add = _fresh(state, 'pendingAdd', t, ADD_WINDOW_S)
    recent = t - float(state.get('lastPromptAt') or 0) <= PROMPT_GRACE_S
    if word == 'yes':
        if block:
            return handle_command({'action': 'block', 'confirm': True, 'target': ''}, quoted, cfg, now, poster)
        if add:
            return handle_add({'cmd': 'add', 'name': '', 'confirm': True}, cfg, now, poster)
        if not recent:
            return None
        detail = 'Nothing is waiting for a yes (confirmations expire after 2 minutes), so nothing was sent. Send your message again.'
    else:
        if not (block or add or recent):
            return None

        def fn():
            st = load_state()
            for k in ('pendingBlock', 'pendingAdd', 'pendingNewContact'):
                st.pop(k, None)
            rc.write_json_atomic(STATE_PATH, st)
        _with_host_lock(fn)
        detail = 'Cancelled. Nothing was sent or changed.'
    rc.log_event('bare_confirm', word=word, pending=bool(block or add))
    notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(t)})
    return dict(base, ok=True, skipped=detail)


def handle_reaction(msg, cfg, now, poster):
    """v1.4.2: the owner put a thumbs-up (confirm) or thumbs-down (cancel) on a pending prompt in the relay
    chat. The extension only sends this for a reaction it saw appear on the newest "Reply yes" prompt within
    2 minutes of it; a pending reply prompt is answered in the extension itself. Here: /block and /add.
    Nothing pending -> ignored silently (a reaction is never a message and never texted)."""
    word = 'yes' if msg.get('word') == 'yes' else 'no' if msg.get('word') == 'no' else None
    conv = str(msg.get('conversationId') or '').strip()
    if not word or (cfg.get('relayConversationId') and conv != cfg['relayConversationId']):
        return {'ok': False, 'error': 'not a relay-chat reaction'}
    if cfg['paused']:
        return {'ok': False, 'error': 'paused'}
    t = now()
    state = load_state()
    if not (_fresh(state, 'pendingBlock', t, CONFIRM_WINDOW_S) or _fresh(state, 'pendingAdd', t, ADD_WINDOW_S)):
        rc.log_event('reaction_ignored', word=word)
        return {'ok': True, 'ignored': True}
    rc.log_event('reaction_confirm', word=word)
    return handle_bare_confirm(word, '', cfg, now, poster)


def _short_mask(handle):
    """'+1 \u2022\u2022\u20220188' -> '\u2022\u2022\u20220188' for the "(new contact, ...)" note; emails stay masked as is."""
    m = rc.mask_handle(handle)
    return m.split(' ', 1)[1] if m.startswith('+') and ' ' in m else m


def handle_send(msg, cfg, runner, sleeper, now, poster):
    text = msg.get('text')
    quoted = msg.get('quotedText') if isinstance(msg.get('quotedText'), str) else ''
    conv = str(msg.get('conversationId') or '').strip()[:120]
    dry = cfg['dryRun']
    base = {'dryRun': dry}

    def refuse(reason, **f):
        rc.log_event('refused', reason=reason, **f)
        return dict(base, ok=False, error=reason)

    if not cfg['relayConversationId']:
        return refuse('relayConversationId not set in config')
    if conv != cfg['relayConversationId']:
        return refuse('not the relay conversation')
    if not isinstance(text, str) or not text.strip():
        return refuse('empty text')
    # v1.3.9: drop X Chat time/status chips an older extension may have appended ("hi\n9:07 AM\n\n9:07 AM").
    text = rc.strip_trailing_chips(text)
    quoted = rc.strip_trailing_chips(quoted)
    if len(text) > MAX_TEXT:
        return refuse('text too long', textLength=len(text))
    oc = parse_owner_command(text)            # /who /pause /resume /help /add /test run even while paused (item 2)
    if oc and oc['cmd'] == 'test':            # a pre-1.3.8 extension forwards "/test" as a reply: never route it
        return relay_test.run_test({'send': oc['send']}, cfg, runner, sleeper, now, poster, via='send-path')
    if oc:
        return handle_owner_command(oc, quoted, cfg, now, poster)
    if cfg['paused']:
        return refuse('paused')
    bw = bare_confirm_word(text)
    if bw and not msg.get('confirmed'):
        r = handle_bare_confirm(bw, quoted, cfg, now, poster)
        if r is not None:                     # a confirm/cancel word answered a prompt: never an iMessage
            return r
    cmd = parse_command(text)
    if cmd:                                   # an explicit /block|/mute|/unblock never becomes an outgoing iMessage
        return handle_command(cmd, quoted, cfg, now, poster)
    hint = bare_command_hint(text, cfg)
    if hint:                                  # bare "block"/"unblock [known name]" -> nudge, send nothing (item N1)
        detail = 'Did you mean /%s \u2026? Nothing was sent.' % hint
        rc.log_event('bare_command_hint', verb=hint)
        notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'bare-command', 'at': rc.iso_utc(now())})
        return dict(base, ok=True, skipped='bare "%s" is not a command (use /%s)' % (hint, hint))
    if not cfg['contacts'] and not cfg['relayAll']:
        return refuse('no contacts in config', textLength=len(text))

    route = rc.route_reply(text, quoted[:500], cfg, rc.load_roster(), now())
    confirmed = bool(msg.get('confirmed'))
    hint = msg.get('handle')
    hint_norm = rc.normalize_handle(hint, cfg['defaultCountryCode']) if hint else None
    new_contact = None
    if confirmed:
        # C1: a "/yes" confirmation must go to the EXACT handle the owner confirmed, never a re-guess.
        # (Between the prompt and the "/yes", someone else may have texted, which would otherwise make
        # the "recent" fallback pick the wrong person.) Require the handle and resolve from it directly.
        if not hint_norm:
            return refuse('confirmed reply without a target handle')
        directory = rc.build_directory(cfg, rc.load_roster())
        tgt = next((e for e in directory if e['handle'] == hint_norm), None)
        pend_new = _peek_pending_new(hint_norm, now())
        if pend_new and (not tgt or tgt.get('kind') != 'group'):
            # v1.4.0: the owner said /yes to texting someone found in Contacts. Only the exact handle that
            # was offered (and only within 2 minutes) qualifies; the name prefix is stripped.
            new_contact = pend_new
            tgt = tgt or {'kind': 'new', 'label': rc.clean_label(pend_new['name']) or rc.format_handle(hint_norm),
                          'name': pend_new['name'], 'handle': hint_norm, 'lastInboundAt': None}
        if not tgt or tgt.get('kind') == 'group':
            return refuse('confirmed handle is not a current recipient', handle=hint_norm)
        if not route.get('needsTarget') and route['contact']['handle'] == hint_norm:
            contact, body, by = route['contact'], route['body'], route['by']   # same target: use its stripped body
        elif new_contact and route.get('unknownName'):
            # "Jordan Rivera: hey" -> "hey" (v1.4.2: the body of the candidate name that was offered)
            pick = next((c for c in route.get('candidates') or [] if c['name'] == new_contact.get('prefix')), None)
            contact, body, by = tgt, (pick['body'] if pick else route['body']), 'contacts'
            if new_contact.get('existing'):
                new_contact = None                                              # a typo of someone on the list
        else:
            contact, body, by = tgt, text, 'confirmed'                         # target drifted: send the full reply
        handle = contact['handle']
    else:
        direct = None
        if route.get('needsTarget') and route.get('unknownName'):
            r = handle_unknown_name(route, cfg, sleeper, now, poster, base)
            if r.get('_fallback'):
                route = route['fallback']            # v1.4.2: no one by that name: ordinary text
            elif '_direct' not in r:
                return r
            else:
                direct = r['_direct']
        if direct:
            # v1.4.1 exact-skip: one Contacts card by its exact full name with one number -> send now.
            new_contact = {'handle': direct['handle'], 'name': direct['name']}
            route = {'contact': {'kind': 'new', 'label': rc.clean_label(direct['name']) or rc.format_handle(direct['handle']),
                                 'name': direct['name'], 'handle': direct['handle'], 'lastInboundAt': None},
                     'body': direct.get('body', route.get('body', '')), 'by': 'contacts'}
            known = next((e for e in rc.build_directory(cfg, rc.load_roster()) if e['handle'] == direct['handle']), None)
            if known and (known.get('kind') == 'contact' or (known.get('kind') == 'roster' and cfg['relayAll'])):
                # already on the relay list under another label (e.g. "Mom"): send there, not as a new contact
                new_contact = None
                route['contact'] = known
        if route.get('needsTarget'):
            rc.log_event('needs_target', textLength=len(text), quoted=bool(quoted))   # reason may name a label; owner gets it via webhook, not the log
            notify(cfg, poster, {'type': 'needs_target', 'detail': route['error'], 'reason': route['reason'],
                                 'at': rc.iso_utc(now())})
            return dict(base, ok=False, needsTarget=True, error=route['error'], reason=route['reason'])
        contact, body, by = route['contact'], route['body'], route['by']
        # item 7: after a quiet-hours digest, the owner's FIRST reply must name a person (several may have
        # texted). A named/quoted/directive reply (by != 'recent') just clears the gate; a bare 'recent' one
        # is turned into a needs_target prompt. Either way the digest is acked so the next reply flows.
        if rc.read_digest_marker() > float(load_state().get('digestAckAt') or 0):
            _ack_digest(now())
            if by == 'recent':
                detail = rc.ask_for_name_message(cfg)
                rc.log_event('needs_target', reason='post-digest', textLength=len(text))
                notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'post-digest', 'at': rc.iso_utc(now())})
                return dict(base, ok=False, needsTarget=True, error=detail, reason='post-digest')
        handle = contact['handle']
        if hint_norm and hint_norm != handle:
            return refuse('the routed recipient does not match the handle the extension reported', handle=handle)
        if not body.strip():
            return refuse('empty text after the name prefix', handle=handle)
        # W1: a directive aimed at someone other than the current conversation (or a dict-word bare form)
        # is held for a /yes confirmation. Label only -- the stripped body is never shown.
        if route.get('confirm'):
            c = route['confirm']
            alt = c.get('altLabel')
            detail = ('Send to %s instead of %s?' % (contact['label'], alt)) if alt else ('Send to %s?' % contact['label'])
            detail = '❓ ' + detail + ' Reply yes within 2 min.'
            rc.log_event('needs_confirm', routedBy=by, reason='directive', textLength=len(body))
            _note_prompt(now())
            notify(cfg, poster, {'type': 'needs_confirm', 'label': contact['label'], 'alt': alt or '',
                                 'reason': 'directive', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, needsConfirm=True, label=contact['label'], handle=handle,
                        reason='directive', contact=contact['label'], routedBy=by)
        # Confirm-when-guessed (item 3): a 'recent'-routed reply may be going to the wrong person. Ask first
        # per the confirmGuessed policy. The host never holds the text; the extension keeps it in session
        # storage and re-sends it with confirmed:true (and the handle) on "/yes".
        # v1.4.2: an unresolved quote, or a roster-only last texter, is always asked first
        must_ask = by == 'recent' and (route.get('quoteUnresolved') or contact.get('kind') == 'roster')
        if must_ask or (by == 'recent' and cfg['confirmGuessed'] != 'off'
                        and (cfg['confirmGuessed'] == 'always' or guess_is_risky(contact, cfg, now()))):
            detail = '❓ Reply to %s? Reply yes within 2 min.' % contact['label']
            rc.log_event('needs_confirm', routedBy=by, reason='guessed', textLength=len(body))
            _note_prompt(now())
            notify(cfg, poster, {'type': 'needs_confirm', 'label': contact['label'], 'reason': 'guessed',
                                 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, needsConfirm=True, guess=True, label=contact['label'],
                        handle=handle, reason='guessed', contact=contact['label'], routedBy=by)
    if not body.strip():
        return refuse('empty text', handle=handle)
    # Echo guard: never send one of the bot's own relay-chat posts, nor a relayed inbound message
    # bounced straight back (compared via per-install HMAC; see relay_common).
    if rc.looks_like_bot_output(text) or rc.looks_like_bot_output(body):
        return refuse('refusing to send one of my own relay messages', handle=handle, textLength=len(body))
    if rc.is_echoed_inbound(text, body, now(), handle=handle):
        # item 4: tell the owner why, body-free, so it is not a silent drop.
        notify(cfg, poster, {'type': 'refused', 'reason': 'echo', 'label': contact['label'], 'at': rc.iso_utc(now())})
        return refuse('refusing to echo a relayed message back to its sender', handle=handle, textLength=len(body))
    if handle in rc.owner_handle_set(cfg):
        return refuse('refusing to relay to the owner handle', textLength=len(body))
    who = {'contact': contact['label'], 'routedBy': by}

    os.makedirs(rc.APP_DIR, exist_ok=True)
    with open(LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)     # serialize concurrent host processes
        state = load_state()
        t = now()
        recent = {k: v for k, v in (state.get('recent') or {}).items() if t - v < DEDUPE_WINDOW_S}
        window = [x for x in (state.get('sends') or []) if t - x < 60]
        fp = fingerprint(handle, body)
        if fp in recent:
            rc.log_event('duplicate_dropped', handle=handle, textLength=len(body), dryRun=dry)
            return dict(base, ok=True, skipped='duplicate within 60s', **who)
        if len(window) >= cfg['maxSendsPerMinute']:
            rc.log_event('rate_limited', handle=handle, textLength=len(body), limit=cfg['maxSendsPerMinute'])
            return dict(base, ok=False, error='rate limited (%d per minute)' % cfg['maxSendsPerMinute'], **who)
        outgoing = body + ('\n' + cfg['footer'] if cfg.get('footer') else '')
        recent[fp] = t
        window.append(t)
        state.update(recent=recent, sends=window)
        ok, err = True, ''
        if not dry:
            ok, err = rc.send_imessage(outgoing, handle, runner=runner, sleeper=sleeper)
            if not ok:
                if err != 'timeout':        # a clear failure: allow an immediate manual retry
                    recent.pop(fp, None)
                    window.pop()
                # a timeout is ambiguous (it may actually have sent): keep the fingerprint, no double-send
            elif cfg.get('deliveryCheck', True):
                rc.add_pending_send(handle, t, len(outgoing), label=contact['label'],   # record inside the lock
                                    note=('new contact, ' + _short_mask(handle)) if new_contact else '')
        rc.write_json_atomic(STATE_PATH, state)
    if new_contact and ok and not dry:
        try:
            _finish_new_contact(handle, new_contact['name'], cfg)
        except Exception:
            pass
    note = ('new contact, ' + _short_mask(handle)) if new_contact else ''
    if note:
        who['note'] = note
    if dry:
        rc.log_event('dry_run', handle=handle, contact=contact['label'], routedBy=by, textLength=len(body), outgoingLength=len(outgoing))
        if cfg['confirmSends']:
            notify(cfg, poster, {'type': 'sent', 'label': contact['label'], 'contactName': contact['name'], 'dryRun': True,
                                 'delivered': None, 'routedBy': by, 'at': rc.iso_utc(t)})
        who.pop('note', None)
        return dict({'ok': True, 'dryRun': True, 'textLength': len(body)}, **who)
    if not ok:
        rc.log_event('send_failed', textLength=len(body), error=err)      # err is a fixed code, never raw text
        return dict({'ok': False, 'dryRun': False, 'error': rc.error_message(err), 'errorCode': err}, **who)
    # Delivery proof is verified by the watcher (it holds Full Disk Access); the host recorded a
    # body-free pending entry inside its lock above. The host never confirms delivery itself.
    rc.log_event('sent', routedBy=by, textLength=len(body), delivered=None, deliveryNote='pending')
    # item 6: when deliveryCheck is on, the WATCHER posts the single confirmation (✓✓ delivered, or ✓ sent
    # after ~20s). The host only confirms here when delivery checking is off, so the owner still hears back.
    if cfg['confirmSends'] and not cfg.get('deliveryCheck', True):
        p = {'type': 'sent', 'label': contact['label'], 'contactName': contact['name'], 'dryRun': False,
             'delivered': None, 'routedBy': by, 'at': rc.iso_utc(t)}
        if note:
            p['note'] = note
        notify(cfg, poster, p)
    return dict({'ok': True, 'dryRun': False, 'sent': True, 'delivered': None, 'deliveryCheck': 'pending'}, **who)


def handle_selftest(msg, cfg, runner, sleeper, now):
    """Sends SELFTEST_TEXT to the owner's own handle. Runs inside Chrome's host process so the
    'Google Chrome wants to control Messages' Automation prompt is answered during setup.
    Ignores dryRun (it only ever goes to the owner). Writes selftest/result.json."""
    pend = pending_selftest(now)
    req_id = str(msg.get('id') or '')
    if not pend or pend['id'] != req_id:
        return {'ok': False, 'error': 'no matching self-test request'}
    owner = cfg.get('ownerHandle', '')
    if not rc.valid_handle(owner):
        res = {'id': req_id, 'ok': False, 'error': 'ownerHandle missing or invalid in config', 'at': now()}
    else:
        t = now()
        ok, err = rc.send_imessage(rc.SELFTEST_TEXT, owner, runner=runner, sleeper=sleeper, timeout=120)
        res = {'id': req_id, 'ok': ok, 'error': err, 'sentAt': t, 'at': now(), 'via': 'chrome-host'}
    rc.write_json_atomic(os.path.join(SELFTEST_DIR, 'result.json'), res)
    rc.log_event('selftest_send', ok=res['ok'], error=res.get('error', ''), via='chrome-host')
    return {'ok': res['ok'], 'error': res.get('error', ''), 'detail': 'sent' if res['ok'] else 'failed'}


def handle_test(msg, cfg, runner, sleeper, now, poster):
    """v1.3.8 /test from the extension. Only from the relay chat; works while paused and with no
    contacts; never texts a contact (see relay_test)."""
    conv = str(msg.get('conversationId') or '').strip()[:120]
    if not cfg['relayConversationId']:
        return {'ok': False, 'error': 'relayConversationId not set in config'}
    if conv != cfg['relayConversationId']:
        rc.log_event('refused', reason='test outside the relay conversation')
        return {'ok': False, 'error': 'not the relay conversation'}
    return relay_test.run_test({'testId': msg.get('testId'), 'send': msg.get('send') is True,
                                'extVersion': msg.get('extVersion')}, cfg, runner, sleeper, now, poster)


def handle_signal(msg, cfg, now, poster):
    """A browser-side condition (item 6) the host turns into one body-free webhook. No message text."""
    status = str(msg.get('status') or '')
    if status == 'wrong_account':
        rc.log_event('signal', status='wrong_account')
        # Unique id per wrong-account EPISODE (the extension sends the episode timestamp), so a second
        # episode within 24h is not suppressed by the routine's id dedupe; repeats inside one episode
        # share the id and stay deduped (item 6).
        at = str(msg.get('at') or '')
        ep_id = ('wrong_account:' + at) if at else 'wrong_account'
        notify(cfg, poster, {'type': 'wrong_account', 'id': ep_id, 'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'guess_cancelled':
        # v1.4.1: the owner answered a reply prompt with "no" / "cancel" (the extension dropped the text)
        label = str(msg.get('label') or '').strip()
        rc.log_event('signal', status='guess_cancelled')
        detail = ('Cancelled. Nothing was sent to %s.' % label) if label else 'Cancelled. Nothing was sent.'
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'guess_expired':
        # the extension's 2-minute confirm window lapsed with no "/yes"; tell the owner, body-free (item 3)
        label = str(msg.get('label') or '').strip()
        rc.log_event('signal', status='guess_expired')
        notify(cfg, poster, {'type': 'guess_expired', 'label': label, 'at': rc.iso_utc(now())})
        return {'ok': True}
    if status in ('test_seen', 'test_unseen'):
        # v1.3.8 /test: the extension saw (or gave up waiting for) the routine's report. Local only.
        return {'ok': relay_test.record_routine(msg.get('testId'), status == 'test_seen', now)}
    return {'ok': False, 'error': 'unknown signal'}


def handle_request(msg, cfg=None, runner=subprocess.run, sleeper=time.sleep, now=time.time,
                   poster=rc.post_json):
    cfg = cfg if cfg is not None else rc.load_config()
    if not isinstance(msg, dict):
        return {'ok': False, 'error': 'bad request'}
    typ = msg.get('type')
    if typ == 'get_config':
        note_extension(msg, now)
        return handle_get_config(cfg, now)
    if typ == 'ping':
        return {'ok': True, 'version': rc.VERSION}
    if typ == 'send':
        return handle_send(msg, cfg, runner, sleeper, now, poster)
    if typ == 'selftest':
        return handle_selftest(msg, cfg, runner, sleeper, now)
    if typ == 'signal':
        return handle_signal(msg, cfg, now, poster)
    if typ == 'test':
        return handle_test(msg, cfg, runner, sleeper, now, poster)
    if typ == 'reaction':
        return handle_reaction(msg, cfg, now, poster)
    return {'ok': False, 'error': 'unknown request type'}


def main():
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            msg = read_message(stdin)
        except Exception as e:
            rc.log_event('bad_frame', error=str(e)[:200])
            write_message(stdout, {'ok': False, 'error': 'bad frame'})
            return 1
        if msg is None:
            return 0
        try:
            resp = handle_request(msg)
        except Exception as e:
            rc.log_event('host_error', error=str(e)[:200])
            resp = {'ok': False, 'error': 'host error'}
        write_message(stdout, resp)


if __name__ == '__main__':
    sys.exit(main())
