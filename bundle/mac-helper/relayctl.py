#!/usr/bin/python3
"""
relayctl: the one command the bot (or you) uses to operate iMessage Relay on the Mac.
Every command prints one JSON object. Message bodies are never printed or stored.

  relayctl status                         health: config, watcher, LaunchAgent, host manifests, Messages
  relayctl pause | resume                 stop / restart relaying in BOTH directions
  relayctl dry-run on|off                 off = replies really send
  relayctl quiet-hours HH:MM HH:MM [--tz Area/City] | off
  relayctl auto-reopen on|off             reopen Messages hidden if it gets closed (opt-in)
  relayctl relay-all on|off               on = every inbound text (full inbox); off = contacts only
  relayctl mute HANDLE_OR_LABEL | unmute HANDLE_OR_LABEL   never forward this sender
  relayctl owner-handles add|remove HANDLE | list | clear   the owner's own numbers/emails (all aliases)
  relayctl owner-handles detect [--apply]  read them from chat.db (sent rows' account; no text) via the watcher
  relayctl roster [--days N]              everyone relayed (labels, handles, last time, can-reply); no text
  relayctl set KEY VALUE                  relayConversationId, footer, webhookUrl, ownerHandle,
                                          ownerXHandle, ownerXUserId, senderXHandle, maxSendsPerMinute, deliveryCheck,
                                          pollSeconds, defaultCountryCode, replyWindowMinutes,
                                          ambiguousSeconds, confirmSends, rosterDays, skipShortCodes
  relayctl set-webhook --url URL --token-stdin   set webhookUrl + webhookToken together; the key is
                                          read from stdin so it never lands in argv or shell history
  relayctl contacts                       list opted-in contacts with their labels
  relayctl add-contact --name N --handle H [--label L]   (label: the short name shown and used in replies)
  relayctl label HANDLE_OR_LABEL NEW_LABEL
  relayctl remove-contact HANDLE_OR_LABEL
  relayctl recent-senders [--days 30] [--limit 20]   (handles + counts + Contacts names; no text)
  relayctl selftest [--direct] [--wait 150]          send "Relay is set up ✅" to ownerHandle, confirm delivery
  relayctl setup [--owner-x-handle H] [--sender-x-handle H] [--owner-x-user-id N] [--relay-conversation-id ID]
                 [--relay-all on|off] [--webhook-url URL [--webhook-key-stdin | --webhook-key-prompt]]
                 [--owner-handle H] [--open-windows]
                                          guided setup in one locked edit; prints what it detected and the
                                          owner's checklist. Never changes dryRun, quiet hours or contacts.
  relayctl test-result                    the last /test run from X Chat (steps only, no text)
  relayctl version
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_common as rc  # noqa: E402
import relay_setup  # noqa: E402

JOBS_DIR = os.path.join(rc.APP_DIR, 'jobs')
SELFTEST_DIR = os.path.join(rc.APP_DIR, 'selftest')
STATUS_PATH = os.path.join(rc.APP_DIR, 'status.json')
LABEL = 'com.imessage-relay.watch'
HOST_NAME = 'com.imessage_relay.host'
BROWSER_DIRS = {
    'Chrome': 'Google/Chrome', 'Chrome Beta': 'Google/Chrome Beta', 'Chrome Canary': 'Google/Chrome Canary',
    'Chromium': 'Chromium', 'Brave': 'BraveSoftware/Brave-Browser', 'Edge': 'Microsoft Edge', 'Arc': 'Arc/User Data',
}
SETTABLE = {
    'footer': str, 'webhookUrl': str, 'ownerHandle': str, 'ownerXHandle': str,
    'senderXHandle': str, 'ownerXUserId': str, 'maxSendsPerMinute': int, 'deliveryCheck': 'bool', 'pollSeconds': int,
    'defaultCountryCode': str, 'relayConversationId': str, 'replyWindowMinutes': int, 'ambiguousSeconds': int,
    'confirmSends': 'bool', 'rosterDays': int, 'skipShortCodes': 'bool', 'redactCodes': 'bool',
}


class CtlError(Exception):
    pass


# ---------- config editing (raw file, so unknown keys survive) ----------
def edit_config(fn):
    # Take the SAME lock the native host holds for its config/state writes (item N5), so a slow
    # read-modify-write here can never clobber a mute the host added meanwhile (or vice versa).
    def _do():
        raw = rc.read_raw_config()
        if not raw:
            raw = json.loads(json.dumps(rc.DEFAULT_CONFIG))
        fn(raw)
        clean = rc.sanitize_config(raw)          # validate + normalize before writing
        raw['contacts'] = [dict({'name': c['name'], 'handle': c['handle']}, **({'label': r['label']} if r.get('label') else {}))
                           for c, r in zip(clean['contacts'], _explicit_labels(raw, clean))]
        if raw.get('ownerHandle'):
            raw['ownerHandle'] = clean['ownerHandle']
        rc.write_json_atomic(rc.CONFIG_PATH, raw)
        return rc.load_config()
    return rc.with_host_lock(_do)


def _explicit_labels(raw, clean):
    """For each sanitized contact, the raw entry's explicit label (kept only if it survived)."""
    cc = clean['defaultCountryCode']
    by_h = {rc.normalize_handle(c.get('handle'), cc): c for c in raw.get('contacts') or [] if isinstance(c, dict)}
    out = []
    for c in clean['contacts']:
        r = by_h.get(c['handle'], {})
        lab = rc.clean_label(r.get('label'))
        out.append({'label': lab} if lab and lab == c['label'] else {})
    return out


