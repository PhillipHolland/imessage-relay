"""v1.4.18: opt-in direct poster. Default off. Fictional names only. No live X calls."""
import collections
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.parse
from datetime import datetime, timezone

from helpers import fresh, read_log, tmpdir, ROOT
import relay_header as rh
import relay_keychain as rk
import relay_xpost as rx
from test_watch import ALLOWED, Base

ROUTINE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'routine'))
if ROUTINE not in sys.path:
    sys.path.insert(0, ROUTINE)
import relay_post as rp  # noqa: E402

OK = {'ok': True, 'retryable': False, 'code': 'ok', 'message_id': 'x-msg-1', 'header_sent': False}
FULL = {'events': [], 'conversation_key_events': ['KEYEV'], 'signing_keys': [{'user_id': 'U'}]}


class XRunner(object):
    """subprocess stand-in. Records direct-post stdin. Never a shell."""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []
        self.inputs = []
        self.results = []
        self.messages_running = True

    def __call__(self, cmd, **kw):
        assert 'shell' not in kw, 'never use a shell'
        self.calls.append(list(cmd))
        if cmd and cmd[-1] == 'post' and any(str(c).endswith('relay_xpost.py') for c in cmd):
            self.inputs.append(kw.get('input'))
            if not self.outcomes:
                raise AssertionError('unexpected direct post')
            out = self.outcomes.pop(0)
            if out == 'timeout':
                raise subprocess.TimeoutExpired(cmd, 8)
            if out == 'empty':
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if isinstance(out, str):
                return subprocess.CompletedProcess(cmd, 0, out, '')
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out), '')
        if cmd[0] == '/usr/bin/osascript':
            rc, err = self.results.pop(0)
            return subprocess.CompletedProcess(cmd, rc, '', err)
        if cmd[0] == '/usr/bin/pgrep':
            return subprocess.CompletedProcess(cmd, 0 if self.messages_running else 1, '', '')
        return subprocess.CompletedProcess(cmd, 0, '', '')


class KeyRunner(object):
    def __init__(self, present=()):
        self.present = set(present)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if len(argv) > 1 and argv[1] == 'delete-generic-password':
            self.present.discard(argv[argv.index('-a') + 1])
            return subprocess.CompletedProcess(argv, 0, '', '')
        if len(argv) > 1 and argv[1] == 'find-generic-password':
            acct = argv[argv.index('-a') + 1]
            return subprocess.CompletedProcess(argv, 0 if acct in self.present else 1, '', '')
        return subprocess.CompletedProcess(argv, 0, '', '')


def _lines(mod, messages):
    out = []
    for i, m in enumerate(messages):
        out.append({'id': str(m.get('id') or i), 'text': mod._message_line(m), 'body': True, 'hdr': mod._hdr_meta(m)})
    return out


