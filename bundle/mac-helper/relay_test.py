"""
iMessage Relay v1.4.2: the /test end-to-end check (runs inside the native host).

The owner types "/test" (or "/test send") in the relay chat. The extension intercepts it before any
reply routing and asks the host for {type:"test"}. The host then checks, each with its own timeout:

  1 extension   the extension saw the command (implied by this request; carries its version)
  2 host        the native host is reachable (this code is running; carries its version)
  3 watcher     the watcher (the app with Full Disk Access) answers a probe job and can read chat.db
  4 messages    Messages.app is running (watcher's answer, else the host's own pgrep)
  [send]        "/test send" only: one "🧪 relay test" iMessage to the OWNER'S OWN handle (never a
                contact; skipped in dry run)
  5 webhook     one body-free {type:"relay_test"} POST to the bot's webhook is accepted
  6 routine     the routine renders it (relay_post.py) back into X Chat as "🧪 relay test #tag";
                the extension watches for that line and reports it seen / not seen

Nothing here ever texts a contact, reads a message body, or stores one. The last result (steps only)
is written to ~/.imessage-relay/selftest/test-last.json for `relayctl test-result`.
"""
import os
import re
import uuid

import relay_common as rc

TEST_TEXT = '\U0001f9ea relay test'
WATCHER_WAIT_S = 12        # the watcher polls jobs every second, so 12 s means it is not running
SEND_TIMEOUT_S = 25
WEBHOOK_TIMEOUT_S = 6
_ID_RE = re.compile(r'^[0-9a-f]{6,32}$')


def last_path():
    return os.path.join(rc.APP_DIR, 'selftest', 'test-last.json')     # resolved per call (APP_DIR can move in tests)


def jobs_dir():
    return os.path.join(rc.APP_DIR, 'jobs')


def new_test_id():
    return uuid.uuid4().hex[:12]


def clean_test_id(v):
    v = str(v or '').strip().lower()
    return v if _ID_RE.match(v) else new_test_id()


def tag_of(test_id):
    """The short tag shown in X Chat ("🧪 relay test #a1b2c3"); the extension matches on it."""
    return str(test_id)[:6]


def _step(name, ok, detail):
    return {'name': name, 'ok': ok, 'detail': str(detail)[:120]}


def probe_watcher(sleeper, now, wait_s=WATCHER_WAIT_S):
    """Drop a test_probe job for the watcher and wait for its answer. None on timeout (watcher down)."""
    jid = 'test' + uuid.uuid4().hex[:10]
    d = jobs_dir()
    job_path = os.path.join(d, jid + '.job.json')
    res_path = os.path.join(d, jid + '.result.json')
    rc.write_json_atomic(job_path, {'id': jid, 'type': 'test_probe', 'createdAt': now()})
    deadline = now() + wait_s
    while True:
        res = rc.read_json(res_path)
        if isinstance(res, dict):
            try:
                os.unlink(res_path)
            except OSError:
                pass
            return res
        if now() >= deadline:
            break
        sleeper(0.5)
    for p in (job_path, res_path):          # a late watcher must not answer a stale probe
        try:
            os.unlink(p)
        except OSError:
            pass
    return None


def watcher_steps(probe, runner):
    if probe is None:
        w = _step('watcher', False, 'watcher did not answer in %d s (not running? relayctl status)' % WATCHER_WAIT_S)
    elif probe.get('chatDb') == 'ok':
        w = _step('watcher', True, 'watcher alive, can read chat.db')
    elif probe.get('chatDb') == 'busy':
        w = _step('watcher', True, 'watcher alive, chat.db busy (readable)')
    else:
        w = _step('watcher', False, 'watcher alive but cannot read chat.db (grant it Full Disk Access)')
    running = probe.get('messagesRunning') if isinstance(probe, dict) else None
    if running is None:
        running = rc.messages_running(runner)
    if running:
        m = _step('messages', True, 'Messages is running')
    elif running is False:
        m = _step('messages', False, 'Messages is not running (open it, or relayctl auto-reopen on)')
    else:
        m = _step('messages', False, 'could not tell whether Messages is running')
    return [w, m]