def parse_bool(v):
    b = rc.truthy(v, None)
    if b is None:
        raise CtlError('expected on/off, true/false, yes/no')
    return b


def public_config(cfg):
    c = dict(cfg)
    c['webhookToken'] = 'set' if cfg.get('webhookToken') else ''
    c['webhookUrl'] = 'set' if cfg.get('webhookUrl') else ''
    return c


def cmd_set(key, value):
    if key not in SETTABLE:
        raise CtlError('not settable: %s (allowed: %s)' % (key, ', '.join(sorted(SETTABLE))))
    typ = SETTABLE[key]
    if typ == 'bool':
        v = parse_bool(value)
    elif typ is int:
        try:
            v = int(value)
        except ValueError:
            raise CtlError('%s needs a number' % key)
    else:
        v = str(value)
    if key == 'ownerHandle' and v and not rc.valid_handle(rc.normalize_handle(v)):
        raise CtlError('ownerHandle is not a valid phone number or email')
    if key == 'ownerXUserId':
        v = __import__('re').sub(r'[^0-9]', '', v)
        if value and not v:
            raise CtlError('ownerXUserId is the owner account numeric id (digits only); get it from get_users_me')
    if key == 'relayConversationId' and v and not rc.CONV_RE.match(v):
        raise CtlError('relayConversationId looks wrong (copy it from https://x.com/i/chat/<id>)')
    if key == 'webhookUrl' and v and not v.startswith('https://'):
        raise CtlError('webhookUrl must start with https://')
    cfg = edit_config(lambda raw: raw.__setitem__(key, v))
    return {'ok': True, 'set': key, 'config': public_config(cfg)}


def cmd_set_webhook(url, token_stdin=False, stdin=None):
    """Set webhookUrl and webhookToken together. The key is ALWAYS read from stdin (--token-stdin) so
    it never appears in argv, a command string, or shell history; the bot pipes it in after the owner
    pastes the URL. There is deliberately no --token flag."""
    url = (url or '').strip()
    if not url.startswith('https://'):
        raise CtlError('webhookUrl must start with https://')
    if not token_stdin:
        raise CtlError('the webhook key must be provided on stdin: relayctl set-webhook --url URL --token-stdin')
    token = (stdin or sys.stdin).read().strip()
    if not token:
        raise CtlError('empty webhook key')
    cfg = edit_config(lambda raw: raw.update({'webhookUrl': url, 'webhookToken': token}))
    return {'ok': True, 'set': ['webhookUrl', 'webhookToken'], 'config': public_config(cfg)}


def cmd_add_contact(name, handle, label=''):
    cfg = rc.load_config()
    cc = cfg['defaultCountryCode']
    h = rc.normalize_handle(handle, cc)
    if not rc.valid_handle(h):
        raise CtlError('not a valid phone number or email: use digits like +1 555 555 0123, or an email')
    if h in rc.owner_handle_set(cfg):
        raise CtlError("that is the owner's own handle; it cannot be a relay contact (it would loop)")
    lab = rc.clean_label(label)
    if label and lab != label.strip():
        raise CtlError('a label cannot contain ":" or line breaks, and has at most %d characters' % rc.MAX_LABEL)
    if lab and any(c['label'].lower() == lab.lower() and c['handle'] != h for c in cfg['contacts']):
        raise CtlError('label "%s" is already used by another contact' % lab)

    def fn(raw):
        lst = [c for c in raw.get('contacts') or [] if rc.normalize_handle(c.get('handle'), cc) != h]
        entry = {'name': name.strip(), 'handle': h}
        if lab:
            entry['label'] = lab
        lst.append(entry)
        raw['contacts'] = lst
    cfg = edit_config(fn)
    return {'ok': True, 'contact': rc.contact_by_handle(cfg, h), 'labels': [c['label'] for c in cfg['contacts']]}


