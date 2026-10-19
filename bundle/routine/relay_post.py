#!/usr/bin/env python3
"""iMessage Relay v1.4.3 — routine helper (runs on the BOT's computer, not the Mac).

The webhook routine writes one Mac webhook payload to a unique request file and runs this script
with `--in <file>` (the file is deleted right after it is read). The routine does nothing else:
it never reads or echoes message text, never runs Mac commands, and only reports a one-line,
body-free failure if this helper exits non-zero.

What it does, all in one process:
  1. Dedupe on the payload `id` (ids only, TTL; message bodies never touch disk). ids are committed
     under a file lock (mkstemp + flock) so concurrent runs never lose each other's ids.
  2. Format the line for the relay chat per event type (message, digest, sent, dry_run, send_failed,
     needs_target, blocked, unblocked, status, catching_up, resynced, wrong_account, refused, guess_expired,
     and v1.3.8's relay_test: the /test report, one X message, one short line per step).
  3. Encrypt each line for the relay conversation with xchat_lite's session-encrypt, which unwraps the
     conversation key from the conversation_key_events the routine fetched on every run (no key cache),
     passing the text to xchat_lite on STDIN — never on argv, never to disk.
  4. Print a JSON plan on stdout: the pre-encrypted send(s) the routine then hands to the X plugin's
     `send_chat_message` (plus `add_conversation_keys` first, for an empty thread).

xchat_lite is NOT vendored here: it comes from the x-api-mcp-guide skill (clone of
xdevplatform/xchat-grokbot-helper). This helper locates that install (or uses the paths the routine
passes in `helper`) and shells out to it. See SKILL.md "Webhook routine" for the dependency and the
exact routine prompt. If xchat_lite is missing, this helper formats + dedupes and returns the plan
with `encrypted:false` so the routine can report the setup problem — it never falls back to putting
text on a command line.

STDIN (one JSON object):
  {
    "payload": { ... one Mac webhook event ... },         # required
    "conversation_id": "AAA-BBB",                          # the relay conversation (defaults to payload.relayConversationId)
    "xchat": {                                             # context the routine fetched via the X plugin (optional; needed to actually encrypt)
      "events": [...], "conversation_key_events": [...], "signing_keys": [...],
      "prepare": null | { ...prepare-keys stdin... }       # set only for an empty / first-message thread
    },
    "helper": {                                            # optional explicit xchat_lite paths (else auto-located)
      "python": "/…/.venv/bin/python", "script": "/…/xchat_lite.py",
      "user_id": "…", "key_version": "…", "juicebox": "/…/juicebox.json",
      "xdg_config_home": "~/xchat/sender",                 # exported so xchat_lite finds the saved key
      "env": {"XDG_CONFIG_HOME": "~/xchat/sender"}          # extra env merged over os.environ
    },
    "dedupe_path": "~/.imessage-relay-routine/seen_ids.json",  # optional override
    "ttl_seconds": 86400,
    "defer_commit": false    # true = don't remember ids now; call `relay_post.py --ack` after send instead
  }
Read the request from a file with `--in <path>` (keeps untrusted text off the command line; the
file is deleted right after it is read). To keep the message BODY out of even the request file, write
the raw Mac webhook event (the `<webhook_event>` object, body included) to its own file and pass it with
`--payload-file <path>`: it becomes the request's `payload` and is deleted right after it is read, so the
`--in` request can be assembled with NO body in it at all (item B). A `--payload-file` must parse as
exactly one JSON object; a bare value, a list, truncation, or two concatenated objects (e.g. from a
heredoc delimiter that appeared inside a relayed message) is refused with a body-free error and nothing
is forwarded. Confirm a strict post-send dedupe
with `--ack ID1 ID2 ... [--dedupe-path P]` (ids on argv), or `--ack` reading {"dedupe_path","ids":[...]}
on stdin.

STDOUT (one JSON object):
  {"action":"send","sends":[{"conversation_id","message_id","encoded_message_create_event",
                             "encoded_message_event_signature","add_conversation_keys":null|{...},
                             "should_notify":bool,"reply_to":null|"<x event id>"}],
   "encrypted":true, "posted_kind":"message"}
  Pass each send's should_notify and reply_to straight to send_chat_message: a relayed text notifies
  the owner; every status/confirmation line is silent (should_notify:false) and threads under the
  owner's reply when reply_to is set (item 1).
  {"action":"skip","reason":"duplicate id …"}
  {"action":"error","reason":"…"}                          # body-free; the routine posts one failure line
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid as _uuid

GROUP_SEP = ' \u00b7 '   # "Family · Sam: …"


# ---------- formatting (pure; unit-tested) ----------
def _label(p, default='someone'):
    return str(p.get('label') or p.get('contactName') or default).strip() or default


def _message_line(m):
    """`<label>: <text>` or `<group.label> · <label>: <text>`. Carries the (untrusted) body. A tapback
    line (item 5) joins with a space, not a colon: "Alex \u2764\ufe0f your message"."""
    label = _label(m)
    text = str(m.get('text') or '')
    g = m.get('group') or {}
    gl = str(g.get('label') or g.get('name') or '').strip()
    head = (gl + GROUP_SEP if gl else '') + label
    sep = ' ' if m.get('reaction') else ': '
    return head + sep + text


def format_event(payload):
    """Return {"kind", "lines":[{"id","text","body":bool}]}.

    `body` is True when the line contains relayed (untrusted) message text; every line is still
    encrypted the same way. Raises ValueError on an unknown type.
    """
    t = str(payload.get('type') or '').strip()
    base_id = str(payload.get('id') or '')
    if t == 'message':
        return {'kind': 'message', 'lines': [{'id': base_id, 'text': _message_line(payload), 'body': True}]}
    if t == 'digest':
        # item 7: a header line, then ONE X message per person (their texts grouped under one label).
        msgs = payload.get('messages') or []
        groups, index = [], {}
        for i, m in enumerate(msgs):
            mid = str(m.get('id') or (base_id + '#' + str(i)))
            label = _label(m)
            g = m.get('group') or {}
            gl = str(g.get('label') or g.get('name') or '').strip()
            head = (gl + GROUP_SEP if gl else '') + label
            if head not in index:
                index[head] = len(groups)
                groups.append([head, [], []])
            gi = index[head]
            sep = ' ' if m.get('reaction') else ''
            groups[gi][1].append(sep + str(m.get('text') or '') if sep else str(m.get('text') or ''))
            groups[gi][2].append(mid)
        n, people = len(msgs), len(groups)
        header = '\U0001f4ec %d message%s while you were away%s.' % (
            n, '' if n == 1 else 's', (' from %d people' % people) if people > 1 else '')
        lines = [{'id': (base_id or 'digest') + ':header', 'text': header, 'body': False}]
        for head, texts, ids in groups:
            lines.append({'id': 'digest:' + '+'.join(ids), 'text': head + ': ' + '\n\u2002'.join(texts), 'body': True})
        return {'kind': 'digest', 'lines': lines}
    if t == 'sent':
        # item 6: ONE confirmation, posted by the watcher: \u2713\u2713 when chat.db shows delivered, else \u2713
        # after the ~20s fallback. (Dry runs and delivery-check-off still come from the host.)
        label = _label(payload)
        note = str(payload.get('note') or '').strip()[:60]     # v1.4.1: "new contact, \u2022\u2022\u20220188"
        if payload.get('dryRun'):
            line = '\u2192 ' + label + ' (dry run, not sent)'
        elif note:
            line = '\u2192 ' + label + (' \u2713\u2713' if payload.get('delivered') else ' \u2713') + ' (' + note + ')'
        elif payload.get('delivered'):
            line = '\u2192 ' + label + ' \u2713\u2713 delivered'
        else:
            line = '\u2192 ' + label + ' \u2713 sent'
        return {'kind': 'sent', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'dry_run':
        return {'kind': 'dry_run', 'lines': [{'id': base_id, 'text': '\u2192 ' + _label(payload) + ' (dry run, not sent)', 'body': False}]}
    if t == 'send_failed':
        if str(payload.get('reason') or '') == 'unconfirmed':
            # W3: no chat.db row ever appeared for this send within ~10 minutes.
            line = '\u26a0\ufe0f Couldn\u2019t confirm your reply to ' + _label(payload) + ' was delivered.'
            return {'kind': 'send_failed', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
        err = str(payload.get('error') or '').strip()
        tail = (' (' + err + ')') if err else ''
        if payload.get('superseded'):
            # W3 nit: a soft "\u2713 sent" already went out, then the send failed -> say this corrects it.
            line = ('\u26a0\ufe0f Correction: your reply to ' + _label(payload)
                    + ' actually failed to deliver \u2014 disregard the earlier \u2713 sent' + tail + '.')
        else:
            line = '\u26a0\ufe0f Couldn\u2019t deliver your reply to ' + _label(payload) + ' \u2014 it may not have sent' + tail + '.'
        return {'kind': 'send_failed', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'needs_confirm':
        # W1/item 3: a label-only confirmation prompt. Never carries the message body. should_notify false.
        label = _label(payload)
        alt = str(payload.get('alt') or '').strip()
        if payload.get('reason') == 'new_contact':
            # v1.4.0: someone found in the Mac's Contacts (host-composed, name + last 4 digits, no body).
            detail = str(payload.get('detail') or '').strip()[:400]
            text = detail or ('\u2753 Text %s from your Contacts? Reply yes within 2 min.' % label)
            return {'kind': 'needs_confirm', 'lines': [{'id': base_id, 'text': text, 'body': False}]}
        if payload.get('reason') == 'directive':
            core = ('Send to %s instead of %s?' % (label, alt)) if alt else ('Send to %s?' % label)
        else:
            core = 'Reply to %s?' % label
        detail = str(payload.get('detail') or '').strip()[:400]      # v1.4.1: host-composed, body-free
        text = detail or ('\u2753 ' + core + ' Reply yes within 2 min.')
        return {'kind': 'needs_confirm', 'lines': [{'id': base_id, 'text': text, 'body': False}]}
    if t == 'needs_target':
        detail = str(payload.get('detail') or 'Who\u2019s this for? Start with a name, like "Alex: \u2026" or "text Alex \u2026".').strip()
        return {'kind': 'needs_target', 'lines': [{'id': base_id, 'text': detail, 'body': False}]}
    if t == 'blocked':
        label = _label(payload)
        if payload.get('dryRun'):
            line = '\U0001f6ab (dry run) would mute ' + label + '. Say "/unblock ' + label + '" to undo once live.'
        else:
            line = '\U0001f6ab Muted ' + label + '. Say "/unblock ' + label + '" to undo.'
        return {'kind': 'blocked', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'unblocked':
        return {'kind': 'unblocked', 'lines': [{'id': base_id, 'text': '\u2705 Unmuted ' + _label(payload) + '.', 'body': False}]}
    if t in ('status', 'catching_up', 'resynced'):
        status = t if t in ('catching_up', 'resynced') else str(payload.get('status') or '').strip()
        detail = str(payload.get('detail') or '').strip()
        if status == 'catching_up':
            line = '\u23f3 Catching up \u2014 relaying a burst in batches (nothing dropped).'
        elif status == 'resynced':
            line = '\U0001f504 Resynced after a long gap; older messages were skipped.'
        elif status == 'messages_not_running':
            line = '\u26a0\ufe0f Messages isn\u2019t running on the Mac, so replies can\u2019t be sent right now.'
        elif status == 'fda_missing':
            line = '\u26a0\ufe0f The watcher can\u2019t read Messages yet (Full Disk Access). New texts aren\u2019t coming through.'
        else:
            line = '\u2139\ufe0f ' + (detail or ('Status: ' + (status or 'unknown')))
        return {'kind': 'status:' + (status or 'unknown'), 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'wrong_account':
        line = '\u26a0\ufe0f The relay tab is signed in to the wrong X account \u2014 replies are paused until you switch back to the owner account.'
        return {'kind': 'wrong_account', 'lines': [{'id': base_id or 'wrong_account', 'text': line, 'body': False}]}
    if t == 'guess_expired':
        label = _label(payload, '')
        tail = (' to ' + label) if label else ''
        return {'kind': 'guess_expired', 'lines': [{'id': base_id, 'text': '\u231b Not sent%s.' % tail, 'body': False}]}
    if t == 'refused':
        reason = str(payload.get('reason') or '').strip()
        label = _label(payload, '')
        if reason == 'echo':
            line = '\u26a0\ufe0f Didn\u2019t send that to ' + (label or 'them') + ' \u2014 it looked like a relayed message bounced straight back.'
        else:
            line = '\u26a0\ufe0f A reply wasn\u2019t sent' + ((' to ' + label) if label else '') + '.'
        return {'kind': 'refused', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'relay_test':
        return _relay_test(payload, base_id)
    raise ValueError('unknown webhook type: ' + (t or '(empty)'))


_TEST_DETAIL_MAX = 100


def _test_detail(v):
    d = re.sub(r'\s+', ' ', str(v or '')).strip()
    return (d[:_TEST_DETAIL_MAX - 1] + '\u2026') if len(d) > _TEST_DETAIL_MAX else (d or 'no detail')


def _relay_test(payload, base_id):
    """v1.3.8 /test report: ONE silent X message, a header then one short line per step. The host sends
    the steps it could check itself; the "webhook accepted" and "routine posted" lines prove themselves
    by appearing at all. The header starts with the test sigil and carries the test tag so the extension
    recognizes it (and the echo guard treats it as bot output, so it is never relayed back out)."""
    steps = [s for s in (payload.get('steps') if isinstance(payload.get('steps'), list) else []) if isinstance(s, dict)]
    tag = re.sub(r'[^0-9a-f]', '', str(payload.get('tag') or payload.get('testId') or '').lower())[:6]
    host = [s for s in steps if s.get('name') not in ('send', 'webhook', 'routine')]
    send = [s for s in steps if s.get('name') == 'send']
    rows = [(s.get('ok'), _test_detail(s.get('detail'))) for s in host]
    rows += [(True, 'webhook POST accepted'), (True, 'routine posted this report')]
    rows += [(s.get('ok'), _test_detail(s.get('detail'))) for s in send]
    failed = sum(1 for ok, _ in rows if ok is False)
    header = '\U0001f9ea relay test' + ((' #' + tag) if tag else '') + (
        ' \u2014 all checks passed' if not failed else ' \u2014 %d failed' % failed)
    body = ['%s %d %s' % ('\u2705' if ok is True else '\u274c' if ok is False else '\u2796', i, d)
            for i, (ok, d) in enumerate(rows, 1)]
    return {'kind': 'relay_test',
            'lines': [{'id': base_id or ('relay_test:' + tag), 'text': '\n'.join([header] + body), 'body': False}]}


# ---------- dedupe (ids only, TTL; no bodies on disk; concurrency-safe) ----------
class Dedupe:
    """Payload-id dedupe store. Reads are lock-free; commits take an exclusive flock on a sidecar lock
    file, re-read the store, merge, prune by TTL, and write atomically (mkstemp + rename), so many
    relay_post processes running at once never lose each other's ids or corrupt the file (item 10)."""

    def __init__(self, path, ttl_seconds=86400, now=time.time):
        self.path = os.path.expanduser(path)
        self.lock_path = self.path + '.lock'
        self.ttl = int(ttl_seconds)
        self.now = now
        self._d = self._read()

    def _read(self):
        try:
            with open(self.path) as fh:
                d = {str(k): float(v) for k, v in (json.load(fh) or {}).items()}
        except (FileNotFoundError, ValueError):
            d = {}
        cut = self.now() - self.ttl
        return {k: v for k, v in d.items() if v >= cut}

    def seen(self, msg_id):
        return bool(msg_id) and msg_id in self._d

    def _ensure_dir(self):
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)

    def commit(self, ids):
        """Record ids under an exclusive lock, merging with whatever other processes wrote meanwhile."""
        ids = [i for i in ids if i]
        if not ids:
            return
        self._ensure_dir()
        with open(self.lock_path, 'a') as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            d = self._read()                         # re-read inside the lock to capture concurrent writers
            t = self.now()
            for i in ids:
                d[i] = t
            cut = t - self.ttl
            d = {k: v for k, v in d.items() if v >= cut}
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or '.', prefix='.seen-')
            try:
                with os.fdopen(fd, 'w') as fh:
                    json.dump(d, fh)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            self._d = d