def send_step(cfg, runner, sleeper):
    """'/test send': one test iMessage to the owner's OWN handle only. Never a contact."""
    owner = cfg.get('ownerHandle', '')
    if not rc.valid_handle(owner):
        return _step('send', False, 'no ownerHandle set, nothing sent (relayctl owner-handles detect)')
    if rc.contact_by_handle(cfg, owner):
        return _step('send', False, 'ownerHandle is also a contact, refusing to send')
    if cfg['dryRun']:
        rc.log_event('test_send', ok=True, dryRun=True)
        return _step('send', None, 'dry run: would text your own number, nothing sent')
    ok, err = rc.send_imessage(TEST_TEXT, owner, runner=runner, sleeper=sleeper, timeout=SEND_TIMEOUT_S)
    rc.log_event('test_send', ok=ok, error=err or '')
    if ok:
        return _step('send', True, 'test iMessage sent to your own number')
    return _step('send', False, 'send to your own number failed: ' + rc.error_message(err))


def run_test(msg, cfg, runner, sleeper, now, poster, via='extension'):
    """Returns {ok, command:'test', testId, tag, steps:[...], webhookAccepted}. Never raises for a
    failed step; a step that cannot finish inside its timeout is reported failed."""
    tid = clean_test_id(msg.get('testId'))
    ext_v = re.sub(r'[^0-9A-Za-z.\-]', '', str(msg.get('extVersion') or ''))[:20]
    steps = [_step('extension', True, 'extension saw /test' + (' (v%s)' % ext_v if ext_v else '')),
             _step('host', True, 'native host reachable (v%s)' % rc.VERSION)]
    try:
        probe = probe_watcher(sleeper, now)
    except Exception as e:                   # e.g. jobs dir unwritable
        rc.log_event('test_probe_error', error=str(e)[:200])
        probe = None
    steps += watcher_steps(probe, runner)
    if msg.get('send') is True:
        try:
            steps.append(send_step(cfg, runner, sleeper))
        except Exception as e:
            steps.append(_step('send', False, 'send step error: ' + str(e)[:80]))
    payload = {'type': 'relay_test', 'id': 'relay_test:' + tid, 'testId': tid, 'tag': tag_of(tid),
               'steps': list(steps), 'via': via, 'at': rc.iso_utc(now())}
    accepted = False
    if cfg.get('webhookUrl'):
        try:
            accepted = bool(poster(cfg['webhookUrl'], payload, cfg.get('webhookToken', ''), timeout=WEBHOOK_TIMEOUT_S))
        except Exception:
            accepted = False
        wh = _step('webhook', accepted, 'webhook POST accepted' if accepted else 'webhook POST failed (URL/key wrong or bot unreachable)')
    else:
        wh = _step('webhook', False, 'no webhook set (relayctl set-webhook)')
    steps.append(wh)
    steps.append(_step('routine', None if accepted else False,
                       'waiting for the routine to post the report' if accepted else 'skipped: webhook failed'))
    rc.log_event('test_run', testId=tid, via=via, failed=[s['name'] for s in steps if s['ok'] is False])
    record = {'testId': tid, 'tag': tag_of(tid), 'at': rc.iso_utc(now()), 'via': via, 'steps': steps}
    try:
        rc.write_json_atomic(last_path(), record)
    except Exception:
        pass
    return {'ok': not any(s['ok'] is False for s in steps), 'command': 'test', 'testId': tid, 'tag': tag_of(tid),
            'steps': steps, 'webhookAccepted': accepted, 'dryRun': cfg['dryRun']}


def record_routine(test_id, seen, now):
    """The extension saw (or timed out waiting for) the routine's report: update test-last.json."""
    rec = rc.read_json(last_path())
    if not isinstance(rec, dict) or rec.get('testId') != str(test_id or ''):
        return False
    for s in rec.get('steps') or []:
        if s.get('name') == 'routine':
            s['ok'] = bool(seen)
            s['detail'] = ('routine posted the report in X Chat' if seen
                           else 'routine did not post the report in time (check the routine run / relay_post.py)')
    rec['routineCheckedAt'] = rc.iso_utc(now())
    rc.write_json_atomic(last_path(), rec)
    rc.log_event('test_routine', testId=rec['testId'], seen=bool(seen))
    return True