def cmd_label(key, label):
    cfg = rc.load_config()
    c = find_contact(cfg, key)
    return cmd_add_contact(c['name'], c['handle'], label)


def find_contact(cfg, key):
    h = rc.normalize_handle(key, cfg['defaultCountryCode'])
    k = key.strip().lower()
    hits = [c for c in cfg['contacts'] if c['handle'] == h or c['label'].lower() == k or c['name'].lower() == k]
    if not hits:
        raise CtlError('no matching contact')
    if len(hits) > 1:
        raise CtlError('more than one contact matches; use the label or the handle')
    return hits[0]


def cmd_remove_contact(key):
    cfg = rc.load_config()
    target = find_contact(cfg, key)
    keep = [c for c in rc.read_raw_config().get('contacts') or []
            if rc.normalize_handle(c.get('handle'), cfg['defaultCountryCode']) != target['handle']]
    cfg = edit_config(lambda raw: raw.__setitem__('contacts', keep))
    return {'ok': True, 'removed': target['label'], 'contacts': len(cfg['contacts'])}


# ---------- relay-all, mute, roster ----------
def resolve_handle(cfg, key):
    """A label/name from contacts + roster, or a phone number/email, -> normalized handle."""
    d = rc.build_directory(cfg, rc.load_roster())
    k = key.strip().lower()
    hits = {e['handle'] for e in d if e['kind'] != 'group' and (e['label'].lower() == k or e['name'].lower() == k)}
    if len(hits) == 1:
        return hits.pop()
    if len(hits) > 1:
        raise CtlError('more than one person matches; use the label or the number')
    h = rc.normalize_sender(key, cfg['defaultCountryCode'])
    if rc.valid_handle(h) or rc.is_short_code(key):
        return h
    raise CtlError('no matching person; use a label from `relayctl roster` or a phone number/email')


def cmd_mute(key, on):
    cfg = rc.load_config()
    h = resolve_handle(cfg, key)

    def fn(raw):
        cur = [rc.normalize_sender(x, cfg['defaultCountryCode']) for x in raw.get('mute') or [] if isinstance(x, str)]
        cur = [x for x in cur if x != h]
        if on:
            cur.append(h)
        raw['mute'] = cur
    cfg = edit_config(fn)
    return {'ok': True, 'muted' if on else 'unmuted': h, 'mute': cfg['mute']}


def cmd_owner_handles(action, value=None, apply=False):
    """Owner's own handles (every alias/number the owner texts from). The relay never forwards these
    and refuses to send to them. `add`/`remove` one handle, `list`, or `clear`."""
    cfg = rc.load_config()
    cc = cfg['defaultCountryCode']
    cur = list(rc.owner_handle_set(cfg))
    if action == 'detect':
        res = run_job({'type': 'detect_owner_handles', 'apply': apply}, wait_s=20)
        res.setdefault('applied', apply and res.get('ok', False))
        return res
    if action == 'list':
        return {'ok': True, 'ownerHandles': sorted(cur)}
    if action == 'clear':
        cfg = edit_config(lambda raw: raw.__setitem__('ownerHandles', [cfg['ownerHandle']] if cfg.get('ownerHandle') else []))
        return {'ok': True, 'ownerHandles': cfg['ownerHandles']}
    h = rc.normalize_handle(value, cc)
    if not rc.valid_handle(h):
        raise CtlError('owner-handles add/remove needs a phone number or email')
    def fn(raw):
        s2 = [x for x in (raw.get('ownerHandles') or []) if isinstance(x, str)]
        s2 = [x for x in s2 if rc.normalize_handle(x, cc) != h]
        if action == 'add':
            s2.append(h)
        raw['ownerHandles'] = s2
    cfg = edit_config(fn)
    return {'ok': True, action + 'ed': h, 'ownerHandles': cfg['ownerHandles']}


def require_owner_identity(cfg):
    """ownerHandles[] (all aliases) and ownerXUserId must be set before relay-all or going live:
    the extension's owner gate needs ownerXUserId, and the watcher needs every owner alias to skip."""
    if not cfg.get('ownerXUserId'):
        raise CtlError('set the owner account id first: relayctl set ownerXUserId <numeric id from get_users_me>')
    if not rc.owner_handle_set(cfg):
        raise CtlError('add the owner\'s handles first: relayctl owner-handles add "<owner number or email>" (all aliases)')


EXT_SEEN_PATH = os.path.join(rc.APP_DIR, 'ext_seen.json')
TEST_LAST_PATH = os.path.join(SELFTEST_DIR, 'test-last.json')
FDA_PANE = 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles'