# ---------- xchat_lite location + encryption ----------
def locate_helper(helper):
    """Return (python, script) for xchat_lite, honoring explicit paths then the skill's clone layout."""
    py = (helper or {}).get('python')
    script = (helper or {}).get('script')
    if py and script:                      # the routine is authoritative about where it cloned xchat_lite
        return os.path.expanduser(str(py)), os.path.expanduser(str(script))   # item N3: expand ~ paths
    home = os.path.expanduser('~')
    roots = (os.path.join(home, 'xchat-grokbot-helper'), os.path.join(os.getcwd(), 'xchat-grokbot-helper'),
             os.path.join(home, 'xchat-lite'), os.path.join(os.getcwd(), 'xchat-lite'))
    for root in roots:
        sp = os.path.join(root, 'xchat_lite.py')
        v = os.path.join(root, '.venv', 'bin', 'python')
        if os.path.exists(sp):
            return (v if os.path.exists(v) else sys.executable), sp
    return None, None


def _run(argv, stdin_obj, runner):
    """Run a helper command, feeding JSON on STDIN. Text never appears in argv, and helper stderr is
    never surfaced (it could echo message text) \u2014 only the exit code is reported (item 3)."""
    proc = runner(argv, input=json.dumps(stdin_obj).encode('utf-8'))
    out = proc.stdout.decode('utf-8') if isinstance(proc.stdout, (bytes, bytearray)) else (proc.stdout or '')
    if proc.returncode != 0:
        raise RuntimeError('xchat_lite exited %s' % proc.returncode)
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError('xchat_lite returned non-JSON output')


