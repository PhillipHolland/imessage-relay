#!/usr/bin/python3
"""
com.imessage_relay.host: Chrome native messaging host for iMessage Relay v1.4.24.

Chrome starts this process and talks to it over stdin/stdout: a 4-byte length (native byte
order, little-endian on Macs) followed by UTF-8 JSON. Requests:

  {"type":"get_config"}  -> {"ok":true,"version","paused","dryRun","relayConversationId",
                             "contacts":[{name,label,handle}],"ownerXHandle","senderXHandle","pendingSelfTest"}
  {"type":"send","text","quotedText"?,"quoteLabel"?,"conversationId","ts"}
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
import unicodedata
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_common as rc  # noqa: E402
import relay_test  # noqa: E402  (v1.3.8: the /test end-to-end check)
import relay_ui  # noqa: E402  (v1.4.6: fast feedback in the owner's X tab)
import relay_media  # noqa: E402  (v1.4.6: attachments the owner sends from X Chat)
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
PICK_WINDOW_S = 600.0            # v1.4.13: a numbered pick prompt (no time limit shown) stays answerable 10 min
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
        'uiReactions': bool(cfg.get('uiReactions', True)), 'undoSeconds': int(cfg.get('undoSeconds', 5)),   # v1.4.6
        'relayAttachments': bool(cfg.get('relayAttachments', True)),
    }


# v1.4.6: per-request UI context. When the extension says it can act in the owner's X tab (`ui: true`),
# a confirm prompt becomes a ❓ reaction on HIS message and a "Who's this for?" is typed by the extension
# (tagged UI_TAG, never relayed). The routine copy of that prompt is DEFERRED by UI_FALLBACK_S and only
# posted if the extension does not ack, so a UI miss still reaches the phone the old way.
UI_TAG = '\U0001f916'          # every relay-authored message the extension types starts with this
UI_DEFER_TYPES = ('needs_confirm', 'needs_target')
_UI = {'on': False, 'uuid': '', 'react': [], 'type': []}


def _ui_reset(msg=None):
    on = bool(isinstance(msg, dict) and msg.get('ui') is True and msg.get('type') in ('send', 'reaction', 'signal'))
    u = str((msg or {}).get('xUuid') or '') if isinstance(msg, dict) else ''
    _UI.update(on=on, uuid=u if relay_ui.valid_uuid(u) else '', react=[], type=[])


def _ui_out():
    out = {}
    if _UI['react']:
        out['react'] = list(_UI['react'])
    if _UI['type']:
        out['type'] = list(_UI['type'])
    return out


def _defer_to_ui(cfg, payload, now=time.time):
    """Queue the routine copy for later and hand the prompt to the extension instead. Returns True if
    deferred (the caller then posts nothing now)."""
    t = payload.get('type')
    if not _UI['on'] or t not in UI_DEFER_TYPES or not cfg.get('uiPrompts', True):
        return False
    detail = str(payload.get('detail') or '').strip()
    # v1.4.12: every prompt (confirm included) is typed as plain 🤖 text right away. The ❓ reaction needed
    # X's emoji picker and could spend ~11 s failing before the routine copy went out.
    if not detail:
        return False
    action = {'text': UI_TAG + ' ' + detail[:400]}
    rc.ensure_event_id(payload)
    note = uuid.uuid4().hex
    try:
        rc.write_json_atomic(os.path.join(NOTIFY_RETRY_DIR, note + '.json'),
                             {'queuedAt': now(), 'notBefore': now() + relay_ui.UI_FALLBACK_S, 'ui': True,
                              'payload': payload}, mode=0o600)
    except OSError:
        return False
    action['noteId'] = note
    (_UI['react'] if 'emoji' in action else _UI['type']).append(action)
    rc.log_event('notify', type=t, ok=True, ui=True)
    return True


def notify(cfg, poster, payload):
    """Body-free status event to the bot's webhook (sent confirmations, "who is this for?")."""
    if not cfg.get('webhookUrl'):
        return False
    if _defer_to_ui(cfg, payload):
        return True
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
                detail = 'Nothing is waiting to confirm. /block name. It expires after 2 minutes.'
                return dict(base, ok=False, error=detail)
            if cmd['target']:
                # "/block Bob yes" must confirm only the staged target, never a different one (item N2).
                tgt = resolve_mute_target(cmd['target'], '', cfg, now())
                if tgt.get('handle') and tgt['handle'] != pend['handle']:
                    _store_pending_block(pend['handle'], pend['label'], pend['at'])   # keep the staged one
                    detail = ('That doesn\u2019t match the block for %s. Reply yes to '
                              'mute %s, or /block %s to start over.' % (pend['label'], pend['label'], tgt['label']))
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
            detail = 'Who do you want to block? Reply to their message, or /block name.'
            rc.log_event('needs_target', reason=tgt['reason'], action='block')
            notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': tgt['reason'], 'at': rc.iso_utc(now())})
            return dict(base, ok=False, needsTarget=True, error=detail, reason=tgt['reason'])
        _store_pending_block(tgt['handle'], tgt['label'], now())
        _note_prompt(now())
        detail = 'Stop relaying %s? Reply yes within 2 min.' % tgt['label']
        notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'confirm-block', 'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsConfirm=True, label=tgt['label'], error=detail)
    # unblock: safe, no confirmation
    tgt = resolve_mute_target(cmd['target'], quoted, cfg, now())
    if tgt.get('needsTarget'):
        detail = 'Who do you want to unblock? /unblock name.'
        rc.log_event('needs_target', reason=tgt['reason'], action='unblock')
        notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': tgt['reason'], 'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsTarget=True, error=detail, reason=tgt['reason'])
    if not cfg['dryRun']:
        toggle_mute(tgt['handle'], False)              # dry run previews only; never mutates config
    rc.log_event('unmuted', routedBy='command', dryRun=cfg['dryRun'])
    notify(cfg, poster, {'type': 'unblocked', 'label': tgt['label'], 'at': rc.iso_utc(now())})
    return dict(base, ok=True, muted=False, label=tgt['label'], action='unblocked')


# ---------- owner commands (item 2): /who /pause /resume /help /add /test /status /snooze ----------
# These act only in the relay conversation, which is the owner's own 1:1 with the sender account, so
# reaching here already means the owner typed them. They are parsed BEFORE the paused check so the owner
# can /resume. None of them ever sends an iMessage, so they are safe while paused.
HELP_TEXT = ('/status \u00b7 /who\n'
             '/pause \u00b7 /resume\n'
             '/snooze \u00b7 /snooze 8h \u00b7 /snooze until 7:00\n'
             '/yes \u00b7 /block name \u00b7 /block yes \u00b7 /unblock name\n'
             '/add name \u00b7 /add name number\n'
             '/test \u00b7 /test send \u00b7 /help\n'
             'name: your message\n'
             'Quote-reply a text to answer that person.\n'
             '\U0001f5d1\ufe0f within 5s undoes a send. \U0001f5d1\ufe0f on an older text asks to block.')
PAUSE_DETAIL = ('Paused. Texts that arrive while paused are not kept. '
                '/resume, or /snooze to hold them.')
_OWNER_CMD_RE = re.compile(r'^/(who|pause|resume|help|add|test|status|snooze)\b\s*(.*)$', re.I | re.S)
ADD_WINDOW_S = 120.0


def parse_owner_command(text):
    """Return {'cmd',...} for /who /pause /resume /help /add /status /snooze, else None. /add takes a
    name and an optional 'yes' confirmation ("/add Kent", then "/add yes"). /snooze keeps its args."""
    s = (text or '').strip()
    m = _OWNER_CMD_RE.match(s)
    if not m:
        return None
    cmd = m.group(1).lower()
    rest = (m.group(2) or '').strip()
    if cmd == 'snooze':
        return {'cmd': 'snooze', 'rest': rest[:40]}
    if cmd == 'status':
        return {'cmd': 'status'}
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


def set_run_state(paused, snooze_until):
    """Pause drops inbound texts. A snooze holds them (quiet-hours path) and always clears pause.
    snooze_until 0 removes the key."""
    def fn():
        raw = rc.read_raw_config() or {}
        raw['paused'] = bool(paused)
        if snooze_until:
            raw['snoozeUntil'] = int(snooze_until)
        else:
            raw.pop('snoozeUntil', None)
        rc.write_json_atomic(rc.CONFIG_PATH, raw)
    _with_host_lock(fn)