def extension_seen(now=time.time):
    """When the extension last asked the host for its config, and its version (host writes this)."""
    e = rc.read_json(EXT_SEEN_PATH, {}) or {}
    at = float(e.get('at') or 0)
    return {'seen': bool(at), 'agoSeconds': int(now() - at) if at else None, 'version': str(e.get('extVersion') or '')}


def setup_checklist(cfg, now=time.time):
    """The owner's whole to-do list after the one Mac command, with each item's known state."""
    reads = rc.watcher_chat_db_state()['state']
    ext = extension_seen(now)
    fresh = ext['seen'] and ext['agoSeconds'] is not None and ext['agoSeconds'] < 180
    ext_ok = fresh and ext['version'] == rc.VERSION
    items = [
        {'id': 'fda', 'done': reads == 'ok',
         'text': "Full Disk Access: in the System Settings window that opened, drag 'iMessage Relay Watcher' "
                 "(Finder is showing it) into the list and switch it on."},
        {'id': 'extension', 'done': ext_ok,
         'text': ('Extension: in chrome://extensions click the reload arrow on "iMessage Relay".' if ext['seen'] else
                  'Extension: in chrome://extensions turn on Developer mode, then drag the "extension" folder '
                  '(Finder is showing it) onto the page.')},
        {'id': 'reload_chat', 'done': False,
         'text': 'Press Cmd+R on your x.com/i/chat tab (unlock X Chat with your passcode if it asks).'},
        {'id': 'test', 'done': False, 'text': 'Type /test in the relay chat; I will check every step.'},
    ]
    return items


def _open_windows(cfg, runner=subprocess.run):
    """macOS only: open exactly the windows the checklist needs. Nothing is clicked for the owner."""
    if sys.platform != 'darwin':
        return []
    opened = []
    app = watcher_app()
    cmds = []
    if rc.watcher_chat_db_state()['state'] != 'ok':
        cmds += [('fda_settings', ['/usr/bin/open', FDA_PANE]), ('watcher_in_finder', ['/usr/bin/open', '-R', app['path']])]
    cmds += [('chrome_extensions', ['/usr/bin/open', '-na', 'Google Chrome', '--args', 'chrome://extensions']),
             ('extension_in_finder', ['/usr/bin/open', '-R', os.path.join(rc.APP_DIR, 'extension')])]
    for name, argv in cmds:
        try:
            runner(argv, capture_output=True, timeout=10)
            opened.append(name)
        except Exception:
            pass
    return opened


def _read_key(stdin_flag, prompt_flag, stdin=None):
    if stdin_flag:
        return (stdin or sys.stdin).read().strip()
    if prompt_flag:
        import getpass
        return getpass.getpass('Webhook key (paste; hidden; never goes in chat): ').strip()
    return None


def cmd_setup(args, stdin=None, runner=subprocess.run, now=time.time):
    """Guided setup: every non-secret answer in one locked config edit, then auto-detection and the
    checklist. The webhook key only ever arrives on stdin or a hidden prompt (never argv)."""
    values = {'ownerXHandle': args.owner_x_handle, 'senderXHandle': args.sender_x_handle,
              'ownerXUserId': args.owner_x_user_id, 'relayConversationId': args.relay_conversation_id,
              'webhookUrl': args.webhook_url,
              'relayAll': None if args.relay_all is None else parse_bool(args.relay_all)}
    token = _read_key(args.webhook_key_stdin, args.webhook_key_prompt, stdin)
    if (args.webhook_key_stdin or args.webhook_key_prompt) and not token:
        raise CtlError('empty webhook key')
    if token and not args.webhook_url and not rc.load_config().get('webhookUrl'):
        raise CtlError('give --webhook-url together with the key')
    if args.owner_handle:
        cmd_set('ownerHandle', args.owner_handle)
    before = rc.read_raw_config() or {}
    try:
        out = relay_setup.apply_setup(values, token)
    except relay_setup.SetupError as e:
        raise CtlError(str(e))
    cfg = rc.load_config()
    detected = {}
    if not rc.owner_handle_set(cfg) and rc.watcher_chat_db_state()['state'] == 'ok':
        detected = run_job({'type': 'detect_owner_handles', 'apply': True}, wait_s=15)
        cfg = rc.load_config()
    out.update(ok=True, version=rc.VERSION, upgrade=bool(before.get('relayConversationId') or before.get('webhookUrl')),
               config={k: public_config(cfg).get(k) for k in ('ownerXHandle', 'senderXHandle', 'ownerXUserId',
                                                               'relayConversationId', 'webhookUrl', 'webhookToken',
                                                               'relayAll', 'dryRun', 'quietHours', 'ownerHandles')},
               contacts=len(cfg['contacts']), ownerHandlesDetected=detected.get('added', []) if detected else [],
               extension=extension_seen(now), checklist=setup_checklist(cfg, now))
    if cfg['relayAll'] is False and (rc.read_raw_config() or {}).get('relayAllPending'):
        out['relayAllPending'] = True
        out['note'] = ('relay-all turns on by itself once the watcher can read chat.db and finds your own '
                       'number (Full Disk Access).')
    if args.open_windows:
        out['opened'] = _open_windows(cfg, runner)
    return out