class HeaderParity(unittest.TestCase):
    def test_matches_relay_post_and_state_has_no_name(self):
        msgs = [
            {'id': '1', 'label': 'Alex', 'handle': '+15555550123', 'text': 'hello there',
             'receivedAt': '2026-01-01T00:00:00Z', 'localTime': '7:20 PM'},
            {'id': '2', 'label': 'Alex', 'handle': '+15555550123', 'text': '\U0001f4f7 Photo', 'quiet': True,
             'receivedAt': '2026-01-01T00:05:00Z', 'localTime': '7:25 PM'},
            {'id': '3', 'label': 'Alex', 'handle': '+15555550123', 'text': 'on my way', 'quiet': True,
             'receivedAt': '2026-01-01T00:06:00Z', 'localTime': '7:26 PM'},
            {'id': '4', 'label': 'Sam', 'handle': '+15555550188', 'text': '\u2764\ufe0f a message', 'reaction': True,
             'receivedAt': '2026-01-01T00:07:00Z', 'localTime': '7:27 PM'},
            {'id': '5', 'label': 'Sam', 'handle': '+15555550188', 'text': 'hi', 'group': {'label': 'Family'},
             'receivedAt': '2026-01-01T00:08:00Z', 'localTime': '7:28 PM'},
            {'id': '6', 'label': 'Sam', 'handle': '+15555550188', 'text': 'hi', 'unknown': True,
             'receivedAt': '2026-01-01T00:09:00Z', 'localTime': '7:29 PM'},
            {'id': '7', 'label': 'Alex', 'handle': '+15555550123', 'text': 'offer', 'tag': 'spam',
             'receivedAt': '2026-01-01T00:10:00Z', 'localTime': '7:30 PM'},
        ]
        photo = rh._message_line(msgs[1])
        self.assertEqual(photo, rp._message_line(msgs[1]))
        self.assertIn('Alex:', photo)                          # a photo line keeps the name
        self.assertEqual(rh._message_line(msgs[2]), 'on my way')
        self.assertNotIn(': ', rh._message_line(msgs[3]))
        self.assertEqual(rh._message_line(msgs[4]), rp._message_line(msgs[4]))
        self.assertTrue(rh._message_line(msgs[5]).startswith('\u26a0\ufe0f Unknown'))
        self.assertTrue(rh._message_line(msgs[6]).startswith('\U0001f6ab'))
        now = 1_800_000_000
        a_lines, a_st = rh.add_headers(_lines(rh, msgs), 30, {}, now=now)
        b_lines, b_st = rp.add_headers(_lines(rp, msgs), 30, {}, now=now)
        self.assertEqual([(ln.get('id'), ln.get('text'), bool(ln.get('header'))) for ln in a_lines],
                         [(ln.get('id'), ln.get('text'), bool(ln.get('header'))) for ln in b_lines])
        self.assertEqual(a_st, b_st)
        self.assertNotIn('Alex', json.dumps(a_st))
        self.assertNotIn('Sam', json.dumps(a_st))
        path = os.path.join(tmpdir(), 'header_state.json')
        self.assertTrue(rh.save_state(a_st, path))
        raw = open(path, encoding='utf-8').read()
        self.assertNotIn('Alex', raw)
        self.assertNotIn('hello there', raw)
        self.assertNotIn('Family', raw)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o600')
        self.assertFalse(rh.save_state({'k': 'older', 'at': 1}, path))
        self.assertEqual(json.load(open(path, encoding='utf-8'))['k'], a_st['k'])
        self.assertTrue(rh.save_state({'k': 'newer', 'at': a_st['at']}, path))
        self.assertEqual(json.load(open(path, encoding='utf-8'))['k'], 'newer')
        kept, st = rh.add_headers(_lines(rh, msgs[:1]), 0, {'k': 'keep', 'at': 1})
        self.assertEqual(st, {'k': 'keep', 'at': 1})
        self.assertFalse(any(ln.get('header') for ln in kept))


class KeychainArgs(unittest.TestCase):
    def test_secret_never_in_argv_and_failed_write_deletes(self):
        rec = KeyRunner()
        written = []
        rk.store('refresh-token', 'super-secret-value', runner=rec, write_secret=lambda a, s: written.append((a, s)))
        blob = json.dumps(rec.calls)
        self.assertNotIn('super-secret-value', blob)
        self.assertIn(rk.PLACEHOLDER, rec.calls[0])
        self.assertNotIn('-A', rec.calls[0])
        self.assertIn('/usr/bin/security', rec.calls[0])
        self.assertEqual(written, [('refresh-token', 'super-secret-value')])
        with self.assertRaises(ValueError):
            rk.store('client-secret', 'has\nnewline', runner=rec, write_secret=lambda a, s: None)
        self.assertEqual(len(rec.calls), 1, 'a rejected secret never reaches security')

        def boom(account, secret):
            raise RuntimeError('update failed')

        with self.assertRaises(RuntimeError):
            rk.store('client-secret', 'sek', runner=rec, write_secret=boom)
        self.assertNotIn('sek', json.dumps(rec.calls))
        self.assertTrue(any('delete-generic-password' in c for c in rec.calls))