def session_encrypt_stdin(conversation_id, text, xchat, reply_to=None):
    """Build the session-encrypt stdin. The body goes here (stdin), never on argv. session-encrypt
    unwraps the conversation key from conversation_key_events itself each run (robust key resolution),
    so there is no separate key cache to go stale. `reply_to`, when set, is the X event id of the
    owner's own reply so the confirmation threads under their bubble (item 1); xchat_lite ignores it if
    it cannot thread, which is the graceful fallback to a plain line."""
    body = {'conversation_id': conversation_id, 'text': text,
            'events': (xchat or {}).get('events') or [],
            'conversation_key_events': (xchat or {}).get('conversation_key_events') or [],
            'signing_keys': (xchat or {}).get('signing_keys') or []}
    prep = (xchat or {}).get('prepare')
    if prep:
        body['prepare'] = prep
    if reply_to:
        body['reply_to'] = str(reply_to)
    return body


def encrypt_line(conversation_id, text, xchat, helper, runner, reply_to=None, should_notify=False):
    """Encrypt one line via xchat_lite session-encrypt. Returns the send dict for send_chat_message.
    session-encrypt warms keys and unwraps the conversation key from conversation_key_events in one
    process (robust key resolution). Raises on failure."""
    py, script = locate_helper(helper)
    if not py or not script:
        raise RuntimeError('xchat_lite not found (see x-api-mcp-guide skill)')
    base = [py, script]
    h = helper or {}
    for flag, key in (('--user-id', 'user_id'), ('--key-version', 'key_version'), ('--juicebox', 'juicebox')):
        if h.get(key):
            val = os.path.expanduser(str(h[key])) if key == 'juicebox' else str(h[key])   # item N3
            base += [flag, val]
    stdin_obj = session_encrypt_stdin(conversation_id, text, xchat, reply_to)
    res = _run(base + ['session-encrypt'], stdin_obj, runner)
    return {
        'conversation_id': str(res.get('id') or conversation_id),
        'message_id': res.get('message_id') or str(_uuid.uuid4()),
        'encoded_message_create_event': res.get('encoded_message_create_event'),
        'encoded_message_event_signature': res.get('encoded_message_event_signature'),
        'add_conversation_keys': res.get('add_conversation_keys') if res.get('needs_add_conversation_keys_before_send') else None,
        # item 1: a relayed inbound message pings the owner; every status/confirmation line is silent.
        'should_notify': bool(should_notify),
        # item 1: thread a confirmation under the owner's reply when the webhook carried its X event id.
        'reply_to': str(reply_to) if reply_to else None,
    }