def cmd_test_result():
    rec = rc.read_json(TEST_LAST_PATH)
    if not isinstance(rec, dict):
        return {'ok': False, 'error': 'no /test run yet: type /test in the relay chat'}
    failed = [s.get('name') for s in rec.get('steps') or [] if s.get('ok') is False]
    return {'ok': not failed, 'failed': failed, 'test': rec}


def cmd_roster(days=None):
    cfg = rc.load_config()
    t = time.time()
    out = []
    for e in rc.build_directory(cfg, rc.load_roster()):
        if e['kind'] == 'contact' and e['lastInboundAt'] is None:
            continue
        at = e['lastInboundAt']
        if days and (at is None or t - at > days * 86400):
            continue
        reply = e['kind'] == 'contact' or (e['kind'] == 'roster' and cfg['relayAll'] and at is not None
                                           and t - at <= cfg['rosterDays'] * 86400)
        out.append({'label': e['label'], 'handle': e['handle'], 'kind': e['kind'],
                    'lastInboundAt': rc.iso_utc(at) if at else None, 'canReply': reply, 'muted': e['handle'] in cfg['mute']})
    out.sort(key=lambda e: e['lastInboundAt'] or '', reverse=True)
    return {'ok': True, 'relayAll': cfg['relayAll'], 'rosterDays': cfg['rosterDays'], 'count': len(out), 'roster': out}


# ---------- jobs for the watcher (it holds Full Disk Access) ----------
def run_job(job, wait_s=40, sleeper=time.sleep, now=time.time):
    jid = uuid.uuid4().hex[:12]
    job = dict(job, id=jid, createdAt=now())
    rc.write_json_atomic(os.path.join(JOBS_DIR, jid + '.job.json'), job)
    res_path = os.path.join(JOBS_DIR, jid + '.result.json')
    deadline = now() + wait_s
    while now() < deadline:
        res = rc.read_json(res_path)
        if isinstance(res, dict):
            os.unlink(res_path)
            return res
        sleeper(1)
    try:
        os.unlink(os.path.join(JOBS_DIR, jid + '.job.json'))
    except FileNotFoundError:
        pass
    return {'ok': False, 'error': 'watcher did not answer in %ds (is the LaunchAgent running? relayctl status)' % wait_s}


def cmd_recent_senders(days=30, limit=20, **kw):
    res = run_job({'type': 'recent_senders', 'days': days, 'limit': limit}, **kw)
    return res


# ---------- self-test ----------
def _confirm_delivery(handle, since, attempts=3, sleeper=time.sleep):
    """Poll chat.db for delivery of a just-sent message (blocking check_delivery was removed in v1.3).
    Returns (True,'delivered'), (False,message) on a real error, or (None, note) when this process has
    no Full Disk Access / the row has not appeared yet -- in which case the caller asks the watcher."""
    for i in range(max(1, attempts)):
        status, error, _ = rc.check_delivery_once(handle, since)
        if status == 'delivered':
            return True, 'delivered'
        if status == 'failed':
            return False, rc.error_message(error)
        if status == 'unavailable':
            return None, 'no chat.db access from this process'
        if i < attempts - 1:
            sleeper(1)
    return None, 'not confirmed yet'