def _promote_hint(cfg, directory):
    """One gentle /who hint for the roster person who has written the most, once they are familiar.
    Never adds them. No message text."""
    if not cfg.get('relayAll'):
        return ''
    best = None
    for e in directory:
        if e.get('kind') != 'roster' or not rc.roster_familiar(e):
            continue
        try:
            n = int(e.get('inboundCount') or 0)
        except (TypeError, ValueError):
            n = 0
        if best is None or n > best[0]:
            best = (n, e)
    if not best:
        return ''
    label = best[1]['label']
    return ' %s has written often. /add %s' % (label, label)


def who_summary(cfg, now_t):
    """A body-free summary for /who: who a bare (unprefixed) reply would go to right now, plus the most
    recent senders by label and relative time. Same directory routing uses. Never includes any message text."""
    d = rc.build_directory(cfg, rc.load_roster())
    kind, val = rc.classify_bare_reply(cfg, d, now_t)
    recents = sorted([e for e in d if e['lastInboundAt'] is not None and e['kind'] in ('contact', 'roster')],
                     key=lambda e: -e['lastInboundAt'])[:5]
    recent = ', '.join('%s %s' % (e['label'], rc.ago(now_t - e['lastInboundAt'])) for e in recents)
    recent_bit = (' Recent: %s.' % recent) if recent else ''
    hint = _promote_hint(cfg, d)
    if kind == 'ask' and val == 'ambiguous':
        return 'Replies are unclear. Two people wrote close together. Start with a name.' + recent_bit + hint
    if kind == 'ask' and val == 'group':
        return 'A group wrote last. Start with a name.' + recent_bit + hint
    if kind != 'send':
        return 'No one to reply to yet. Start with name: your message' + recent_bit + hint
    return 'Replies go to %s, %s.%s%s' % (val['label'], rc.ago(now_t - val['lastInboundAt']), recent_bit, hint)


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def _age_unit(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return s, 's'
    if s < 3600:
        return max(1, int(round(s / 60.0))), 'm'
    if s < 86400:
        return max(1, int(round(s / 3600.0))), 'h'
    return max(1, int(round(s / 86400.0))), 'd'


def bare_reply_name(cfg, now_t):
    """Who a bare reply would go to right now: a label, 'unclear', or 'name someone'. Same directory
    routing uses. No message text."""
    d = rc.build_directory(cfg, rc.load_roster())
    kind, val = rc.classify_bare_reply(cfg, d, now_t)
    if kind == 'ask' and val == 'ambiguous':
        return 'unclear'
    if kind != 'send':
        return 'name someone'
    return val['label']


def status_line(cfg, now_t):
    """One body-free line for /status. Ages and names only. The routine posts this text as-is."""
    st = rc.read_json(os.path.join(rc.APP_DIR, 'status.json'), {}) or {}
    if cfg['paused']:
        state = 'Paused'
    elif rc.snoozing(cfg, now_t):
        state = 'Snoozing until ' + rc.format_clock(rc.snooze_until(cfg, now_t))
    else:
        state = 'On'
    if st.get('chatDb') == 'unavailable':
        watcher = 'watcher can\u2019t read'
    else:
        at = _parse_iso(st.get('lastTickAt'))
        if at is None:
            watcher = 'watcher not seen'
        else:
            n, u = _age_unit(now_t - at)
            watcher = 'watcher %d%s' % (n, u)
    mr = st.get('messagesRunning')
    messages = 'Messages open' if mr is True else 'Messages closed' if mr is False else 'Messages not checked'
    fwd = _parse_iso(st.get('lastForwardAt'))
    if fwd is None:
        last = 'no texts yet'
    elif now_t - fwd < 45:
        last = 'last text just now'
    else:
        n, u = _age_unit(now_t - fwd)
        last = 'last text %d%s' % (n, u)
    who = bare_reply_name(cfg, now_t)
    if who == 'unclear':
        reply = 'replies unclear'
    elif who == 'name someone':
        reply = 'name someone'
    else:
        reply = 'replies to ' + who
    return '%s \u00b7 v%s \u00b7 %s \u00b7 %s \u00b7 %s \u00b7 %s' % (
        state, rc.VERSION, watcher, messages, last, reply)


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


def _save_label(match, fallback):
    return rc.clean_label((match or {}).get('saveAs') or (match or {}).get('name') or fallback) or fallback


def _add_target_for_name(name, cfg, now, sleeper):
    """Who `/add Name` means: one directory hit, else one Contacts card. None leaves the last-unknown path.
    Returns {'handle','label','already'} or {'error': detail} or None."""
    hits = [e for e in rc.directory_name_hits(cfg, name) if e.get('kind') in ('contact', 'roster')]
    if len(hits) > 1:
        labels = ', '.join(e['label'] for e in hits[:3])
        return {'error': 'More than one %s (%s). /add %s and the number.' % (name, labels, name)}
    if len(hits) == 1:
        e = hits[0]
        if e.get('kind') == 'contact':
            return {'error': '%s is already a contact.' % e['label']}
        return {'handle': e['handle'], 'label': rc.clean_label(name) or e['label']}
    if sleeper is None:
        return None
    res = contacts_lookup(name, sleeper, now, wait_s=CONTACTS_WAIT_QUICK_S)
    matches = []
    if isinstance(res, dict):
        matches = list(res.get('matches') or [])
    if len(matches) > 1:
        return {'error': 'More than one %s in Contacts. /add %s and the number.' % (name, name)}
    if len(matches) == 1:
        m = matches[0]
        return {'handle': m.get('handle'), 'label': _save_label(m, name)}
    return None


def _stage_named_add(found, name, cfg, now, poster, base):
    if found.get('error'):
        notify(cfg, poster, {'type': 'status', 'detail': found['error'], 'at': rc.iso_utc(now())})
        return dict(base, ok=False, command='add', needsTarget=True, error=found['error'])
    handle = found.get('handle')
    if not handle:
        return None
    label = found.get('label') or name
    _store_pending_add(handle, label, now())
    _note_prompt(now())
    detail = 'Add %s (%s)? Reply yes within 2 min.' % (label, rc.mask_handle(handle))
    notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
    return dict(base, ok=False, command='add', needsConfirm=True, label=label)


def handle_add(oc, cfg, now, poster, sleeper=None):
    base = {'dryRun': cfg['dryRun']}
    if oc.get('handle'):
        name = rc.clean_label(oc.get('name')) or ''
        h = rc.normalize_handle(oc['handle'], cfg['defaultCountryCode'])
        detail = None
        if not name:
            detail = '/add name and the number.'
        elif not rc.valid_handle(h):
            detail = 'That doesn\u2019t look like a phone number or email. /add %s and the number.' % name
        elif h in rc.owner_handle_set(cfg):
            detail = 'That\u2019s your own number. It can\u2019t be a contact.'
        if detail:
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, command='add', error=detail)
        if cfg['dryRun']:
            detail = 'Dry run. Would add %s (%s).' % (name, rc.mask_handle(h))
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=True, command='add', dryRun=True, added=name)
        add_contact(h, name)
        rc.log_event('contact_added', via='command-handle')
        detail = 'Added %s (%s).' % (name, rc.mask_handle(h))
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='add', added=name)
    if oc.get('confirm'):
        pend = _take_pending_add(now())
        if not pend:
            detail = 'Nothing is waiting to add. /add name. It expires after 2 minutes.'
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, command='add', error=detail)
        # Nit: "/add <Name> yes" must name the SAME person that was staged; a mismatch is refused (and the
        # pending offer is kept so the owner can retry with the right name within its window).
        cname = (oc.get('name') or '').strip()
        if cname and cname.lower() != str(pend['name']).lower():
            _store_pending_add(pend['handle'], pend['name'], float(pend.get('at') or now()))
            detail = 'That doesn\u2019t match the add for %s. /add yes' % pend['name']
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, command='add', error=detail)
        if cfg['dryRun']:
            # Nit: dry run never writes config.
            detail = 'Dry run. Would add %s (%s).' % (pend['name'], rc.mask_handle(pend['handle']))
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=True, command='add', dryRun=True, added=pend['name'])
        add_contact(pend['handle'], pend['name'])
        rc.log_event('contact_added', via='command')
        detail = 'Added %s.' % pend['name']
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='add', added=pend['name'])
    name = oc.get('name') or ''
    if not name:
        detail = 'Who do you want to add? /add name'
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=False, command='add', needsTarget=True, error=detail)
    found = _add_target_for_name(name, cfg, now, sleeper)
    if found:
        staged = _stage_named_add(found, name, cfg, now, poster, base)
        if staged:
            return staged
    who = last_unknown_texter(cfg)
    if not who:
        detail = 'No recent unknown number to add.'
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=False, command='add', needsTarget=True, error=detail)
    _store_pending_add(who['handle'], name, now())
    _note_prompt(now())
    detail = 'Add %s (%s)? Reply yes within 2 min.' % (name, rc.mask_handle(who['handle']))
    notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
    return dict(base, ok=False, command='add', needsConfirm=True, label=name)