# ---------- main ----------
def _default_runner(helper):
    """subprocess runner that carries XDG_CONFIG_HOME (and any helper.env) so xchat_lite finds the
    sender's saved key at $XDG_CONFIG_HOME/xchat-grokbot-helper/saved-key.json (item 3). The call
    signature stays (argv, input=...) so a test runner can stand in unchanged."""
    env = dict(os.environ)
    h = helper or {}
    if h.get('xdg_config_home'):
        # expand ~ before exporting so xchat_lite finds $XDG_CONFIG_HOME/xchat-grokbot-helper/saved-key.json
        env['XDG_CONFIG_HOME'] = os.path.expanduser(str(h['xdg_config_home']))   # item N3
    for k, v in (h.get('env') or {}).items():
        val = str(v)
        if str(k).upper() == 'XDG_CONFIG_HOME' or '/' in val or val.startswith('~'):
            val = os.path.expanduser(val)
        env[str(k)] = val
    return lambda argv, input=b'': subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def process(req, runner=None):
    payload = req.get('payload') or {}
    conv = str(req.get('conversation_id') or payload.get('relayConversationId') or '').strip()
    ev = format_event(payload)                              # may raise ValueError -> error
    runner = runner or _default_runner(req.get('helper'))

    dedupe_path = req.get('dedupe_path') or '~/.imessage-relay-routine/seen_ids.json'
    dd = Dedupe(dedupe_path, req.get('ttl_seconds', 86400))
    lines = [ln for ln in ev['lines'] if not dd.seen(ln['id'])]
    if not lines:
        return {'action': 'skip', 'reason': 'duplicate id(s); nothing to post', 'posted_kind': ev['kind']}

    if not conv:
        return {'action': 'error', 'reason': 'no relay conversation_id'}

    xchat = req.get('xchat')
    # No X context supplied: we cannot encrypt. Return the plan unencrypted so the routine can report
    # the setup gap. We NEVER send text any other way.
    if not xchat:
        return {'action': 'error', 'reason': 'no xchat context (fetch conversation events + keys first)',
                'posted_kind': ev['kind'], 'encrypted': False}

    reply_to = payload.get('reply_to')          # item 1: X event id of the owner's reply (optional)
    sends = []
    try:
        for ln in lines:
            # body lines (relayed texts) notify and never thread; status/confirmation lines are silent
            # and thread under the owner's bubble when a reply_to was supplied.
            notify = bool(ln.get('body'))
            rt = None if notify else reply_to
            sends.append(encrypt_line(conv, ln['text'], xchat, req.get('helper'), runner,
                                      reply_to=rt, should_notify=notify))
    except Exception as e:                                  # body-free error for the routine
        return {'action': 'error', 'reason': str(e)[:200], 'posted_kind': ev['kind'], 'encrypted': False}

    # The sends are prepared and encrypted; remember their ids now (concurrency-safe). The routine
    # then calls send_chat_message. For strict "remember only after the send landed", set
    # defer_commit:true and call `relay_post.py --ack {"dedupe_path","ids":[…]}` after the send
    # succeeds instead (see SKILL.md). The Mac retries each POST until it gets a 2xx, so a committed
    # id is what prevents a retried webhook from being re-sent.
    ids = [ln['id'] for ln in lines]
    if not req.get('defer_commit'):
        dd.commit(ids)
    return {'action': 'send', 'sends': sends, 'encrypted': True, 'posted_kind': ev['kind'], 'ack_ids': ids}