class XpostUnit(unittest.TestCase):
    def test_pkce_signing_and_send_body(self):
        verifier, challenge = rx.pkce_pair()
        self.assertTrue(verifier)
        self.assertNotIn('=', challenge)
        url = rx.authorize_url('clid', 'http://127.0.0.1:53682/cb', 'st', challenge)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith('https://x.com/i/oauth2/authorize?'))
        self.assertEqual(q['response_type'], ['code'])
        self.assertEqual(q['scope'], [rx.SCOPES])
        self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['code_challenge'], [challenge])
        self.assertEqual(q['redirect_uri'], ['http://127.0.0.1:53682/cb'])
        self.assertEqual(rx.callback_error('a', {'state': 'b', 'code': 'z'}), 'state_mismatch')
        rows = rx.map_signing({'data': [
            {'public_key': 'ID', 'signing_public_key': 'SG', 'public_key_version': 2,
             'identity_public_key_signature': 'SIG'},
            {'signing_public_key': 'only-signing'},
        ]}, '42')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['public_key'], 'SG')
        self.assertEqual(rows[0]['identity_public_key'], 'ID')
        self.assertEqual(rows[0]['user_id'], '42')
        self.assertEqual(rows[0]['identity_public_key_signature'], 'SIG')
        body = rx.send_body({'message_id': 'm', 'encrypted_content': 'E', 'encoded_event_signature': 'S'})
        self.assertEqual(body['encoded_message_create_event'], 'E')
        self.assertEqual(body['encoded_message_event_signature'], 'S')
        self.assertEqual(rx.participant_ids('111:222', '9'), ['111', '222'])
        self.assertEqual(rx.participant_ids('g12345', '9'), ['9'])

    def test_header_then_500_and_network_and_encrypt(self):
        secret = 'secret words'
        calls = []

        def http_500(body):
            calls.append(body)
            return (201, {}) if len(calls) == 1 else (500, {})

        def encrypt(text, notify):
            return {'message_id': 'mid-%d' % (1 if notify else 0), 'encrypted_content': 'E',
                    'encoded_event_signature': 'S'}

        out = rx.post_prepared(
            [{'text': 'HEADER-LINE', 'header': True, 'notify': False},
             {'text': secret, 'header': False, 'notify': True}], http_500, encrypt)
        self.assertFalse(out['ok'])
        self.assertTrue(out['retryable'])
        self.assertTrue(out['header_sent'])
        self.assertEqual(out['code'], 'http_500')
        self.assertNotIn(secret, json.dumps(out))
        self.assertNotIn('HEADER-LINE', json.dumps(out))

        def http_down(body):
            raise rx.NetError('down')

        net = rx.post_payloads(
            [({'header': False}, {'message_id': 'm1', 'encrypted_content': 'E', 'encoded_event_signature': 'S'})],
            http_down)
        self.assertEqual((net['ok'], net['retryable'], net['code']), (False, False, 'timeout'))

        def boom(text, notify):
            raise RuntimeError('nope ' + text)

        enc = rx.post_prepared([{'text': secret, 'header': False, 'notify': True}],
                               lambda body: (_ for _ in ()).throw(AssertionError('posted')), boom)
        self.assertEqual(enc['code'], 'encrypt_failed')
        self.assertTrue(enc['retryable'])
        self.assertNotIn(secret, json.dumps(enc))

    def test_empty_extract_does_not_post(self):
        class Chat(object):
            def import_keys(self, keys, version=None):
                pass

            def set_identity(self, user_id, version):
                pass

            def set_cache_keys(self, on):
                pass

            def set_signing_keys(self, keys):
                pass

            def extract_conversation_keys(self, events):
                return {'keys': {}, 'latest_version': None}

            def encrypt_message(self, *a, **k):
                raise AssertionError('encrypt')

        class Http(object):
            def __init__(self):
                self.posted = []

            def refresh(self):
                return True

            def get_events(self, path):
                return 200, {'meta': {'conversation_key_events': ['EV']}}

            def get_signing(self, path):
                return 200, {'data': [{'public_key': 'ID', 'signing_public_key': 'SG',
                                       'public_key_version': '1', 'user_id': '9'}]}

            def post(self, body):
                self.posted.append(body)
                raise AssertionError('posted')

        http = Http()
        out = rx.perform_post([{'text': 'hi', 'header': False, 'notify': True}], '111-222', '9', '1',
                              b'not-a-real-key', Chat(), http, cache=None)
        self.assertEqual(out['code'], 'no_conversation_key')
        self.assertTrue(out['retryable'])
        self.assertEqual(http.posted, [])

    def test_bad_stdin_is_one_public_line(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rx.main(['post'], stdin=io.StringIO('not-json'))
        self.assertEqual(code, 0)
        obj = json.loads(buf.getvalue())
        self.assertEqual((obj['ok'], obj['retryable'], obj['code']), (False, True, 'bad_request'))
        self.assertNotIn('not-json', buf.getvalue())
        self.assertNotIn('text', rx._public_outcome({'ok': False, 'retryable': True, 'code': 'x', 'text': 'secret words'}))


class RoutineDirect(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.dedupe = os.path.join(self.d, 'seen.json')
        self.helper = {'python': sys.executable, 'script': os.path.join(self.d, 'xchat_lite.py')}

    def _run(self, calls, argv_out=None):
        def runner(argv, input=b''):
            calls.append(json.loads(input.decode()))
            if argv_out is not None:
                argv_out.append(' '.join(argv))
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                'message_id': 'm', 'encoded_message_create_event': 'E', 'encoded_message_event_signature': 'S'}), '')
        return runner

    def test_direct_marker_commits_ids_and_drops_text(self):
        calls = []
        hdr = os.path.join(self.d, 'header_state.json')
        with open(hdr, 'w', encoding='utf-8') as fh:
            json.dump({'k': 'newer', 'at': 100}, fh)
        r = rp.process({'payload': {'type': 'direct', 'ids': ['m1', 'm1#hdr'],
                                    'headerState': {'k': 'older', 'at': 10}, 'text': 'secret words'},
                        'dedupe_path': self.dedupe}, runner=self._run(calls))
        self.assertEqual(r['action'], 'skip')
        self.assertEqual(r['posted_kind'], 'direct')
        self.assertEqual(r['reason'], 'already posted from the Mac')
        self.assertEqual(calls, [])
        self.assertNotIn('secret words', json.dumps(r))
        self.assertNotIn('secret words', open(self.dedupe, encoding='utf-8').read())
        seen = json.load(open(self.dedupe, encoding='utf-8'))
        self.assertIn('m1', seen)
        self.assertIn('m1#hdr', seen)
        self.assertEqual(json.load(open(hdr, encoding='utf-8'))['k'], 'newer')   # monotonic: do not rewind
        rp.process({'payload': {'type': 'direct', 'ids': ['m2'], 'headerState': {'k': 'later', 'at': 200}},
                    'dedupe_path': self.dedupe})
        self.assertEqual(json.load(open(hdr, encoding='utf-8'))['at'], 200)

    def test_header_state_empty_forces_a_header_and_skip_and_gap_default(self):
        who = rp._hkey('h:+15555550123')
        hdr = os.path.join(self.d, 'header_state.json')
        with open(hdr, 'w', encoding='utf-8') as fh:
            json.dump({'k': who, 'at': time.time()}, fh)
        calls = []
        argv = []
        payload = {'type': 'message', 'id': 'g1', 'label': 'Alex', 'handle': '+15555550123',
                   'text': 'dinner at 7?', 'receivedAt': '2026-01-01T00:00:00Z', 'localTime': '7:20 PM',
                   'headerGapMinutes': 30, 'headerState': {}}
        r = rp.process({'payload': payload, 'conversation_id': '1111-2222', 'xchat': dict(FULL),
                        'dedupe_path': self.dedupe, 'helper': self.helper}, runner=self._run(calls, argv))
        self.assertEqual(r['action'], 'send')
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]['text'].startswith('\U0001f4ac'))
        self.assertFalse(r['sends'][0]['should_notify'])
        self.assertTrue(r['sends'][1]['should_notify'])
        self.assertNotIn('dinner at 7?', ' '.join(argv))

        calls2 = []
        payload2 = dict(payload, id='g2', skipHeader=True, headerState={'k': 'before', 'at': 1},
                        headerStateAfter={'k': 'afterkey', 'at': 1900000000})
        r2 = rp.process({'payload': payload2, 'conversation_id': '1111-2222', 'xchat': dict(FULL),
                         'dedupe_path': self.dedupe, 'helper': self.helper}, runner=self._run(calls2))
        self.assertEqual(len(calls2), 1)
        self.assertNotIn('\U0001f4ac', calls2[0]['text'])
        self.assertEqual(json.load(open(hdr, encoding='utf-8'))['k'], 'afterkey')

        calls3 = []
        bare = {'type': 'message', 'id': 'g3', 'label': 'Alex', 'handle': '+15555550123', 'text': 'plain'}
        r3 = rp.process({'payload': bare, 'conversation_id': '1111-2222', 'xchat': dict(FULL),
                         'dedupe_path': self.dedupe, 'helper': self.helper}, runner=self._run(calls3))
        self.assertEqual(len(calls3), 1)
        self.assertFalse(calls3[0]['text'].startswith('\U0001f4ac'))
        self.assertTrue(r3['sends'][0]['should_notify'])
        self.assertEqual(r['ack_ids'][0], 'g1#hdr')