def handle_owner_command(oc, quoted, cfg, now, poster, sleeper=None):
    base = {'dryRun': cfg['dryRun']}
    c = oc['cmd']
    if c == 'pause':
        set_run_state(True, None)          # also ends a snooze; pause does not keep texts
        rc.log_event('paused', via='command')
        notify(cfg, poster, {'type': 'status', 'status': 'paused',
                             'detail': PAUSE_DETAIL, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='pause', paused=True)
    if c == 'resume':
        if cfg['paused'] or cfg.get('snoozeUntil'):
            set_run_state(False, None)
        rc.log_event('resumed', via='command')
        notify(cfg, poster, {'type': 'status', 'status': 'resumed',
                             'detail': 'Relaying is back on.', 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='resume', paused=False)
    if c == 'snooze':
        until, err = rc.parse_snooze(oc.get('rest') or '', now())
        if err:
            notify(cfg, poster, {'type': 'status', 'detail': err, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, command='snooze', error=err)
        until = int(round(float(until)))
        set_run_state(False, until)        # hold like quiet hours; do not use the pause cursor
        rc.log_event('snoozed', seconds=max(0, int(until - now())))
        detail = 'Snoozing until %s. Texts arrive as one digest. /resume' % rc.format_clock(until)
        notify(cfg, poster, {'type': 'status', 'status': 'snoozing', 'detail': detail, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='snooze', snoozeUntil=int(until))
    if c == 'help':
        notify(cfg, poster, {'type': 'status', 'detail': HELP_TEXT, 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='help')
    if c == 'who':
        notify(cfg, poster, {'type': 'status', 'detail': who_summary(cfg, now()), 'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='who')
    if c == 'status':
        rc.log_event('status_cmd')
        notify(cfg, poster, {'type': 'status', 'status': 'status', 'detail': status_line(cfg, now()),
                             'at': rc.iso_utc(now())})
        return dict(base, ok=True, command='status')
    if c == 'add':
        return handle_add(oc, cfg, now, poster, sleeper=sleeper)
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
    if cfg['relayAll'] and contact.get('kind') == 'roster' and not rc.roster_familiar(contact):
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
    stalled = 0
    last = now()
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
        sleeper(0.1)                           # v1.4.13: was 0.5 s (the watcher now answers in ~0.25 s)
        cur = now()
        if cur <= last:                        # a clock that does not move must not spin
            stalled += 1
            if stalled >= 3:
                break
        else:
            stalled = 0
            last = cur
    for p in (job_p, res_p):
        try:
            os.unlink(p)
        except OSError:
            pass
    return None


GROUP_CONFIRM_WINDOW_S = 150.0     # the 2-minute prompt plus a little slack for the extension round trip
GROUP_GUID_WAIT_S = 4.0


def group_guid_lookup(key, sleeper, now, wait_s=GROUP_GUID_WAIT_S):
    """v1.4.5: ask the watcher (it can read chat.db) for the Messages chat id of a group key. Metadata only."""
    jobs = os.path.join(rc.APP_DIR, 'jobs')
    jid = 'groupguid' + uuid.uuid4().hex[:10]
    job_p, res_p = os.path.join(jobs, jid + '.job.json'), os.path.join(jobs, jid + '.result.json')
    rc.write_json_atomic(job_p, {'id': jid, 'type': 'group_guid', 'key': str(key)[:130], 'createdAt': now()})
    deadline = now() + wait_s
    while True:
        res = rc.read_json(res_p)
        if isinstance(res, dict):
            try:
                os.unlink(res_p)
            except OSError:
                pass
            g = str(res.get('guid') or '') if res.get('ok') else ''
            return g if re.match(r'^[A-Za-z0-9;:+@._-]{3,160}$', g) else None
        if now() >= deadline:
            break
        sleeper(0.1)
    for p in (job_p, res_p):
        try:
            os.unlink(p)
        except OSError:
            pass
    return None


def _store_pending_group(handle, guid, label, at):
    def fn():
        state = load_state()
        state['pendingGroupSend'] = {'handle': handle, 'guid': guid, 'label': label, 'at': float(at)}
        rc.write_json_atomic(STATE_PATH, state)
    _with_host_lock(fn)


def _take_pending_group(handle, now_t):
    """The group prompt the owner is answering: same group, within the window. Consumed once."""
    def fn():
        state = load_state()
        p = state.get('pendingGroupSend')
        if not (isinstance(p, dict) and p.get('handle') == handle
                and 0 <= now_t - float(p.get('at') or 0) <= GROUP_CONFIRM_WINDOW_S):
            return None
        state.pop('pendingGroupSend', None)
        rc.write_json_atomic(STATE_PATH, state)
        return p
    return _with_host_lock(fn)


def _merge_same_name(matches):
    """v1.4.11: several Contacts cards with the same full name are ONE person: merge their handles (already
    normalized by the watcher; deduped), keep the order they came in."""
    groups, order = {}, []
    for m in matches:
        k = ' '.join(str(m.get('name') or '').lower().split())
        g = groups.get(k)
        if g is None:
            g = groups[k] = dict(m, handles=[])
            order.append(k)
        g['exact'] = bool(g.get('exact') or m.get('exact'))
        for x in (m.get('handles') or [{'handle': m['handle'], 'label': ''}]):
            if x.get('handle') and x['handle'] not in [y['handle'] for y in g['handles']]:
                g['handles'].append(x)
    return [groups[k] for k in order]


def _by_recent_use(handles, best=''):
    """v1.4.11: the handle this person last used with the relay (roster times, handles only) goes first."""
    seen = {}
    for r in rc.load_roster():
        t = max(float(r.get(k) or 0) for k in ('lastInboundAt', 'lastOutboundAt', 'lastSentAt'))
        seen[r.get('handle')] = t
    return sorted(handles, key=lambda x: (-seen.get(x['handle'], 0.0), x['handle'] != best))   # ties: the card's best


_PERSONAL_MAIL = ('icloud.com', 'me.com', 'mac.com', 'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com',
                  'live.com', 'msn.com', 'yahoo.com', 'aol.com', 'proton.me', 'protonmail.com')
_PHONE_LABEL_RANK = {'mobile': 0, 'iphone': 0, 'cell': 0, 'pref': 1, 'main': 2, '': 3, 'home': 4, 'work': 5}
PICK_MAX = 4


def _rank_options(hs, best=''):
    """v1.4.12: phones first (most recently relayed, then mobile/iPhone/pref, then the card's best), then
    emails. A work/school/business email is dropped when a phone or personal email exists. At most 4."""
    seen = {}
    for r in rc.load_roster():
        seen[r.get('handle')] = max(float(r.get(k) or 0) for k in ('lastInboundAt', 'lastOutboundAt', 'lastSentAt'))
    phones = [x for x in hs if '@' not in x['handle']]
    mails = [x for x in hs if '@' in x['handle']]
    personal = [x for x in mails if x['handle'].rsplit('@', 1)[-1].lower() in _PERSONAL_MAIL]
    if phones or personal:
        mails = personal
    phones.sort(key=lambda x: (-seen.get(x['handle'], 0.0), _PHONE_LABEL_RANK.get(x.get('label') or '', 3),
                               x['handle'] != best))
    mails.sort(key=lambda x: -seen.get(x['handle'], 0.0))
    return (phones + mails)[:PICK_MAX] or hs[:1]


def _store_pending_new(handle, name, at, prefix='', existing=False, options=None):
    def fn():
        state = load_state()
        state['pendingNewContact'] = {'handle': handle, 'name': name, 'at': float(at)}
        if options:
            state['pendingNewContact']['options'] = [str(h) for h in options][:9]   # v1.4.11: "1/2/3" pick
        if prefix:
            state['pendingNewContact']['prefix'] = str(prefix)[:80]     # which candidate name was offered
        if existing:
            state['pendingNewContact']['existing'] = True               # a typo of someone already on the list
        rc.write_json_atomic(STATE_PATH, state)
    _with_host_lock(fn)


def _peek_pending_new(handle, now_t):
    p = load_state().get('pendingNewContact')
    if (isinstance(p, dict) and (p.get('handle') == handle or handle in (p.get('options') or []))
            and now_t - float(p.get('at') or 0) <= (PICK_WINDOW_S if handle in (p.get('options') or []) else NEW_CONTACT_WINDOW_S)):
        return p
    return None


def pick_answer(text, options):
    """v1.4.13: (looks_like_pick, handle) for a reply to a numbered pick prompt: an index 1-9, a phone's
    last 4 digits, or the unique start of an email. Same rules as the extension's pickOption."""
    s = str(text or '').strip().rstrip('.)!').lower()
    opts = [str(h) for h in (options or [])]
    if re.match(r'^[1-9]$', s):
        i = int(s) - 1
        return True, (opts[i] if i < len(opts) else '')
    if re.match(r'^\d{4}$', s):
        hits = [h for h in opts if '@' not in h and re.sub(r'\D', '', h).endswith(s)]
        return bool(hits), (hits[0] if len(hits) == 1 else '')   # "2026" matching no option is ordinary text
    if re.match(r'^[a-z0-9._%+-]{2,}@?[a-z0-9.-]*$', s):
        hits = [h for h in opts if '@' in h and h.lower().startswith(s)]
        if len(hits) == 1:
            return True, hits[0]
    return False, ''


def short_answer(text):
    """v1.4.15: a reply that can only be a prompt answer: at most 4 characters, all digits, emoji only, or
    an email start ("alex@", "alex@ic")."""
    s = str(text or '').strip()
    if not s:
        return False
    if len(s) <= 4 or re.match(r'^[\d\s().+-]+$', s) or re.match(r'^[a-z0-9._%+-]{1,40}@[a-z0-9.-]*$', s, re.I):
        return True
    return not re.search(r'[^\W_]', s)          # no letters or digits at all: emoji / punctuation only


def emoji_only(text):
    """v1.4.21: emoji (and joiners) only. Not a short word, digits, punctuation, or an email start."""
    s = re.sub(r'\s+', '', str(text or ''))
    if not s or len(s) > 32:
        return False
    saw = False
    for ch in s:
        cat = unicodedata.category(ch)
        if cat == 'So':
            saw = True
            continue
        if cat in ('Mn', 'Mc', 'Me', 'Cf', 'Sk'):
            continue
        return False
    return saw


def _prompt_recent(t):
    return t - float(load_state().get('lastPromptAt') or 0) <= PICK_WINDOW_S


def handle_pending_pick(text, cfg, now, poster, quoted=''):
    """v1.4.13: while a pick prompt is waiting, an answer to it ("2", "0188", "alex@") is checked against
    the pick FIRST and never routed as a new text (it used to become "Reply to <last texter>?"). The
    extension holds the message text and resolves a valid pick itself; one that reaches the host means
    the extension no longer has it (restarted, or the pick was out of range): say so, send nothing."""
    # v1.4.21: an emoji-only native Reply that names a contact is a send, even inside the 10-minute
    # prompt window. "ok", digits, and email starts stay prompt answers.
    # v1.4.27: a photo with no caption is not a prompt answer (empty text falls through below).
    # A quote on that photo routes like this emoji-only Reply.
    if emoji_only(text):
        directory = rc.build_directory(cfg, rc.load_roster())
        h, st = rc.parse_quote(quoted, cfg, directory)
        if st == 'match' and h:
            return None
    state = load_state()
    p = state.get('pendingNewContact')
    if not (isinstance(p, dict) and p.get('options') and now() - float(p.get('at') or 0) <= PICK_WINDOW_S):
        # v1.4.14/15: a short answer-like reply right after ANY prompt is an answer, never a text
        if short_answer(text) and now() - float(state.get('lastPromptAt') or 0) <= PICK_WINDOW_S:
            detail = 'That isn\u2019t a choice. Nothing was sent.'
            rc.log_event('pick_unresolved', resolved=False, pending=False)
            notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
            return {'dryRun': cfg['dryRun'], 'ok': True, 'skipped': detail}
        return None
    like, h = pick_answer(text, p['options'])
    if not like and not short_answer(text):
        return None
    name = rc.clean_label(p.get('name') or '') or 'name'
    if h:
        detail = 'That choice expired. Send it again, starting with %s:' % name
    else:
        detail = 'Reply 1\u2013%d, or no. Nothing was sent.' % len(p['options'])
    rc.log_event('pick_unresolved', resolved=bool(h))
    notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
    return {'dryRun': cfg['dryRun'], 'ok': True, 'skipped': detail}


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
        detail = 'Did you mean %s? Reply yes within 2 min.' % df['label']
        notify(cfg, poster, {'type': 'needs_confirm', 'label': df['label'], 'reason': 'new_contact', 'detail': detail,
                             'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsConfirm=True, label=df['label'], handle=df['handle'], reason='new_contact',
                    contact=df['label'], routedBy='contacts')
    if not matches and len(fuzzy) == 1:
        m = fuzzy[0]
        shown = _save_label(m, m['name'])
        _store_pending_new(m['handle'], shown, now(), prefix=name)
        _note_prompt(now())
        detail = 'Text %s (%s)? Reply yes within 2 min.' % (shown, rc.mask_handle(m['handle']))
        notify(cfg, poster, {'type': 'needs_confirm', 'label': shown, 'reason': 'new_contact', 'detail': detail,
                             'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsConfirm=True, label=shown, handle=m['handle'], reason='new_contact',
                    contact=shown, routedBy='contacts')
    matches, fuzzy = _merge_same_name(matches), _merge_same_name(fuzzy)   # v1.4.11: duplicate cards = one person
    if not matches and fuzzy:
        matches = fuzzy                          # several close names: list them (names only), send nothing
    if not matches and not (res is None and any(c.get('nameLike') and c.get('form', 'colon') == 'colon' for c in cands)):
        return dict(base, _fallback=True)        # not a name anyone knows: ordinary text
    if len(matches) == 1:
        m = dict(matches[0])
        hs = _rank_options(m.get('handles') or [{'handle': m['handle'], 'label': ''}], m['handle'])
        m['handle'] = hs[0]['handle']
        shown = _save_label(m, m['name'])
        if cfg['confirmNewContacts'] == 'exact-skip' and m.get('exact') and len(hs) == 1:
            # v1.4.1: "Jordan Rivera: hey" names exactly one Contacts card, by its full name, with one
            # number/email: no question to ask. The caller sends it and adds them as a contact.
            rc.log_event('new_contact_direct')
            return dict(base, _direct={'handle': m['handle'], 'name': shown, 'body': chosen['body']})
        hs = hs[:9]
        _store_pending_new(m['handle'], shown, now(), prefix=name,
                           options=[x['handle'] for x in hs] if len(hs) > 1 else None)
        _note_prompt(now())
        if len(hs) > 1:
            # v1.4.11: a pick prompt (masked handles only), never a dead end. 1 = the one used most recently.
            # v1.4.12: one choice per line; he may answer the index, a phone's last 4 digits or an email's start
            def opt(i, x):
                return '%d \u00b7 %s%s' % (i + 1, _short_mask(x['handle']), (' ' + x['label']) if x.get('label') else '')
            detail = 'Text %s at which?\n%s\nReply 1\u2013%d, or no.' % (
                shown, '\n'.join(opt(i, x) for i, x in enumerate(hs)), len(hs))
        else:
            detail = 'Text %s (%s)? Reply yes within 2 min.' % (shown, rc.mask_handle(m['handle']))
        notify(cfg, poster, {'type': 'needs_confirm', 'label': shown, 'reason': 'new_contact', 'detail': detail,
                             'at': rc.iso_utc(now())})
        r = dict(base, ok=False, needsConfirm=True, label=shown, handle=m['handle'], reason='new_contact',
                 contact=shown, routedBy='contacts')
        if len(hs) > 1:
            r['options'] = [x['handle'] for x in hs]
        return r
    if matches and ' ' in name.strip():
        # v1.4.11: he already typed a full name; never tell him to "start with the full name"
        names = ', '.join(x['name'] for x in matches[:3]) + (', \u2026' if len(matches) > 3 else '')
        detail = ('%s isn\u2019t on your list. Matches: %s. /add %s and the number.'
                  % (name, names, first))
    elif matches:
        detail = 'More than one %s. Start with %s: your message' % (first, matches[0]['name'])
    elif res is None:
        detail = ('%s isn\u2019t on your list, and Contacts didn\u2019t answer. /add %s and the number.'
                  % (first, first))
    else:
        detail = ('%s isn\u2019t on your list, and isn\u2019t in Contacts. /add %s and the number.'
                  % (first, first))
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
        detail = 'Nothing is waiting for yes. Send it again.'
    else:
        if not (block or add or recent):
            return None

        def fn():
            st = load_state()
            for k in ('pendingBlock', 'pendingAdd', 'pendingNewContact'):
                st.pop(k, None)
            rc.write_json_atomic(STATE_PATH, st)
        _with_host_lock(fn)
        detail = 'Cancelled.'
    rc.log_event('bare_confirm', word=word, pending=bool(block or add))
    notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(t)})
    return dict(base, ok=True, skipped=detail)


def handle_trash_reaction(msg, cfg, now, poster):
    """v1.4.5: the owner put a wastebasket on a relayed message. Find its sender from the bubble's
    "Label:" (like a quote; a group line "Family \u00b7 Sam: \u2026" means Sam, never the whole group) and ask
    "Block <label>?". Nothing is muted until yes / /block yes / a thumbs-up on the prompt. Relaying only."""
    base = {'dryRun': cfg['dryRun']}
    quoted = msg.get('quotedText') if isinstance(msg.get('quotedText'), str) else ''
    quoted = quoted[:500]
    d = rc.build_directory(cfg, rc.load_roster())
    by = {e['handle']: e for e in d}
    tgt = None
    h, st = rc.parse_quote(quoted, cfg, d)
    if st == 'match' and by[h]['kind'] == 'group':
        # the sender inside a group line: "Family \u00b7 Sam: hi" -> look at what follows the group name
        rest = re.sub(r'\s+', ' ', quoted).strip()
        i = rest.find('\u00b7')
        h2, st2 = rc.parse_quote(rest[i + 1:] if i >= 0 else '', cfg, [e for e in d if e['kind'] != 'group'])
        h, st = (h2, st2) if st2 == 'match' else (None, 'none')
    if st == 'match':
        tgt = by.get(h)
    if not tgt:
        detail = 'Couldn\u2019t tell whose message that was. /block name.'
        rc.log_event('needs_target', reason='trash-unresolved', action='block')
        notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'trash-unresolved', 'at': rc.iso_utc(now())})
        return dict(base, ok=False, needsTarget=True, error=detail)
    shown = rc.guess_label(tgt)
    _store_pending_block(tgt['handle'], tgt['label'], now())
    _note_prompt(now())
    detail = 'Stop relaying %s here? Messages stays as it is. Reply yes within 2 min.' % shown
    rc.log_event('needs_confirm', reason='trash-block')
    notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'confirm-block', 'at': rc.iso_utc(now())})
    return dict(base, ok=False, needsConfirm=True, label=tgt['label'])


def handle_reaction(msg, cfg, now, poster):
    """v1.4.2: the owner put a thumbs-up (confirm) or thumbs-down (cancel) on a pending prompt in the relay
    chat. The extension only sends this for a reaction it saw appear on the newest "Reply yes" prompt within
    2 minutes of it; a pending reply prompt is answered in the extension itself. Here: /block and /add.
    Nothing pending -> ignored silently (a reaction is never a message and never texted)."""
    word = msg.get('word') if msg.get('word') in ('yes', 'no', 'block') else None
    conv = str(msg.get('conversationId') or '').strip()
    if not word or (cfg.get('relayConversationId') and conv != cfg['relayConversationId']):
        return {'ok': False, 'error': 'not a relay-chat reaction'}
    if cfg['paused']:
        return {'ok': False, 'error': 'paused'}
    if word == 'block':
        return handle_trash_reaction(msg, cfg, now, poster)
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


_UI_PROMPT_RE = re.compile(
    r"reply yes within 2 min|^\s*who(?:\u2019s|'s| is) this for\?", re.I)


def is_relay_ui_text(text):
    """True for anything the relay itself authored in the owner's name: the UI_TAG prefix, or (belt and
    braces) the exact prompt phrasing the relay uses."""
    s = str(text or '').lstrip()
    return s.startswith(UI_TAG) or bool(_UI_PROMPT_RE.search(s[:300]))


def handle_send(msg, cfg, runner, sleeper, now, poster):
    text = msg.get('text')
    quoted = msg.get('quotedText') if isinstance(msg.get('quotedText'), str) else ''
    conv = str(msg.get('conversationId') or '').strip()[:120]
    dry = cfg['dryRun']
    base = {'dryRun': dry}

    def refuse(reason, label='', **f):
        rc.log_event('refused', reason=reason, **f)
        _discard_attachments(msg.get('attachments'))     # v1.4.6: a refused reply keeps no upload
        _tell_refusal(reason, label, cfg, poster, now)   # v1.4.6: he always hears why (text + \u26a0\ufe0f)
        return dict(base, ok=False, error=reason)

    if not cfg['relayConversationId']:
        return refuse('relayConversationId not set in config')
    if conv != cfg['relayConversationId']:
        return refuse('not the relay conversation')
    skipped_n = msg.get('attachmentsSkipped')
    skipped_n = skipped_n if isinstance(skipped_n, int) and not isinstance(skipped_n, bool) and 0 < skipped_n <= 10 else 0
    atts, att_err = _parse_attachments(msg.get('attachments'))
    if att_err:
        _discard_attachments(msg.get('attachments'))
        return refuse(att_err)
    if text is None and (atts or skipped_n):
        text = ''
    if isinstance(text, str) and not text.strip() and not atts and skipped_n and not cfg['paused']:
        # v1.4.27: the photo was seen and could not be read. Say so. Nothing goes out.
        _warn_photo('', cfg, poster, now)
        return refuse('attachments could not be read', attachments=skipped_n)
    if not isinstance(text, str) or (not text.strip() and not atts):
        return refuse('empty text')
    if is_relay_ui_text(text):
        # v1.4.6: a prompt the extension typed as the owner (tagged). Never a reply, never a command.
        rc.log_event('ui_text_ignored', textLength=len(text))
        return dict(base, ok=True, skipped='relay prompt')
    # v1.3.9: drop X Chat time/status chips an older extension may have appended ("hi\n9:07 AM\n\n9:07 AM").
    text = rc.strip_trailing_chips(text)
    quoted = rc.strip_trailing_chips(quoted)
    if len(text) > MAX_TEXT:
        return refuse('text too long', textLength=len(text))
    oc = parse_owner_command(text)            # /who /pause /resume /help /add /test /status /snooze, even while paused
    if oc and oc['cmd'] == 'test':            # a pre-1.3.8 extension forwards "/test" as a reply: never route it
        return relay_test.run_test({'send': oc['send']}, cfg, runner, sleeper, now, poster, via='send-path')
    if oc:
        return handle_owner_command(oc, quoted, cfg, now, poster, sleeper=sleeper)
    if cfg['paused']:
        return refuse('paused')
    bw = bare_confirm_word(text)
    if bw and not msg.get('confirmed'):
        r = handle_bare_confirm(bw, quoted, cfg, now, poster)
        if r is not None:                     # a confirm/cancel word answered a prompt: never an iMessage
            return r
    if '\u2022\u2022\u2022' in text or re.search(r'Reply 1\u2013\d, or no|Reply yes within 2 min', text) or rc.HEADER_LINE_RE.match(text):
        # v1.4.15: text that came from a prompt or a masked preview row is never his reply
        rc.log_event('refused_prompt_text', textLength=len(text))
        notify(cfg, poster, {'type': 'status', 'detail': '\u26a0\ufe0f That looked like a prompt, so nothing was sent.',
                             'at': rc.iso_utc(now())})
        return refuse('text from a prompt row', textLength=len(text))
    if not msg.get('confirmed'):
        r = handle_pending_pick(text, cfg, now, poster, quoted)
        if r is not None:                     # v1.4.13: an answer to a pick prompt is never a new text
            return r
    cmd = parse_command(text)
    if cmd:                                   # an explicit /block|/mute|/unblock never becomes an outgoing iMessage
        return handle_command(cmd, quoted, cfg, now, poster)
    hint = bare_command_hint(text, cfg)
    if hint:                                  # bare "block"/"unblock [known name]" -> nudge, send nothing (item N1)
        detail = 'Did you mean /%s? Nothing was sent.' % hint
        rc.log_event('bare_command_hint', verb=hint)
        notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'bare-command', 'at': rc.iso_utc(now())})
        return dict(base, ok=True, skipped='bare "%s" is not a command (use /%s)' % (hint, hint))
    if not cfg['contacts'] and not cfg['relayAll']:
        return refuse('no contacts in config', textLength=len(text))

    qlabel = _quote_label(msg)
    route = rc.route_reply(text, quoted[:500], cfg, rc.load_roster(), now(), quote_label=qlabel)
    probe = msg.get('quoteProbe')
    if isinstance(probe, str) and probe:
        # v1.4.3 quote diagnostics: text-free page structure around the owner's bubble (tags, roles,
        # sanitized test ids, attribute NAMES, 'Replied to' yes/no flags). Never any message text.
        rc.log_event('quote_probe', quoteSeen=bool(quoted.strip()), quoteResolved=route.get('by') == 'quote',
                     routedBy=route.get('by') or '', needsTarget=bool(route.get('needsTarget')),
                     confirmed=bool(msg.get('confirmed')), fingerprint=rc.clean_probe(probe),
                     quoteLabelSeen=bool(qlabel),
                     cards=msg.get('cards') if isinstance(msg.get('cards'), int) and 0 < msg.get('cards') <= 5 else 0)
    confirmed = bool(msg.get('confirmed'))
    hint = msg.get('handle')
    if hint and str(hint).startswith('group:'):
        hint_norm = str(hint)[:130]          # v1.4.5: a group key is kept as is
    else:
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
        group_ok = None
        if tgt and tgt.get('kind') == 'group':
            # v1.4.5: a group send needs the group prompt the host itself issued (same group, 2 min) and relay-all
            pend_g = _take_pending_group(hint_norm, now()) if cfg['relayAll'] else None
            if not pend_g:
                return refuse('confirmed group send without a pending group prompt', handle=hint_norm)
            group_ok = dict(tgt, guid=pend_g['guid'])
        if not tgt or (tgt.get('kind') == 'group' and not group_ok):
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
        if group_ok:
            if route.get('needsTarget') or route['contact']['handle'] != hint_norm:
                return refuse('confirmed group send no longer names that group', handle=hint_norm)
            contact = group_ok
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
        if not body.strip() and not atts:
            # v1.4.28: "Alex:" with a photo that could not be read still says the photo did not send.
            if skipped_n:
                _warn_photo(contact.get('label', ''), cfg, poster, now)
                return refuse('attachments could not be read', label=contact.get('label', ''), handle=handle)
            return refuse('empty text after the name prefix', label=contact.get('label', ''), handle=handle)
        if contact.get('kind') == 'group':
            # v1.4.5: a reply to a group (exact name or a quote of its message) is ALWAYS confirmed first.
            guid = group_guid_lookup(handle, sleeper, now)
            if not guid:
                detail = 'Can\u2019t reach the %s group right now. Nothing was sent.' % contact['label']
                rc.log_event('group_unavailable', routedBy=by, textLength=len(body))
                notify(cfg, poster, {'type': 'needs_target', 'detail': detail, 'reason': 'group-unavailable', 'at': rc.iso_utc(now())})
                return dict(base, ok=False, needsTarget=True, error=detail, reason='group-unavailable')
            _store_pending_group(handle, guid, contact['label'], now())
            detail = 'Reply to the %s group? Reply yes within 2 min.' % contact['label']
            rc.log_event('needs_confirm', routedBy=by, reason='group', textLength=len(body))
            _note_prompt(now())
            notify(cfg, poster, {'type': 'needs_confirm', 'label': contact['label'], 'reason': 'group',
                                 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, needsConfirm=True, guess=True, label=contact['label'], handle=handle,
                        reason='group', contact=contact['label'], routedBy=by)
        # W1: a directive aimed at someone other than the current conversation (or a dict-word bare form)
        # is held for a /yes confirmation. Label only -- the stripped body is never shown.
        if route.get('confirm'):
            c = route['confirm']
            alt = c.get('altLabel')
            detail = ('Send to %s instead of %s?' % (contact['label'], alt)) if alt else ('Send to %s?' % contact['label'])
            detail = detail + ' Reply yes within 2 min.'
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
        # v1.4.2: an unresolved quote, or a roster person the owner does not know yet, is always asked.
        # v1.4.30: a familiar roster person (many texts, or a send already confirmed) is not asked
        # on every bare reply. v1.4.15: never a last-texter send right after a prompt.
        unfamiliar = contact.get('kind') == 'roster' and not rc.roster_familiar(contact)
        must_ask = by == 'recent' and (route.get('quoteUnresolved') or unfamiliar or _prompt_recent(now()))
        if must_ask or (by == 'recent' and cfg['confirmGuessed'] != 'off'
                        and (cfg['confirmGuessed'] == 'always' or guess_is_risky(contact, cfg, now()))):
            shown = rc.guess_label(contact)          # v1.4.3: Contacts name, else masked handle + warning
            detail = 'Reply to %s? Reply yes within 2 min.' % shown
            rc.log_event('needs_confirm', routedBy=by, reason='guessed', textLength=len(body),
                         inContacts=shown == contact['label'])
            _note_prompt(now())
            notify(cfg, poster, {'type': 'needs_confirm', 'label': shown, 'reason': 'guessed',
                                 'detail': detail, 'at': rc.iso_utc(now())})
            return dict(base, ok=False, needsConfirm=True, guess=True, label=contact['label'],
                        handle=handle, reason='guessed', contact=contact['label'], routedBy=by)
    if not body.strip() and not atts:
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
    if msg.get('preflight') is True:
        # v1.4.16: the extension asks during the undo window. Every check and prompt above has run (a prompt is
        # already shown); a reply that would go out is only reported as held and is sent by the real request
        # after the window. Nothing is sent, deduped, rate-counted or discarded here.
        rc.log_event('preflight_held', routedBy=by, textLength=len(body))
        return dict(base, ok=True, held=True, **who)

    os.makedirs(rc.APP_DIR, exist_ok=True)
    with open(LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)     # serialize concurrent host processes
        state = load_state()
        t = now()
        recent = {k: v for k, v in (state.get('recent') or {}).items() if t - v < DEDUPE_WINDOW_S}
        window = [x for x in (state.get('sends') or []) if t - x < 60]
        fp = fingerprint(handle, body + ''.join('\n#' + a['sha256'] for a in atts))
        if fp in recent:
            rc.log_event('duplicate_dropped', handle=handle, textLength=len(body), dryRun=dry)
            _discard_attachments(msg.get('attachments'))
            return dict(base, ok=True, skipped='duplicate within 60s', **who)
        if len(window) >= cfg['maxSendsPerMinute']:
            rc.log_event('rate_limited', handle=handle, textLength=len(body), limit=cfg['maxSendsPerMinute'])
            _discard_attachments(msg.get('attachments'))
            if atts:
                _warn_photo(contact.get('label', ''), cfg, poster, now)
            return dict(base, ok=False, error='rate limited (%d per minute)' % cfg['maxSendsPerMinute'], **who)
        outgoing = (body + ('\n' + cfg['footer'] if cfg.get('footer') else '')) if body.strip() else ''
        recent[fp] = t
        window.append(t)
        state.update(recent=recent, sends=window)
        ok, err = True, ''
        pending_id = ''
        is_group = contact.get('kind') == 'group'
        pending_ids = []
        ui_on = bool(_UI['on'] and _UI['uuid'] and cfg.get('uiReactions', True))
        dest, tgt = (contact['guid'], 'chat') if is_group else (handle, 'participant')
        staged = []
        if not dry and atts:
            staged, err = _stage_attachments(atts)
            ok = err == ''
        if not dry and ok and outgoing:
            ok, err = rc.send_imessage(outgoing, dest, runner=runner, sleeper=sleeper, target=tgt)
            if not ok:
                for p in staged:
                    relay_media.remove_staged(p)
            if ok and cfg.get('deliveryCheck', True):
                pending_ids.append(rc.add_pending_send(handle, t, len(outgoing), label=contact['label'],
                                   note=('new contact, ' + _short_mask(handle)) if new_contact else '',
                                   chat_guid=contact['guid'] if is_group else '', x_uuid=_UI['uuid'], ui=ui_on))
        if not dry and ok and atts:
            # v1.4.6: each attachment is its own Messages send (file mode), after the text, same target
            sent_files, err = _send_attachments(atts, staged, dest, tgt, handle, contact, t, cfg, runner, sleeper,
                                               ui_on, pending_ids)
            ok = err == ''
            if not ok and (outgoing or sent_files):
                err = 'attachment_failed'       # part of the reply went out: never resend (keep the fingerprint)
        if not dry:
            if not ok:
                if err not in ('timeout', 'attachment_failed'):   # a clear failure: allow an immediate manual retry
                    recent.pop(fp, None)
                    window.pop()
                # a timeout is ambiguous (it may actually have sent): keep the fingerprint, no double-send
            pending_id = pending_ids[0] if pending_ids else ''
        rc.write_json_atomic(STATE_PATH, state)
    _discard_attachments(msg.get('attachments'))      # dry run / failure / leftovers: no upload outlives the request
    if new_contact and ok and not dry:
        try:
            _finish_new_contact(handle, new_contact['name'], cfg)
        except Exception:
            pass
    if ok and not dry and contact.get('kind') == 'roster':
        try:
            rc.note_roster_trusted(handle)
        except Exception:
            pass
    note = ('new contact, ' + _short_mask(handle)) if new_contact else ''
    if note:
        who['note'] = note
    if dry:
        rc.log_event('dry_run', handle=handle, contact=contact['label'], routedBy=by, textLength=len(body), outgoingLength=len(outgoing),
                     attachments=len(atts))
        if cfg['confirmSends']:
            notify(cfg, poster, {'type': 'sent', 'label': contact['label'], 'contactName': contact['name'], 'dryRun': True,
                                 'delivered': None, 'routedBy': by, 'at': rc.iso_utc(t)})
        who.pop('note', None)
        return dict({'ok': True, 'dryRun': True, 'textLength': len(body)}, **who)
    if not ok:
        rc.log_event('send_failed', textLength=len(body), error=err, attachments=len(atts))      # err is a fixed code, never raw text
        if atts:
            _warn_photo(contact.get('label', ''), cfg, poster, now)
        return dict({'ok': False, 'dryRun': False, 'error': rc.error_message(err), 'errorCode': err}, **who)
    # Delivery proof is verified by the watcher (it holds Full Disk Access); the host recorded a
    # body-free pending entry inside its lock above. The host never confirms delivery itself.
    sent_fields = {'attachments': len(atts), 'attachmentBytes': sum(a['size'] for a in atts)}
    if atts:
        sent_fields['kinds'] = sorted({a['kind'] for a in atts if a.get('kind')})
    rc.log_event('sent', routedBy=by, textLength=len(body), delivered=None, deliveryNote='pending', **sent_fields)
    # item 6: when deliveryCheck is on, the WATCHER posts the single confirmation (✓✓ delivered, or ✓ sent
    # after ~20s). The host only confirms here when delivery checking is off, so the owner still hears back.
    if cfg['confirmSends'] and not cfg.get('deliveryCheck', True):
        p = {'type': 'sent', 'label': contact['label'], 'contactName': contact['name'], 'dryRun': False,
             'delivered': None, 'routedBy': by, 'at': rc.iso_utc(t)}
        if note:
            p['note'] = note
        notify(cfg, poster, p)
    res = dict({'ok': True, 'dryRun': False, 'sent': True, 'delivered': None, 'deliveryCheck': 'pending'}, **who)
    if skipped_n:
        _warn_photo(contact.get('label', ''), cfg, poster, now)
        res['attachmentsSkipped'] = skipped_n
    if pending_id and _UI['on'] and _UI['uuid'] and cfg.get('uiReactions', True):
        # v1.4.6: \u2705 on HIS message right now (Messages accepted it); the watcher then skips the text line.
        r = {'emoji': '\u2705', 'uuid': _UI['uuid'], 'pendingId': pending_id}
        if len(pending_ids) > 1:
            r['pendingIds'] = list(pending_ids)     # text + attachments: one \u2705 covers them all
        _UI['react'].append(r)
        res['pendingId'] = pending_id
    if atts:
        res['attachments'] = len(atts)
    return res


# v1.4.6: every refusal on the reply path tells him why (fixed, body-free wording). A few already say so
# their own way, or are not about one of his replies at all.
_REFUSAL_SILENT = {'relayConversationId not set in config', 'not the relay conversation', 'attachments could not be read',
                   'refusing to echo a relayed message back to its sender', 'refusing to send one of my own relay messages'}
_REFUSAL_TEXT = {
    'empty text': 'There was nothing to send.',
    'empty text after the name prefix': 'Nothing came after \u201c{label}:\u201d.',
    'text too long': 'Over %d characters.' % MAX_TEXT,
    'paused': 'The relay is paused. /resume',
    'no contacts in config': 'No contacts are set up yet. /add name and the number.',
    'confirmed reply without a target handle': 'That yes expired. Send it again.',
    'confirmed group send without a pending group prompt': 'That yes expired. Send it again.',
    'confirmed handle is not a current recipient': 'That yes expired. Send it again.',
    'confirmed group send no longer names that group': 'That no longer names the group. Send it again.',
    'the routed recipient does not match the handle the extension reported': 'The recipient changed. Send it again.',
    'refusing to relay to the owner handle': 'That would text your own number. /test send',
    'bad attachment': 'The attachment was damaged. Attach it again.',
    'attachment type not allowed': '',
}


def _tell_refusal(reason, label, cfg, poster, now):
    if reason in _REFUSAL_SILENT:
        return
    if reason == 'attachment type not allowed':
        _warn_photo(label, cfg, poster, now)
        return
    detail = _REFUSAL_TEXT.get(reason)
    if detail == '':
        return
    if detail is None:
        if reason.startswith('attachment too large'):
            detail = 'The attachment is over 25 MB.'
        elif reason.startswith('too many attachments'):
            detail = 'Too many attachments.'
        else:
            detail = 'That reply was not sent.'
    detail = detail.replace('{label}', rc.clean_label(label) or 'name')
    if _UI['on'] and _UI['uuid'] and cfg.get('uiReactions', True):
        _UI['react'].append({'emoji': '\u26a0\ufe0f', 'uuid': _UI['uuid']})    # instant, on his own message
    try:
        notify(cfg, poster, {'type': 'refused', 'reason': 'not_sent', 'detail': detail, 'at': rc.iso_utc(now())})
    except Exception:
        pass


def _parse_attachments(raw):
    """v1.4.6: validate the attachment list the extension sends with a reply (metadata only; the bytes
    already arrived as attach_chunk uploads). Returns (list, error)."""
    if raw is None:
        return [], ''
    if not isinstance(raw, list) or len(raw) > relay_media.MAX_OUTBOUND_FILES:
        return [], 'too many attachments (limit %d)' % relay_media.MAX_OUTBOUND_FILES
    out = []
    for a in raw:
        if not isinstance(a, dict) or not relay_media.valid_upload_id(a.get('uploadId')):
            return [], 'bad attachment'
        size = a.get('size')
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= relay_media.MAX_OUTBOUND_BYTES:
            return [], 'attachment too large (limit %s)' % relay_media.fmt_size(relay_media.MAX_OUTBOUND_BYTES)
        sha = str(a.get('sha256') or '').lower()
        if not relay_media.SHA_RE.match(sha):
            return [], 'bad attachment'
        mime = str(a.get('mime') or '')[:80]
        kind = relay_media.outbound_kind(mime)
        if not kind:
            return [], 'attachment type not allowed'
        out.append({'uploadId': a['uploadId'], 'size': size, 'sha256': sha, 'mime': mime, 'kind': kind,
                    'name': relay_media.safe_name(a.get('name'), mime)})
    return out, ''


def _discard_attachments(raw):
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict):
            relay_media.discard_upload(a.get('uploadId'))


def _stage_attachments(atts):
    """Verify every upload (size + sha256) and move it to the Messages-readable stage BEFORE anything is
    sent, so a bad attachment sends nothing at all. Returns (staged_paths, errcode)."""
    staged = []
    for a in atts:
        try:
            staged.append(relay_media.stage_for_send(relay_media.take_upload(a['uploadId'], a['size'], a['sha256']), a['name']))
        except (ValueError, OSError):
            for p in staged:
                relay_media.remove_staged(p)
            rc.log_event('attachment_rejected', size=a['size'], mime=a['mime'].split('/')[0])
            return [], 'attachment_unavailable'
    return staged, ''


def _send_attachments(atts, staged, dest, tgt, handle, contact, t, cfg, runner, sleeper, ui_on, pending_ids):
    """Send each staged file as one Messages file send. Returns (files_sent, errcode). A staged copy is
    deleted on failure right away, else by the watcher once delivery resolves (or by the sweep)."""
    sent = 0
    for i, path in enumerate(staged):
        ok, err = rc.send_imessage(path, dest, runner=runner, sleeper=sleeper, target=tgt, mode='file')
        if not ok:
            for p in staged[i:]:
                relay_media.remove_staged(p)
            return sent, err
        sent += 1
        if cfg.get('deliveryCheck', True):
            pending_ids.append(rc.add_pending_send(handle, t, 0, label=contact['label'],
                               chat_guid=contact['guid'] if contact.get('kind') == 'group' else '',
                               x_uuid=_UI['uuid'], ui=ui_on, part='file', staged_path=path))
        # deliveryCheck off: no watcher record; the sweep removes the staged copy after SEND_STAGE_TTL_S
    return sent, ''


def handle_attach_chunk(msg, cfg, now):
    """v1.4.6: one in-order chunk of an attachment the owner sent in the relay chat (bytes only; never logged)."""
    if cfg['paused'] or not cfg.get('relayAttachments', True):
        return {'ok': False, 'error': 'attachments are off'}
    return relay_media.stage_chunk(msg.get('uploadId'), msg.get('seq'), msg.get('data'), now=now())


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


def _quote_label(msg):
    """v1.4.24: a sender label the extension read off the 💬 header above the quoted bubble. Label only."""
    ql = msg.get('quoteLabel') if isinstance(msg, dict) else ''
    if not isinstance(ql, str) or '\n' in ql or '\r' in ql:
        return ''
    return re.sub(r'\s+', ' ', ql).strip()[:80]


# v1.4.24: the extension saw the owner's send and the host never answered. Exact line, via status detail.
UNACKED_LINE = '\u26a0\ufe0f Didn\u2019t send. Reload X Chat and retype.'
# v1.4.28: a photo was seen and did not go out. With a person, name them. With no name, the short line.
PHOTO_MISS = '\u26a0\ufe0f photo didn\u2019t send to %s, send it from Messages'
PHOTO_MISS_BARE = '\u26a0\ufe0f photo didn\u2019t send'


def photo_miss_line(label):
    who = rc.clean_label(label)
    if not who:
        return PHOTO_MISS_BARE
    return PHOTO_MISS % who


def _warn_photo(label, cfg, poster, now):
    """One health line. Never the message text, never the image."""
    try:
        notify(cfg, poster, {'type': 'status', 'detail': photo_miss_line(label), 'at': rc.iso_utc(now())})
    except Exception:
        pass


# v1.4.21: the extension saw a reply and will not send it. Fixed sentences, never the message.
_SKIP_REASON = {
    'empty': 'There was nothing to send.',
    'paused': 'The relay is paused. /resume',
    'no_contacts': 'No contacts are set up yet. /add name and the number.',
    'host_down': 'The Mac helper isn\u2019t connected.',
    'other': 'That reply was not sent.',
}


def handle_signal(msg, cfg, now, poster):
    """A browser-side condition (item 6) the host turns into one body-free webhook. No message text."""
    status = str(msg.get('status') or '')
    if status == 'skip_mail_card':
        # v1.4.30: a real Mail card. One fingerprint in the log. A fixed line so the skip is not silent.
        rc.log_event('skip_mail_card', fingerprint=rc.clean_probe(msg.get('fingerprint')))
        notify(cfg, poster, {'type': 'status',
                             'detail': 'Left that for Mail in X Chat. Nothing was texted.',
                             'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'unacked':
        rc.log_event('signal', status='unacked')
        notify(cfg, poster, {'type': 'status', 'detail': UNACKED_LINE, 'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'skipped':
        raw = re.sub(r'[^a-z_]', '', str(msg.get('reason') or '').lower())[:40]
        reason = raw if raw in _SKIP_REASON else 'other'
        rc.log_event('signal', status='skipped', reason=reason)
        notify(cfg, poster, {'type': 'refused', 'reason': 'not_sent', 'detail': _SKIP_REASON[reason],
                             'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'wrong_account':
        rc.log_event('signal', status='wrong_account')
        # Unique id per wrong-account EPISODE (the extension sends the episode timestamp), so a second
        # episode within 24h is not suppressed by the routine's id dedupe; repeats inside one episode
        # share the id and stay deduped (item 6).
        at = str(msg.get('at') or '')
        ep_id = ('wrong_account:' + at) if at else 'wrong_account'
        notify(cfg, poster, {'type': 'wrong_account', 'id': ep_id, 'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'undo_cancelled':
        rc.log_event('signal', status='undo_cancelled')    # v1.4.6: 🗑️ inside the undo window; nothing was sent
        return {'ok': True}
    if status == 'guess_cancelled':
        # v1.4.1: the owner answered a reply prompt with "no" / "cancel" (the extension dropped the text)
        label = str(msg.get('label') or '').strip()
        rc.log_event('signal', status='guess_cancelled')
        detail = ('Cancelled. Nothing sent to %s.' % label) if label else 'Cancelled.'
        notify(cfg, poster, {'type': 'status', 'detail': detail, 'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'guess_expired':
        # the extension's 2-minute confirm window lapsed with no "/yes"; tell the owner, body-free (item 3)
        label = str(msg.get('label') or '').strip()
        rc.log_event('signal', status='guess_expired')
        notify(cfg, poster, {'type': 'guess_expired', 'label': label, 'at': rc.iso_utc(now())})
        return {'ok': True}
    if status == 'photo_held':
        # v1.4.28: a lone photo was seen and is waiting for its caption. Counts only. No bytes, no text.
        try:
            count = int(msg.get('count') or 0)
        except (TypeError, ValueError):
            count = 0
        try:
            nbytes = int(msg.get('bytes') or 0)
        except (TypeError, ValueError):
            nbytes = 0
        raw_kinds = msg.get('kinds') if isinstance(msg.get('kinds'), list) else []
        kinds = sorted({k for k in raw_kinds if k in ('jpeg', 'png', 'gif', 'webp', 'heic')})
        rc.log_event('photo_held', count=max(0, min(10, count)), bytes=max(0, min(250 * 1024 * 1024, nbytes)), kinds=kinds)
        return {'ok': True}
    if status == 'photo_miss':
        # v1.4.27: a photo was seen and never handed off (grab failed, or the short pairing window ended).
        rc.log_event('signal', status='photo_miss')
        _warn_photo('', cfg, poster, now)
        return {'ok': True}
    if status == 'typed_mismatch':
        # v1.4.14: the extension read his bubble back differently from what he typed and sent nothing
        rc.log_event('signal', status='typed_mismatch')
        notify(cfg, poster, {'type': 'status', 'detail': '\u26a0\ufe0f That didn\u2019t match what you typed. Send it again.',
                             'at': rc.iso_utc(now())})
        return {'ok': True}
    if status in ('test_seen', 'test_unseen'):
        # v1.3.8 /test: the extension saw (or gave up waiting for) the routine's report. Local only.
        return {'ok': relay_test.record_routine(msg.get('testId'), status == 'test_seen', now)}
    return {'ok': False, 'error': 'unknown signal'}


def handle_ui_ack(msg, cfg, now, poster):
    """v1.4.6: the extension reports whether a UI action worked. ok=False on a prompt posts the routine
    copy right away; ok=False on a \u2705 leaves the watcher's normal text confirmation in place."""
    ok = msg.get('ok') is True
    step = re.sub(r'[^a-z_-]', '', str(msg.get('step') or ''))[:30]
    pid = re.sub(r'[^0-9a-f]', '', str(msg.get('pendingId') or ''))[:32]
    note = re.sub(r'[^0-9a-f]', '', str(msg.get('noteId') or ''))[:32]
    aid = re.sub(r'[^0-9a-f]', '', str(msg.get('actionId') or ''))[:32]
    lat = msg.get('latencyMs')
    lat = int(lat) if isinstance(lat, (int, float)) and 0 <= lat < 600000 else None
    more = msg.get('pendingIds') if isinstance(msg.get('pendingIds'), list) else []
    for extra in more[:12]:
        e = re.sub(r'[^0-9a-f]', '', str(extra or ''))[:32]
        if e and e != pid:
            rc.update_pending(e, uiConfirmed=ok, uiFailed=not ok)
    if pid:
        rc.update_pending(pid, uiConfirmed=ok, uiFailed=not ok)
        rc.log_event('ui_confirm', ok=ok, step=step, **({'latencyMs': lat} if lat is not None else {}))
    if note:
        path = os.path.join(NOTIFY_RETRY_DIR, note + '.json')
        item = rc.read_json(path, {}) or {}
        payload = item.get('payload') if isinstance(item.get('payload'), dict) else None
        if not ok and payload is not None and not cfg['paused']:
            notify(cfg, poster, payload)          # _UI is off here, so this posts (or queues a retry)
        try:
            os.unlink(path)
        except OSError:
            pass
        rc.log_event('ui_prompt', ok=ok, step=step, type=(payload or {}).get('type'),
                     **({'latencyMs': lat} if lat is not None else {}))
    if aid:
        relay_ui.ack(aid, ok, step)
    return {'ok': True}


def handle_request(msg, cfg=None, runner=subprocess.run, sleeper=time.sleep, now=time.time,
                   poster=rc.post_json):
    _ui_reset(msg)
    try:
        res = _handle_request(msg, cfg, runner, sleeper, now, poster)
        if isinstance(res, dict) and _UI['on']:
            u = _ui_out()
            if u:
                res['ui'] = u
        return res
    finally:
        _ui_reset(None)


def _handle_request(msg, cfg, runner, sleeper, now, poster):
    cfg = cfg if cfg is not None else rc.load_config()
    if not isinstance(msg, dict):
        return {'ok': False, 'error': 'bad request'}
    typ = msg.get('type')
    if typ == 'ui_ack':
        return handle_ui_ack(msg, cfg, now, poster)
    if typ == 'attach_chunk':
        return handle_attach_chunk(msg, cfg, now)
    if typ == 'attach_discard':
        relay_media.discard_upload(msg.get('uploadId'))
        return {'ok': True}
    if typ == 'ui_poll':
        if cfg['paused'] or not cfg.get('uiReactions', True):
            return {'ok': True, 'actions': []}
        return {'ok': True, 'actions': relay_ui.take(limit=5)}
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
