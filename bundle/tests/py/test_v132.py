"""v1.3.2 NO-SHIP remediation: one regression test per fixed item. Each test is written to pass on
1.3.2 and fail on 1.3.1 (run this same file against a 1.3.1 checkout to confirm). JS-only items (owner
gate #1, frame inbox-nav #7) have their regression tests under tests/js/."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

from helpers import fresh, tmpdir, FakeRunner, FakeClock, read_log, ChatDb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

ALLOWED = '+15555550123'
OWNER = '+15550000001'
CONV = '1111-2222'


# ---------- item 3: relay_post plumbs XDG_CONFIG_HOME to xchat_lite ----------
class XdgPlumbing(unittest.TestCase):
    def test_default_runner_exports_xdg_config_home(self):
        d = tempfile.mkdtemp()
        runner = rp._default_runner({'xdg_config_home': d})
        proc = runner([sys.executable, '-c', 'import os;print(os.environ.get("XDG_CONFIG_HOME",""))'])
        self.assertEqual(proc.stdout.decode().strip(), d)

    def test_default_runner_merges_helper_env(self):
        runner = rp._default_runner({'env': {'XDG_CONFIG_HOME': '/x/sender'}})
        proc = runner([sys.executable, '-c', 'import os;print(os.environ.get("XDG_CONFIG_HOME",""))'])
        self.assertEqual(proc.stdout.decode().strip(), '/x/sender')


# ---------- item 6 + 2 + 10: relay_post formats status/refused, --in, main always prints JSON ----------
class RelayPostStatus(unittest.TestCase):
    def test_wrong_account_and_refused_are_formatted_not_errors(self):
        ev = rp.format_event({'type': 'wrong_account'})
        self.assertEqual(ev['kind'], 'wrong_account')
        self.assertFalse(ev['lines'][0]['body'])
        ev2 = rp.format_event({'type': 'refused', 'reason': 'echo', 'label': 'Alex'})
        self.assertEqual(ev2['kind'], 'refused')
        self.assertIn('Alex', ev2['lines'][0]['text'])

    def test_main_reads_in_file_and_always_prints_json(self):
        d = tempfile.mkdtemp()
        req = os.path.join(d, 'req.json')
        with open(req, 'w') as fh:
            json.dump({'payload': {'type': 'wrong_account'}, 'conversation_id': CONV}, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rp.main(['--in', req])
        out = json.loads(buf.getvalue())            # must be exactly one JSON object
        self.assertIn(out['action'], ('error', 'send'))
        self.assertIsInstance(code, int)

    def test_main_on_garbage_in_file_still_prints_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rp.main(['--in', '/nonexistent/nope.json'])
        self.assertEqual(json.loads(buf.getvalue())['action'], 'error')
        self.assertEqual(code, 2)

    def test_ack_records_ids_post_send(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'seen.json')
        res = rp.ack({'dedupe_path': p, 'ids': ['G1', 'G2']})   # post-send strict dedupe path
        self.assertEqual(res['remembered'], 2)
        self.assertTrue(rp.Dedupe(p).seen('G1'))


# ---------- item 10: Dedupe is concurrency-safe (commit merges under a lock) ----------
class DedupeConcurrency(unittest.TestCase):
    def test_commit_merges_concurrent_writers(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'seen.json')
        a = rp.Dedupe(p)
        b = rp.Dedupe(p)
        a.commit(['A1', 'A2'])
        b.commit(['B1'])                 # b never saw A's ids in memory, but must not clobber them
        fresh_reader = rp.Dedupe(p)
        for i in ('A1', 'A2', 'B1'):
            self.assertTrue(fresh_reader.seen(i), i)

    def test_commit_writes_ids_only_0600(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'sub', 'seen.json')
        rp.Dedupe(p).commit(['guid-xyz'])
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), '0o600')
        self.assertIn('guid-xyz', open(p).read())


# ---------- watcher-backed items (4 echo, 8 delivery, 11 lock-defer) ----------
class WatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.cfg = {'contacts': [{'name': 'Test Contact', 'handle': ALLOWED}], 'relayConversationId': CONV,
                    'webhookUrl': 'https://example.invalid/hook', 'webhookToken': 'tok', 'ownerHandle': OWNER,
                    'quietHours': None}
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        self.posts = []
        self.runner = FakeRunner()
        self.w = self._watcher()

    def _watcher(self):
        return self.m.watch.Watcher(poster=lambda u, p, token='', timeout=15: self.posts.append(p) or True,
                                    runner=self.runner, now=lambda: self.now,
                                    config_loader=lambda: self.m.rc.sanitize_config(self.cfg), sleeper=lambda s: None)

    def types(self):
        return [p.get('type') for p in self.posts]


class DeliveryProof(WatchBase):
    # item 8 (D2): a message the owner sent from another device just BEFORE the relay send must not count
    def test_message_before_send_time_is_not_counted_as_delivery(self):
        self.w.tick()
        self.db.add(ALLOWED, text='hello back', from_me=1, delivered=1, when=self.now - 10)  # earlier row
        self.m.rc.add_pending_send(ALLOWED, self.now, len('hello back'), label='Test')       # send at now
        self.w.tick()
        self.assertEqual(len(self.m.rc.load_pending()), 1, 'earlier row (date < send) must not resolve it')
        self.assertNotIn('"status": "delivered"', read_log())

    # item 8 (D1): two sends must not both claim the SAME outgoing row across ticks
    def test_one_row_is_not_claimed_by_two_sends(self):
        self.w.tick()
        self.m.rc.add_pending_send(ALLOWED, self.now, 5, label='Test')       # send #1 (oldest)
        self.m.rc.add_pending_send(ALLOWED, self.now + 1, 5, label='Test')   # send #2
        self.db.add(ALLOWED, text='abcde', from_me=1, delivered=1, when=self.now + 2)  # exactly ONE outgoing row
        self.w.tick()
        self.assertEqual(len(self.m.rc.load_pending()), 1, 'only one send may match the single row')
        self.w.tick()                                                         # the row stays claimed next tick
        self.assertEqual(len(self.m.rc.load_pending()), 1, 'the claimed row must not resolve the 2nd send later')
        self.assertEqual(read_log().count('"status": "delivered"'), 1)


class EchoGuard132(WatchBase):
    # item 4: short bodies are never treated as echoes (a stranger cannot block the owner's replies)
    def test_short_body_is_not_an_echo(self):
        self.m.rc.record_relayed_text('ok', self.now, handle=ALLOWED)
        self.assertEqual(self.m.rc.is_echoed_inbound('ok', None, self.now, handle=ALLOWED), False)

    def test_echo_is_keyed_by_sender(self):
        body = 'dinner at the lake house this saturday at six'
        self.m.rc.record_relayed_text(body, self.now, handle=ALLOWED)
        self.assertTrue(self.m.rc.is_echoed_inbound(body, None, self.now, handle=ALLOWED))
        self.assertFalse(self.m.rc.is_echoed_inbound(body, None, self.now, handle='+15559999999'),
                         'a different sender must not be blocked by someone else\u2019s relayed text')


class LockDefer(WatchBase):
    # item 11: a locked chat.db during the row loop defers the row instead of dropping it
    def test_operational_error_defers_the_row(self):
        self.w.tick()                                   # baseline
        r = self.db.add(ALLOWED, text='after the lock', when=self.now + 1)
        orig = self.w._decide
        calls = {'n': 0}

        def flaky(*a, **k):
            calls['n'] += 1
            if calls['n'] == 1:
                raise sqlite3.OperationalError('database is locked')
            return orig(*a, **k)
        self.w._decide = flaky
        self.w.tick()                                   # first attempt: locked -> deferred, cursor not advanced
        self.assertEqual(self.types(), [], 'nothing forwarded while locked')
        self.assertLess(self.m.rc.read_json(self.m.watch.STATE_PATH)['lastRowid'], r)
        self.w._decide = orig
        self.w.tick()                                   # next tick re-reads the same row and forwards it
        self.assertIn('message', self.types())


# ---------- item 9: AppleScript error classification ----------
class Classify(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())

    def test_minus_1728_is_no_imessage(self):
        self.assertEqual(self.m.rc.classify_osascript_error('error ... (-1728)'), 'no_imessage')

    def test_automation_and_busy_unchanged(self):
        self.assertEqual(self.m.rc.classify_osascript_error('(-1743)'), 'automation_denied')
        self.assertEqual(self.m.rc.classify_osascript_error('(-1712)'), 'messages_busy')


# ---------- item 13: BOT_FORMAT_RE covers every status line relay_post can post ----------
class BotFormat(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())

    def test_every_status_line_shape_is_recognized(self):
        shapes = []
        for payload in ({'type': 'sent', 'label': 'Alex'},
                        {'type': 'send_failed', 'label': 'Alex', 'error': '5'},
                        {'type': 'needs_target'},
                        {'type': 'blocked', 'label': 'Alex'},
                        {'type': 'unblocked', 'label': 'Alex'},
                        {'type': 'status', 'status': 'catching_up'},
                        {'type': 'status', 'status': 'resynced'},
                        {'type': 'status', 'status': 'messages_not_running'},
                        {'type': 'status', 'status': 'fda_missing'},
                        {'type': 'status', 'status': 'whatever', 'detail': 'hi'},
                        {'type': 'wrong_account'},
                        {'type': 'refused', 'reason': 'echo', 'label': 'Alex'}):
            shapes.append(rp.format_event(payload)['lines'][0]['text'])
        for line in shapes:
            self.assertTrue(self.m.rc.looks_like_bot_output(line), 'unrecognized bot line: %r' % line)

    def test_ordinary_text_is_not_bot_output(self):
        for t in ('hey are we still on tonight', 'Mute the TV please', 'blockchain news'):
            self.assertFalse(self.m.rc.looks_like_bot_output(t), t)


# ---------- item 14: OTP redaction table ----------
class Redact(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())

    def test_table(self):
        red = self.m.watch.redact_codes
        hidden = '[code hidden]'
        # (text, should_be_hidden)
        keep = [
            'your zip code is 90210',
            'the dress code is 2 for the party',
            'please deposit 5000 by friday',
            'area code 214 works',
            'promo code SAVE20 at checkout',
            'see you at 7',
        ]
        hide = [
            'your verification code is 123456',
            'code is 445566',
            'one-time passcode: 8891',
            'your login code is AB12CD',
            'use code 4820 to sign in',
        ]
        for t in keep:
            self.assertNotIn(hidden, red(t), 'wrongly redacted: ' + t)
        for t in hide:
            self.assertIn(hidden, red(t), 'failed to redact: ' + t)


# ---------- item 15: host fingerprint is an HMAC with a per-install secret ----------
class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())

    def test_fingerprint_is_hmac_not_plain_sha256(self):
        import hashlib
        fp = self.m.host.fingerprint('+15555550123', 'hello world')
        plain = hashlib.sha256(('+15555550123' + '\x00' + 'hello world').encode()).hexdigest()
        self.assertNotEqual(fp, plain, 'must be keyed (HMAC), not a bare sha256')
        self.assertEqual(len(fp), 64)
        # stable for the same input under the same install secret
        self.assertEqual(fp, self.m.host.fingerprint('+15555550123', 'hello world'))


# ---------- item 6: host turns a browser signal into a body-free wrong_account webhook ----------
class HostSignal(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.posts = []

    def poster(self, url, payload, token='', timeout=15):
        self.posts.append(payload)
        return True

    def test_wrong_account_signal_posts_body_free_webhook(self):
        cfg = self.m.rc.sanitize_config({'relayConversationId': CONV, 'webhookUrl': 'https://x.invalid/h',
                                         'ownerHandle': OWNER})
        r = self.m.host.handle_request({'type': 'signal', 'status': 'wrong_account'}, cfg=cfg,
                                       now=lambda: 1_800_000_000.0, poster=self.poster)
        self.assertTrue(r['ok'])
        self.assertEqual(self.posts[-1]['type'], 'wrong_account')
        self.assertNotIn('text', self.posts[-1])


# ---------- nits: label apostrophe, relayConversationId migration, quiet-hours tz validation ----------
class Nits(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())

    def test_label_keeps_apostrophe(self):
        self.assertEqual(self.m.rc.clean_label("O'Brien"), "O'Brien")

    def test_top_level_xconversationid_is_migrated(self):
        cfg = self.m.rc.sanitize_config({'xConversationId': '1000001-2000002'})
        self.assertEqual(cfg['relayConversationId'], '1000001-2000002')
        self.assertNotIn('xConversationId', cfg)

    def test_quiet_hours_rejects_a_bad_timezone(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.m.ctl.main(['quiet-hours', '22:00', '07:00', '--tz', 'Mars/Phobos'])
        self.assertNotEqual(code, 0)
        self.assertIn('unknown time zone', buf.getvalue())
        self.assertIsNone(self.m.rc.load_config()['quietHours'], 'bad tz must not be stored')

    def test_quiet_hours_accepts_a_real_timezone(self):
        rc = self.m.ctl.main(['quiet-hours', '22:00', '07:00', '--tz', 'America/Chicago'])
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