class DirectWatch(Base):
    def touch_venv(self):
        py = os.path.join(os.environ['IMESSAGE_RELAY_HOME'], 'xpost-venv', 'bin', 'python')
        os.makedirs(os.path.dirname(py), exist_ok=True)
        open(py, 'wb').close()
        return py

    def arm(self, outcomes):
        self.cfg['directPost'] = True
        self.touch_venv()
        self.runner = XRunner(outcomes)
        self.w = self.new_watcher()
        self.w.tick()
        return self.runner

    def parsed(self):
        out = []
        for raw in self.runner.inputs:
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8')
            out.append(json.loads(raw))
        return out

    def _msg(self, mid, text, rowid, **extra):
        m = {'rowid': rowid, 'id': mid, 'contactName': 'Test Contact', 'label': 'Test', 'handle': ALLOWED,
             'text': text, 'receivedAt': '2026-01-01T00:00:00Z', 'route': ALLOWED}
        m.update(extra)
        return m

    def test_default_off_is_one_webhook_and_no_header_state(self):
        self.assertFalse(self.m.rc.sanitize_config({})['directPost'])
        self.w.tick()
        self.db.add(ALLOWED, text='hello direct', guid='m-direct-1')
        self.w.tick()
        self.assertEqual(len(self.poster.calls), 1)
        p = self.poster.calls[0][1]
        self.assertEqual((p['type'], p['id'], p['text']), ('message', 'm-direct-1', 'hello direct'))
        self.assertNotIn('headerState', p)
        self.assertFalse(any('relay_xpost.py' in ' '.join(c) for c in self.runner.calls))
        self.assertFalse(os.path.exists(os.path.join(os.environ['IMESSAGE_RELAY_HOME'], 'header_state.json')))

    def test_fallback_uses_the_same_id(self):
        self.arm([{'ok': False, 'retryable': True, 'code': 'http_503', 'header_sent': False}])
        self.db.add(ALLOWED, text='hello direct', guid='m-direct-1')
        c = self.w.tick()
        self.assertEqual(len(self.poster.calls), 1)
        p = self.poster.calls[0][1]
        self.assertEqual((p['type'], p['id'], p['text']), ('message', 'm-direct-1', 'hello direct'))
        self.assertIn('headerState', p)
        self.assertNotIn('skipHeader', p)
        self.assertEqual(c['directPostFallback'], 1)
        self.assertEqual(len(self.parsed()), 1)
        joined = ' '.join(' '.join(c) for c in self.runner.calls)
        self.assertNotIn('hello direct', joined)
        self.assertIn('hello direct', self.parsed()[0]['texts'][-1]['text'])
        self.assertNotIn('hello direct', read_log())
        self.assertIn('http_503', read_log())

    def test_success_dedupes_and_header_state_has_no_name(self):
        self.arm([OK, OK, OK])
        self.db.add(ALLOWED, text='hello direct', guid='m-direct-1')
        c = self.w.tick()
        self.assertEqual(self.poster.types(), ['direct'])
        marker = self.poster.calls[0][1]
        self.assertNotIn('text', marker)
        self.assertEqual(marker['ids'][0], 'm-direct-1')
        self.assertIn('m-direct-1#hdr', marker['ids'])
        self.assertIn('headerState', marker)
        self.assertEqual((c['directPosted'], c['forwarded']), (1, 1))
        body = self.parsed()[0]
        self.assertTrue(body['texts'][0]['header'])
        self.assertFalse(body['texts'][0]['notify'])
        self.assertIn('hello direct', body['texts'][1]['text'])
        st = json.load(open(os.path.join(os.environ['IMESSAGE_RELAY_HOME'], 'header_state.json'), encoding='utf-8'))
        self.assertNotIn('Test', json.dumps(st))
        self.assertNotIn('hello direct', json.dumps(st))
        n = len(self.runner.inputs)
        self.w.tick()
        self.w = self.new_watcher()
        self.w.tick()
        self.assertEqual(len(self.runner.inputs), n, 'a second tick and a new watcher do not resend')

    def test_timeout_does_not_webhook_or_retry(self):
        self.arm(['timeout'])
        self.db.add(ALLOWED, text='hello direct', guid='m-direct-1')
        c = self.w.tick()
        self.assertEqual(self.poster.calls, [])
        self.assertEqual(c['directPostTimeout'], 1)
        self.assertNotIn('hello direct', read_log())
        self.assertIn('timeout', read_log())
        self.w.tick()
        self.w = self.new_watcher()
        self.w.tick()
        self.assertEqual(len(self.runner.inputs), 1)

    def test_empty_stdout_is_not_retried(self):
        self.arm(['empty'])
        self.db.add(ALLOWED, text='hello direct', guid='m-direct-1')
        self.w.tick()
        self.assertEqual(self.poster.calls, [])
        self.w.tick()
        self.assertEqual(len(self.runner.inputs), 1)

    def test_two_texts_are_two_posts_and_a_header_is_not_repeated(self):
        self.cfg['batchWebhooks'] = True
        self.arm([OK, OK])
        self.db.add(ALLOWED, text='one line', guid='m-a')
        self.db.add(ALLOWED, text='two line', guid='m-b')
        self.w.tick()
        self.assertEqual(self.poster.types(), ['direct', 'direct'])
        self.assertEqual(len(self.parsed()), 2)
        self.assertGreater(len(self.parsed()[0]['texts']), 1)     # header + text
        self.assertEqual(len(self.parsed()[1]['texts']), 1)       # same person, inside the gap
        self.assertFalse(self.parsed()[1]['texts'][0]['header'])
        for cmd in self.runner.calls:
            if cmd[-1:] == ['post']:
                blob = ' '.join(cmd)
                self.assertNotIn('one line', blob)
                self.assertNotIn('two line', blob)

    def test_reaction_stays_on_the_webhook_with_header_state(self):
        self.arm([])
        self.db.add(ALLOWED, text='Loved "hi"', assoc=2000, guid='m-tap')
        self.w.tick()
        self.assertEqual(self.runner.inputs, [])
        self.assertEqual(len(self.poster.calls), 1)
        p = self.poster.calls[0][1]
        self.assertEqual(p['type'], 'message')
        self.assertTrue(p.get('reaction'))
        self.assertIn('headerState', p)
        self.assertNotIn('Loved', json.dumps(p))

    def test_photo_with_media_is_not_batched_with_text(self):
        self.cfg['directPost'] = True
        self.cfg['batchWebhooks'] = True
        self.touch_venv()
        self.runner = XRunner([OK])
        self.w = self.new_watcher()
        photo = self._msg('m-photo', '\U0001f4f7 Photo', 50, media=[{'token': 'tok', 'size': 4}])
        text = self._msg('m-text', 'after photo', 51)
        self.w.retry = [photo, text]
        self.w._flush_now(self.m.rc.sanitize_config(self.cfg), collections.defaultdict(int))
        self.assertEqual(self.poster.types(), ['message', 'direct'])
        self.assertEqual(self.poster.calls[0][1]['text'], '\U0001f4f7 Photo')
        self.assertIn('headerState', self.poster.calls[0][1])
        self.assertNotIn('after photo', json.dumps(self.poster.calls[0][1]))
        self.assertEqual(len(self.parsed()), 1)
        self.assertIn('after photo', self.parsed()[0]['texts'][-1]['text'])
        self.assertTrue(self.m.direct.eligible({'directPost': True}, {'text': '\U0001f4f7 Photo'}))
        self.assertFalse(self.m.direct.eligible({'directPost': True}, {'text': '\U0001f4f7 Photo', 'media': [{}]}))
        self.assertFalse(self.m.direct.eligible({'directPost': True}, {'text': 'hi', 'reaction': True}))
        self.assertTrue(self.m.direct.eligible({'directPost': True}, {'text': 'hi', 'group': {'label': 'Family'}}))

    def test_digest_is_not_direct_posted(self):
        self.arm([])
        self.cfg['quietHours'] = {'start': '00:00', 'end': '23:59', 'tz': 'UTC'}
        self.now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc).timestamp()
        self.db.add(ALLOWED, text='night one', guid='m-night')
        c = self.w.tick()
        self.assertEqual((self.poster.calls, self.runner.inputs, c['held']), ([], [], 1))
        self.now = datetime(2026, 10, 18, 23, 59, tzinfo=timezone.utc).timestamp()
        self.w.tick()
        self.assertEqual(self.poster.types(), ['digest'])
        self.assertEqual(self.runner.inputs, [])
        self.assertNotIn('night one', read_log())

    def test_header_already_sent_asks_the_routine_to_skip(self):
        self.arm([{'ok': False, 'retryable': True, 'code': 'http_500', 'header_sent': True}])
        self.db.add(ALLOWED, text='hello direct', guid='m-direct-1')
        self.w.tick()
        p = self.poster.calls[0][1]
        self.assertEqual(p['id'], 'm-direct-1')
        self.assertTrue(p.get('skipHeader'))
        self.assertIn('headerStateAfter', p)
        self.assertIn('k', p['headerStateAfter'])