def ack(req):
    """Record ids as sent AFTER the routine's send_chat_message succeeded (strict dedupe path)."""
    dedupe_path = req.get('dedupe_path') or '~/.imessage-relay-routine/seen_ids.json'
    dd = Dedupe(dedupe_path, req.get('ttl_seconds', 86400))
    ids = [str(i) for i in (req.get('ids') or []) if i]
    dd.commit(ids)
    return {'action': 'ack', 'remembered': len(ids)}


def _read_request(infile):
    """Read the request JSON from a file or stdin. The --in file holds the (untrusted) message body, so
    it is DELETED right after it is read — even if parsing or anything downstream then fails (item N7)."""
    if infile:
        try:
            with open(infile, encoding='utf-8') as fh:
                raw = fh.read()
        finally:
            try:
                os.remove(infile)
            except OSError:
                pass
    else:
        raw = sys.stdin.read()
    return json.loads(raw or '{}')


def _read_payload_file(path):
    """Read the raw Mac webhook event JSON from a file and DELETE the file right after (even on error):
    it holds the untrusted body, so it never lingers and never reaches a command line (item B). The file
    must parse as exactly ONE JSON object; anything else (a bare value, a list, truncated/garbage, or two
    concatenated objects) is refused with a body-free error so a half-written or corrupt file is never
    forwarded. The raw text is never echoed."""
    try:
        with open(path, encoding='utf-8') as fh:
            raw = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    obj = json.loads(raw or 'null')          # JSONDecodeError (a ValueError) on garbage/truncation/two objects
    if not isinstance(obj, dict):
        raise ValueError('payload-file is not a single JSON object')
    return obj