def cmd_selftest(direct=False, wait_s=150, runner=subprocess.run, sleeper=time.sleep, now=time.time, confirm_wait_s=45):
    cfg = rc.load_config()
    owner = cfg.get('ownerHandle', '')
    if not rc.valid_handle(owner):
        raise CtlError('set ownerHandle first: relayctl set ownerHandle "+1 555 555 0123"')
    out = {'ok': False, 'to': owner, 'via': 'direct' if direct else 'chrome-host'}
    req_path = os.path.join(SELFTEST_DIR, 'request.json')
    res_path = os.path.join(SELFTEST_DIR, 'result.json')
    if direct:
        sent_at = now()
        ok, err = rc.send_imessage(rc.SELFTEST_TEXT, owner, runner=runner, sleeper=sleeper, timeout=120)
        rc.log_event('selftest_send', ok=ok, error=err, via='direct')
    else:
        sid = uuid.uuid4().hex[:12]
        rc.write_json_atomic(req_path, {'id': sid, 'handle': owner, 'createdAt': now()})
        deadline = now() + wait_s
        res = None
        while now() < deadline:
            r = rc.read_json(res_path)
            if isinstance(r, dict) and r.get('id') == sid:
                res = r
                break
            sleeper(1)
        try:
            os.unlink(req_path)
        except FileNotFoundError:
            pass
        if res is None:
            out['error'] = ('the extension did not pick up the self-test within %ds. Is the extension loaded and the '
                            'host connected (popup shows "Mac helper connected: yes")? Opening the popup triggers it. '
                            'Or run: relayctl selftest --direct' % wait_s)
            out['stage'] = 'send'
            return out
        ok, err, sent_at = bool(res.get('ok')), res.get('error', ''), float(res.get('sentAt') or now())
    if not ok:
        out.update(stage='send', error=rc.error_message(err), errorCode=err)
        if err in ('messages_busy', 'timeout'):
            out['hint'] = 'Messages was busy, often because the Automation prompt was not answered. Click OK on it and run selftest again.'
        if err == 'automation_denied':
            out['hint'] = 'Automation was denied. System Settings > Privacy & Security > Automation: allow it to control Messages.'
        return out
    out['sent'] = True
    delivered, note = _confirm_delivery(owner, sent_at - 5, attempts=3, sleeper=sleeper)
    out['confirmedBy'] = 'this process'
    if delivered is None:                       # no chat.db access here: ask the watcher (it has FDA)
        res = run_job({'type': 'confirm_delivery', 'handle': owner, 'since': sent_at - 5, 'attempts': 15},
                      wait_s=confirm_wait_s, sleeper=sleeper, now=now)
        delivered, note = res.get('delivered'), res.get('note') or res.get('error', '')
        out['confirmedBy'] = 'watcher'
    out.update(delivered=delivered, note=note, ok=delivered is True)
    rc.log_event('selftest_result', delivered=delivered, note=note)
    return out