class XpostCtl(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.ctl = self.m.ctl

    def touch_venv(self):
        py = os.path.join(self.m.rc.APP_DIR, 'xpost-venv', 'bin', 'python')
        os.makedirs(os.path.dirname(py), exist_ok=True)
        open(py, 'wb').close()

    def ready_cfg(self):
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, {
            'directPost': False, 'xpostClientId': 'abc_DEF-123', 'xpostUserId': '4242424242',
            'xpostKeyVersion': '3', 'relayConversationId': '1111-2222', 'xpostRedirectPort': 53682,
        })

    def test_set_directpost_is_rejected(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.ctl.main(['set', 'directPost', 'true'])
        self.assertEqual(code, 1)
        self.assertFalse(self.m.rc.load_config()['directPost'])
        self.assertIn('directPost', buf.getvalue())

    def test_on_off_status_forget(self):
        self.ready_cfg()
        self.touch_venv()
        runner = KeyRunner(('refresh-token', 'client-secret', 'private-key'))
        cache = os.path.join(self.m.rc.APP_DIR, 'xchat_keys.json')
        with open(cache, 'w', encoding='utf-8') as fh:
            json.dump({'savedAt': time.time() - 15}, fh)
        view = self.ctl.cmd_xpost('status', runner=runner)
        blob = json.dumps(view)
        self.assertTrue(view['ready'])
        self.assertFalse(view['directPost'])
        self.assertTrue(view['venv'] and view['clientIdSet'] and view['userIdSet'])
        self.assertTrue(all(view['keychain'].values()))
        self.assertIsInstance(view['keyCacheAgeSeconds'], int)
        self.assertNotIn('abc_DEF-123', blob)
        self.assertNotIn('4242424242', blob)
        self.assertEqual(view['redirect'], 'http://127.0.0.1:53682/cb')
        on = self.ctl.cmd_xpost('on', runner=runner)
        self.assertTrue(on['directPost'])
        self.assertTrue(self.m.rc.load_config()['directPost'])
        off = self.ctl.cmd_xpost('off', runner=runner)
        self.assertFalse(off['directPost'])
        self.ctl.cmd_xpost('on', runner=runner)
        gone = self.ctl.cmd_xpost('forget', runner=runner)
        self.assertFalse(gone['directPost'])
        self.assertEqual(self.m.rc.read_raw_config().get('xpostClientId'), 'abc_DEF-123')
        self.assertFalse(os.path.exists(cache))
        deleted = [c[c.index('-a') + 1] for c in runner.calls if 'delete-generic-password' in c]
        self.assertEqual(sorted(deleted), ['client-secret', 'private-key', 'refresh-token'])
        bare = KeyRunner()
        with self.assertRaises(self.ctl.CtlError) as ctx:
            self.ctl.cmd_xpost('on', runner=bare)
        self.assertIn('not ready', str(ctx.exception))

    def test_setup_missing_venv_and_mock_does_not_enable(self):
        with self.assertRaises(self.ctl.CtlError) as ctx:
            self.ctl.cmd_xpost('setup', runner=lambda *a, **k: None)
        msg = str(ctx.exception)
        self.assertIn('python@3.12', msg)
        self.assertIn('install.sh', msg)
        self.assertIn('not rebuilt', msg)
        self.touch_venv()
        calls = []

        def runner(argv, **kw):
            calls.append(list(argv))
            if argv[-1] == 'setup':
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'ok': True, 'userId': '4242424242', 'userIdSet': True, 'keySaved': True,
                    'redirect': 'http://127.0.0.1:53682/cb', 'directPost': True,
                }), '')
            return subprocess.CompletedProcess(argv, 1, '', '')

        out = self.ctl.cmd_xpost('setup', runner=runner)
        self.assertTrue(out['ok'])
        self.assertTrue(out['userIdSet'])
        self.assertTrue(out['keySaved'])
        self.assertNotIn('userId', out)
        self.assertNotIn('4242424242', json.dumps(out))
        self.assertFalse(self.m.rc.load_config()['directPost'])
        self.assertTrue(any(c[-1] == 'setup' for c in calls))


class InstallGuards(unittest.TestCase):
    def test_venv_is_optional_and_uninstall_wont_touch_keychain_in_tests(self):
        inst = open(os.path.join(ROOT, 'install.sh'), encoding='utf-8').read()
        un = open(os.path.join(ROOT, 'uninstall.sh'), encoding='utf-8').read()
        self.assertIn("IMESSAGE_RELAY_ALLOW_NON_MAC:-}", inst)
        self.assertIn('chatxdk<0.6', inst)
        self.assertIn('brew install python@3.12', inst)
        self.assertIn('xpost-venv', inst)
        self.assertLess(un.index('IMESSAGE_RELAY_ALLOW_NON_MAC'), un.index('delete-generic-password'))
        self.assertIn('com.imessage-relay.xpost', un)
        self.assertIn('refresh-token', un)
        self.assertIn('client-secret', un)
        self.assertIn('private-key', un)


if __name__ == '__main__':
    unittest.main()