def main(argv=None):
    ap = argparse.ArgumentParser(description='Format + encrypt one Mac relay webhook for X Chat.')
    ap.add_argument('--in', dest='infile', help='read the request JSON from this file instead of stdin '
                    '(keeps untrusted message text out of any command line); the file is deleted right '
                    'after it is read')
    ap.add_argument('--ack', action='store_true', help='record ids as sent AFTER send_chat_message '
                    'succeeded (post-send dedupe). Pass ids as arguments: --ack ID1 ID2 ... '
                    '[--dedupe-path P]; with no id arguments, reads {dedupe_path, ids:[...]} on stdin')
    ap.add_argument('--dedupe-path', dest='dedupe_path', help='dedupe store path for --ack')
    ap.add_argument('--payload-file', dest='payload_file', help='read the raw Mac webhook event (the '
                    'message body) from this file and use it as the request payload; the file is deleted '
                    'right after it is read, so the --in request can be assembled with no body (item B)')
    ap.add_argument('ids', nargs='*', help='message ids to record as sent (used with --ack)')
    args = ap.parse_args(argv)
    # main ALWAYS prints exactly one JSON object and never raises (item 10).
    try:
        if args.ack and args.ids:
            req = {'ids': args.ids}
            if args.dedupe_path:
                req['dedupe_path'] = args.dedupe_path
        else:
            req = _read_request(args.infile)
            if args.payload_file:
                # the body lives only in the payload file; splice it in, then it is gone from disk.
                if not isinstance(req, dict):
                    req = {}
                try:
                    req['payload'] = _read_payload_file(args.payload_file)
                except (OSError, ValueError):
                    # body-free: never include the file's contents or the parser message text.
                    print(json.dumps({'action': 'error', 'reason': 'payload-file is not a single valid JSON object'}))
                    return 2
    except (OSError, ValueError) as e:
        print(json.dumps({'action': 'error', 'reason': 'could not read request JSON: %s' % type(e).__name__}))
        return 2
    try:
        if args.ack:
            if args.dedupe_path and isinstance(req, dict) and 'dedupe_path' not in req:
                req['dedupe_path'] = args.dedupe_path
            res = ack(req)
        else:
            res = process(req)
    except ValueError as e:
        res = {'action': 'error', 'reason': str(e)[:200]}
    except Exception as e:                                  # never crash without a JSON line
        res = {'action': 'error', 'reason': '%s' % type(e).__name__}
    print(json.dumps(res))
    return 0 if res.get('action') != 'error' else 2


if __name__ == '__main__':
    sys.exit(main())