# ---------- status ----------
def launchagent_loaded(runner=subprocess.run):
    try:
        r = runner(['/bin/launchctl', 'print', 'gui/%d/%s' % (os.getuid(), LABEL)], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return None


def host_manifests():
    base = os.path.join(rc.HOME, 'Library', 'Application Support')
    out = {}
    for name, sub in BROWSER_DIRS.items():
        p = os.path.join(base, sub, 'NativeMessagingHosts', HOST_NAME + '.json')
        if os.path.exists(p):
            m = rc.read_json(p, {}) or {}
            out[name] = {'path': m.get('path'), 'hostExists': bool(m.get('path')) and os.path.exists(m.get('path', '')),
                         'allowedOrigins': m.get('allowed_origins', [])}
    return out


WATCHER_APP_NAME = 'iMessage Relay Watcher.app'
WATCHER_BUNDLE_ID = 'com.imessage-relay.watcher'
FDA_BINARY_FILE = os.path.join(rc.APP_DIR, 'fda_binary.txt')
LAUNCH_AGENT_PLIST = os.path.join(rc.HOME, 'Library', 'LaunchAgents', LABEL + '.plist')


def _applet_from_plist():
    """The actual program the LaunchAgent starts (ProgramArguments[0]) — this is the FDA binary the
    owner granted, which on a hand-built install may live anywhere, not under ~/Applications."""
    try:
        import plistlib
        with open(LAUNCH_AGENT_PLIST, 'rb') as fh:
            args = plistlib.load(fh).get('ProgramArguments') or []
        return args[0] if args else ''
    except Exception:
        return ''


def watcher_app():
    """Resolve the watcher binary the install actually uses. install.sh records it in fda_binary.txt
    (and the LaunchAgent points at it); honor those first so a custom / pre-existing app path is never
    reported missing (item 12). Fall back to the default ~/Applications bundle."""
    default = os.path.join(rc.HOME, 'Applications', WATCHER_APP_NAME)
    recorded = ''
    try:
        with open(FDA_BINARY_FILE) as fh:
            recorded = fh.read().strip()
    except OSError:
        recorded = ''
    if recorded in ('', '(not built)'):
        recorded = ''
    # fda_binary.txt stores the .app bundle path; the LaunchAgent stores the applet executable.
    candidates = []
    if recorded:
        candidates.append(recorded if recorded.endswith('/applet') else os.path.join(recorded, 'Contents', 'MacOS', 'applet'))
        candidates.append(recorded)                       # in case a custom binary isn't a .app bundle
    # Only an actual applet executable counts as "the watcher app"; the Python-fallback LaunchAgent
    # runs /usr/bin/python3 directly (no FDA app), which must still report the app as missing.
    plist_prog = _applet_from_plist()
    if plist_prog.endswith('/Contents/MacOS/applet'):
        candidates.append(plist_prog)
    candidates.append(os.path.join(default, 'Contents', 'MacOS', 'applet'))
    for exe in candidates:
        if exe and os.path.isfile(exe):
            app = exe[:-len('/Contents/MacOS/applet')] if exe.endswith('/Contents/MacOS/applet') else exe
            return {'path': app, 'exists': True, 'binary': exe, 'source': 'recorded' if recorded else ('plist' if plist_prog else 'default')}
    return {'path': (recorded or default), 'exists': False,
            'binary': candidates[0] if candidates else '', 'source': 'recorded' if recorded else 'default'}


def cmd_status(runner=subprocess.run):
    cfg = rc.load_config()
    st = rc.read_json(STATUS_PATH, {}) or {}
    problems = []
    if cfg.get('_configError'):
        problems.append('config.json is not valid JSON: ' + cfg['_configError'])
    if not cfg['contacts']:
        if not cfg['relayAll']:
            problems.append('no contacts yet (or turn on relay-all)')
    if not cfg['relayConversationId']:
        problems.append('relayConversationId not set (the extension has no chat to watch; replies cannot route)')
    if not cfg['webhookUrl']:
        problems.append('webhookUrl not set (inbound messages cannot reach the bot)')
    if not cfg['ownerHandle']:
        problems.append('ownerHandle not set (self-test needs it)')
    # ownerXUserId powers the extension's owner-identity gate; on a live install (relaying enabled)
    # an unset id means the gate can only fall back to the account-switcher avatar (item 12).
    live = bool(cfg.get('relayConversationId')) and bool(cfg.get('webhookUrl')) and (cfg.get('relayAll') or cfg['contacts'])
    if live and not cfg.get('ownerXUserId'):
        problems.append('ownerXUserId not set: the extension owner gate is running on the avatar fallback '
                        'only. Set it with: relayctl set ownerXUserId <numeric id from get_users_me>')
    app = watcher_app()
    reads = rc.watcher_chat_db_state()
    if not app['exists']:
        problems.append('watcher app missing (%s): run install.sh again' % app['path'])
    if reads['state'] == 'denied' or st.get('chatDb') == 'unavailable':
        problems.append("watcher cannot read chat.db: drag 'iMessage Relay Watcher' (%s) into System Settings > "
                        "Privacy & Security > Full Disk Access and switch it on" % app['path'])
    elif reads['state'] == 'unknown':
        problems.append('watcher has not reported chat.db access yet (is the LaunchAgent loaded?)')
    if st.get('messagesRunning') is False:
        problems.append('Messages is not running; it must stay open (relayctl auto-reopen on, or open it)')
    if not host_manifests():
        problems.append('native host manifest missing: run install.sh again')
    ext = extension_seen()
    if ext['version'] and ext['version'] != rc.VERSION:
        problems.append('the Chrome extension is still v%s: click reload on "iMessage Relay" in chrome://extensions, '
                        'then Cmd+R on x.com/i/chat' % ext['version'])
    return {'ok': True, 'version': rc.VERSION, 'config': public_config(cfg), 'watcher': st,
            'watcherCanReadChatDb': {'ok': True, 'denied': False}.get(reads['state']), 'watcherChatDbEvent': reads,
            'watcherApp': app,
            'launchAgentLoaded': launchagent_loaded(runner), 'hostManifests': host_manifests(),
            'messagesRunning': rc.messages_running(runner), 'extension': ext, 'problems': problems}


def main(argv=None):
    p = argparse.ArgumentParser(prog='relayctl', description='Operate iMessage Relay.')
    sp = p.add_subparsers(dest='cmd', required=True)
    sp.add_parser('status'); sp.add_parser('pause'); sp.add_parser('resume'); sp.add_parser('contacts'); sp.add_parser('version')
    a = sp.add_parser('dry-run'); a.add_argument('value')
    a = sp.add_parser('auto-reopen'); a.add_argument('value')
    a = sp.add_parser('relay-all'); a.add_argument('value')
    a = sp.add_parser('mute'); a.add_argument('key')
    a = sp.add_parser('unmute'); a.add_argument('key')
    a = sp.add_parser('owner-handles'); a.add_argument('action', choices=['add','remove','list','clear','detect']); a.add_argument('value', nargs='?'); a.add_argument('--apply', action='store_true')
    a = sp.add_parser('roster'); a.add_argument('--days', type=int, default=0)
    a = sp.add_parser('quiet-hours'); a.add_argument('start'); a.add_argument('end', nargs='?'); a.add_argument('--tz', default='local')
    a = sp.add_parser('set'); a.add_argument('key'); a.add_argument('value')
    a = sp.add_parser('set-webhook'); a.add_argument('--url', required=True); a.add_argument('--token-stdin', action='store_true')
    a = sp.add_parser('add-contact'); a.add_argument('--name', required=True); a.add_argument('--handle', required=True); a.add_argument('--label', default='')
    a = sp.add_parser('label'); a.add_argument('key'); a.add_argument('label')
    a = sp.add_parser('remove-contact'); a.add_argument('key')
    a = sp.add_parser('recent-senders'); a.add_argument('--days', type=int, default=30); a.add_argument('--limit', type=int, default=20)
    a = sp.add_parser('selftest'); a.add_argument('--direct', action='store_true'); a.add_argument('--wait', type=int, default=150)
    a = sp.add_parser('setup')
    for flag in ('--owner-x-handle', '--sender-x-handle', '--owner-x-user-id', '--relay-conversation-id',
                 '--relay-all', '--webhook-url', '--owner-handle'):
        a.add_argument(flag)
    g = a.add_mutually_exclusive_group()
    g.add_argument('--webhook-key-stdin', action='store_true'); g.add_argument('--webhook-key-prompt', action='store_true')
    a.add_argument('--open-windows', action='store_true')
    sp.add_parser('test-result')
    args = p.parse_args(argv)
    try:
        c = args.cmd
        if c == 'status':
            out = cmd_status()
        elif c in ('pause', 'resume'):
            cfg = edit_config(lambda raw: raw.__setitem__('paused', c == 'pause'))
            out = {'ok': True, 'paused': cfg['paused']}
        elif c == 'dry-run':
            if not parse_bool(args.value):
                require_owner_identity(rc.load_config())
            cfg = edit_config(lambda raw: raw.__setitem__('dryRun', parse_bool(args.value)))
            out = {'ok': True, 'dryRun': cfg['dryRun']}
        elif c == 'auto-reopen':
            cfg = edit_config(lambda raw: raw.__setitem__('autoReopenMessages', parse_bool(args.value)))
            out = {'ok': True, 'autoReopenMessages': cfg['autoReopenMessages']}
        elif c == 'quiet-hours':
            if args.start == 'off':
                cfg = edit_config(lambda raw: raw.__setitem__('quietHours', None))
            else:
                if not (args.end and rc.HHMM_RE.match(args.start) and rc.HHMM_RE.match(args.end)):
                    raise CtlError('use: relayctl quiet-hours 22:00 07:00 [--tz America/Chicago], or: quiet-hours off')
                if args.tz not in ('', 'local') and rc._tz(args.tz) is None:
                    raise CtlError('unknown time zone %r (use an IANA name like America/Chicago, or omit --tz for local)' % args.tz)
                cfg = edit_config(lambda raw: raw.__setitem__('quietHours', {'start': args.start, 'end': args.end, 'tz': args.tz}))
            out = {'ok': True, 'quietHours': cfg['quietHours']}
        elif c == 'set':
            out = cmd_set(args.key, args.value)
        elif c == 'set-webhook':
            out = cmd_set_webhook(args.url, token_stdin=args.token_stdin)
        elif c == 'contacts':
            out = {'ok': True, 'contacts': rc.load_config()['contacts']}
        elif c == 'add-contact':
            out = cmd_add_contact(args.name, args.handle, args.label)
        elif c == 'relay-all':
            if parse_bool(args.value):
                require_owner_identity(rc.load_config())
            def _ra(raw):
                raw['relayAll'] = parse_bool(args.value)
                raw.pop('relayAllPending', None)      # an explicit choice replaces a guided-setup pending one
            cfg = edit_config(_ra)
            out = {'ok': True, 'relayAll': cfg['relayAll']}
        elif c in ('mute', 'unmute'):
            out = cmd_mute(args.key, c == 'mute')
        elif c == 'owner-handles':
            out = cmd_owner_handles(args.action, args.value, apply=args.apply)
        elif c == 'roster':
            out = cmd_roster(args.days or None)
        elif c == 'label':
            out = cmd_label(args.key, args.label)
        elif c == 'remove-contact':
            out = cmd_remove_contact(args.key)
        elif c == 'recent-senders':
            out = cmd_recent_senders(args.days, args.limit)
        elif c == 'selftest':
            out = cmd_selftest(direct=args.direct, wait_s=args.wait)
        elif c == 'setup':
            out = cmd_setup(args)
        elif c == 'test-result':
            out = cmd_test_result()
        else:
            out = {'ok': True, 'version': rc.VERSION}
    except CtlError as e:
        out = {'ok': False, 'error': str(e)}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if c == 'selftest' and out.get('sent') and not out.get('ok'):
        return 2
    return 0 if out.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
