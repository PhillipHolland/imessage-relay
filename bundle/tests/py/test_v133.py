"""v1.3.3 NO-SHIP / warnings remediation: one regression test per fixed item. Each test passes on
1.3.3 and fails on the frozen 1.3.2 checkout (run this same file against a 1.3.2 tree to confirm).
JS-only items (owner gate T7-T9, the quoted/u= twid parser) have their regression tests under
tests/js/owner.test.js."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

from helpers import fresh, tmpdir, FakeRunner, read_log, ChatDb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

A = '+15555550123'
B = '+15555550199'
OWNER = '+15550000001'
CONV = '1111-2222'
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


class HostBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.cfgd = {'contacts': [{'name': 'Sam Alpha', 'handle': A}, {'name': 'Bob Beta', 'handle': B}],
                     'relayConversationId': CONV, 'webhookUrl': 'https://x.invalid/h', 'webhookToken': 't',
                     'ownerHandle': OWNER, 'ownerHandles': [OWNER], 'quietHours': None, 'dryRun': False}
        self._write()
        self.posts = []
        self.runner = FakeRunner([(0, '')] * 40)
        self.t = NOW

    def _write(self):
        with open(self.m.rc.CONFIG_PATH, 'w') as f:
            json.dump(self.cfgd, f)

    def send(self, text, quoted='', t=None):
        if t is not None:
            self.t = t
        cfg = self.m.rc.load_config()
        return self.m.host.handle_request(
            {'type': 'send', 'text': text, 'quotedText': quoted, 'conversationId': CONV, 'ts': ''},
            cfg=cfg, runner=self.runner, sleeper=lambda x: None, now=lambda: self.t,
            poster=lambda u, p, token='', timeout=4: self.posts.append(p) or True)

    def osa(self):
        return [c for c in self.runner.calls if c[0] == '/usr/bin/osascript']


# ---------- item N1: bare block/unblock nudges, sends nothing ----------
class BareCommandHint(HostBase):
    def test_bare_block_does_not_text_and_nudges(self):
        self.m.rc.record_inbound(A, NOW - 30, label='Sam')
        n = len(self.osa())
        r = self.send('block', t=NOW)
        self.assertTrue(r.get('skipped'), r)
        self.assertEqual(len(self.osa()), n, 'bare "block" must not text anyone')
        self.assertEqual(self.m.rc.load_config()['mute'], [])
        self.assertIn('/block', self.posts[-1].get('detail', ''))

    def test_typing_unblock_known_name_does_not_text(self):
        self.m.rc.record_inbound(B, NOW - 10, label='Bob')
        n = len(self.osa())
        r = self.send('unblock Sam', t=NOW)      # Sam is a known contact -> nudge, send nothing
        self.assertEqual(len(self.osa()), n, 'typing "unblock Sam" must not text Sam')
        self.assertTrue(r.get('skipped'), r)

    def test_ordinary_block_sentence_still_sends(self):
        self.m.rc.record_inbound(A, NOW - 10, label='Sam')
        n = len(self.osa())
        self.send('Block party tonight?', t=NOW)
        self.assertGreater(len(self.osa()), n, 'ordinary prose must still be relayed')


# ---------- item N2: "/block <name> yes" confirms only the staged target ----------
class ConfirmTarget(HostBase):
    def test_block_wrong_name_yes_does_not_mute_staged(self):
        self.m.rc.record_inbound(A, NOW - 30, label='Sam')
        self.send('/block Sam', t=NOW)           # stage Sam
        self.send('/block Bob yes', t=NOW + 5)   # confirm names Bob, not Sam
        self.assertNotIn(A, self.m.rc.load_config()['mute'], 'Sam must not be muted by "/block Bob yes"')


# ---------- item N4: group chats can be muted by chat id; no junk entries ----------
class GroupMute(HostBase):
    def test_sanitize_keeps_group_id_and_drops_junk(self):
        cfg = self.m.rc.sanitize_config({'mute': ['group:chat0001', '+01', A]})
        self.assertIn('group:chat0001', cfg['mute'])
        self.assertNotIn('+01', cfg['mute'])
        self.assertIn(A, cfg['mute'])

    def test_muted_group_chat_is_silenced(self):
        self.cfgd.update(relayAll=True)
        self._write()
        posts = []
        w = self.m.watch.Watcher(poster=lambda u, p, token='', timeout=15: posts.append(p) or True,
                                 runner=FakeRunner(), now=lambda: self.t, config_loader=self.m.rc.load_config,
                                 sleeper=lambda s: None)
        self.t = NOW
        w.tick()
        sp = '+15557770000'
        self.db.add(sp, text='VOTE YES ON PROP 9 reply STOP', when=NOW + 1, group='grp1',
                    display_name='Spam Group', members=(sp, '+15557770001'))
        self.t = NOW + 2
        w.tick()
        line = [p for p in posts if p.get('type') == 'message'][-1]
        q = '%s \u00b7 %s: %s' % (line['group']['label'], line['label'], line['text'])
        self.send('/block', quoted=q, t=NOW + 5)
        self.send('/block yes', t=NOW + 6)
        mute = self.m.rc.load_config()['mute']
        self.assertTrue(any(x.startswith('group:') for x in mute), mute)
        self.assertNotIn('+01', mute)
        self.db.add(sp, text='VOTE YES AGAIN reply STOP now', when=NOW + 10, group='grp1',
                    display_name='Spam Group', members=(sp, '+15557770001'))
        n = len(posts)
        self.t = NOW + 12
        w.tick()
        self.assertFalse([p for p in posts[n:] if p.get('type') == 'message'], 'a muted group must not relay')


# ---------- item N5: relayctl config writes take the shared host lock ----------
class ConfigLock(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        with open(self.m.rc.CONFIG_PATH, 'w') as f:
            json.dump({'contacts': [], 'mute': []}, f)

    def test_relayctl_edit_does_not_clobber_host_mute(self):
        orig = self.m.rc.read_raw_config

        def slow(*a, **k):
            r = orig(*a, **k)
            if threading.current_thread().name == 'ctl':
                time.sleep(0.3)
            return r
        self.m.rc.read_raw_config = slow
        try:
            t = threading.Thread(name='ctl', target=lambda: self.m.ctl.edit_config(
                lambda raw: raw.__setitem__('replyWindowMinutes', 30)))
            t.start()
            time.sleep(0.1)
            self.m.host.toggle_mute(A, True)     # races the slow relayctl edit
            t.join()
        finally:
            self.m.rc.read_raw_config = orig
        cfg = json.load(open(self.m.rc.CONFIG_PATH))
        self.assertEqual(cfg.get('mute'), [A], 'host mute must survive a concurrent relayctl edit')
        self.assertEqual(cfg.get('replyWindowMinutes'), 30)


# ---------- item N6: quiet hours default to null (never auto-filled with 22:00-07:00) ----------
class QuietHoursDefault(unittest.TestCase):
    def test_config_example_quiethours_is_null(self):
        ex = os.path.join(os.path.dirname(__file__), '..', '..', 'mac-helper', 'config.example.json')
        self.assertIsNone(json.load(open(ex)).get('quietHours'))

    def test_missing_quiethours_stays_null(self):
        rc = fresh(tmpdir()).rc
        self.assertIsNone(rc.sanitize_config({'contacts': []})['quietHours'])


# ---------- item N7: relay_post deletes the --in file after reading it ----------
class InFileDeletion(unittest.TestCase):
    def _run_in(self, body):
        d = tempfile.mkdtemp()
        req = os.path.join(d, 'req.json')
        with open(req, 'w') as fh:
            fh.write(body)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rp.main(['--in', req])
        return req, buf.getvalue()

    def test_in_file_deleted_after_read(self):
        req, out = self._run_in(json.dumps({'payload': {'type': 'wrong_account'}, 'conversation_id': CONV}))
        self.assertFalse(os.path.exists(req), 'the request file (with body) must be removed after reading')

    def test_in_file_deleted_even_on_bad_json(self):
        req, out = self._run_in('{ not valid json')
        self.assertFalse(os.path.exists(req))
        self.assertEqual(json.loads(out)['action'], 'error')


# ---------- item 10: relay_post --ack takes ids on argv ----------
class AckArgv(unittest.TestCase):
    def test_ack_ids_on_argv(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'seen.json')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rp.main(['--ack', '--dedupe-path', p, 'ID1', 'ID2'])
        out = json.loads(buf.getvalue())
        self.assertEqual(out['action'], 'ack')
        self.assertEqual(out['remembered'], 2)
        self.assertTrue(rp.Dedupe(p).seen('ID1'))


# ---------- item 8: a late unrelated outgoing row is NOT reported delivered ----------
class DeliveryFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.cfg = {'contacts': [{'name': 'Test', 'handle': A}], 'relayConversationId': CONV,
                    'webhookUrl': 'https://x.invalid/h', 'webhookToken': 't', 'ownerHandle': OWNER, 'quietHours': None}
        self.posts = []
        self.t = NOW
        self.w = self.m.watch.Watcher(poster=lambda u, p, token='', timeout=15: self.posts.append(p) or True,
                                      runner=FakeRunner(), now=lambda: self.t,
                                      config_loader=lambda: self.m.rc.sanitize_config(self.cfg), sleeper=lambda s: None)
        self.w.tick()

    def test_late_unrelated_row_not_delivered(self):
        self.m.rc.add_pending_send(A, NOW, 11, label='Sam')
        # only the owner's own iPhone message (NULL text, length 0), 5s after the send -> unrelated
        self.db.add(A, text=None, ab=b'y', from_me=1, delivered=1, when=NOW + 5)
        self.t = NOW + 6
        self.w.tick()
        self.assertNotIn('"status": "delivered"', read_log())
        self.assertEqual(len(self.m.rc.load_pending()), 1, 'the send stays pending, not falsely delivered')


# ---------- item 11: a locked chat.db is "busy", never reported as fda_missing ----------
class LockedDbBusy(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.cfg = {'contacts': [{'name': 'Test', 'handle': A}], 'relayConversationId': CONV,
                    'webhookUrl': 'https://x.invalid/h', 'webhookToken': 't', 'ownerHandle': OWNER, 'quietHours': None}
        self.posts = []
        self.t = NOW
        self.w = self.m.watch.Watcher(poster=lambda u, p, token='', timeout=15: self.posts.append(p) or True,
                                      runner=FakeRunner(), now=lambda: self.t,
                                      config_loader=lambda: self.m.rc.sanitize_config(self.cfg), sleeper=lambda s: None)
        self.w.tick()

    def test_chatdbbusy_exists_and_is_distinct(self):
        self.assertTrue(hasattr(self.m.rc, 'ChatDbBusy'))
        self.assertFalse(issubclass(self.m.rc.ChatDbBusy, self.m.rc.ChatDbUnavailable))

    def test_locked_open_does_not_post_fda_missing(self):
        self.db.add(A, text='hello during a lock', when=NOW + 1)
        lk = sqlite3.connect(os.environ['IMESSAGE_RELAY_CHAT_DB'], timeout=0)
        lk.execute('PRAGMA locking_mode=EXCLUSIVE')
        lk.execute('BEGIN EXCLUSIVE')
        try:
            self.t = NOW + 2
            counts = self.w.tick()
        finally:
            lk.rollback()
            lk.close()
        self.assertNotIn('fda_missing', [p.get('status') for p in self.posts])
        self.assertFalse([p for p in self.posts if p.get('type') == 'message'])
        self.assertGreaterEqual(counts.get('dbBusy', 0), 1)
        # after the lock clears the message still forwards (nothing dropped)
        self.t = NOW + 15
        self.w.tick()
        self.assertIn('hello during a lock', [p.get('text') for p in self.posts if p.get('type') == 'message'])


# ---------- item 13: the host's structured routing replies are recognized as bot output ----------
class BotLines(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc

    def test_structured_host_lines_recognized(self):
        for l in ("Can't send to Sam: they aren't one of your relay contacts. Ask me to add them.",
                  "Can't send to (555) 123-4567: they have never texted you. Ask me to add them as a contact first.",
                  "Group replies aren't supported yet. To text one person, start with their name, like Sam: \u2026",
                  'Did you mean /block \u2026? Nothing was sent.'):
            self.assertTrue(self.rc.looks_like_bot_output(l), l)

    def test_ordinary_lines_not_flagged(self):
        self.assertFalse(self.rc.looks_like_bot_output("Can't send to you the photos, sorry"))
        self.assertFalse(self.rc.looks_like_bot_output('Who is coming tonight?'))


# ---------- item 6: each wrong-account EPISODE gets a unique webhook id ----------
class WrongAccountEpisodeId(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.cfg = self.m.rc.sanitize_config({'contacts': [], 'relayConversationId': CONV,
                                              'webhookUrl': 'https://x.invalid/h', 'webhookToken': 't'})

    def test_id_unique_per_episode(self):
        posts = []
        poster = lambda u, p, token='', timeout=4: posts.append(p) or True  # noqa: E731
        for at in ('2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'):
            self.m.host.handle_request({'type': 'signal', 'status': 'wrong_account', 'at': at},
                                       cfg=self.cfg, now=lambda: 0, poster=poster)
        ids = [p.get('id') for p in posts]
        self.assertTrue(all(ids), 'each wrong_account webhook must carry an id')
        self.assertEqual(len(set(ids)), 2, 'two episodes -> two distinct ids (not dedupe-suppressed)')


# ---------- item 14: OTP redaction (11-digit code hidden; a phone number is not) ----------
class Redaction(unittest.TestCase):
    def setUp(self):
        self.R = fresh(tmpdir()).watch.redact_codes

    def test_eleven_digit_code_is_redacted(self):
        self.assertIn('[code hidden]', self.R('Your code is 12345678901 do not share'))

    def test_phone_number_is_not_redacted(self):
        self.assertNotIn('[code hidden]', self.R('verification: call 800-555-1234 for help'))


# ---------- echo guard: trailing punctuation is ignored (nit) ----------
class EchoNorm(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc

    def test_trailing_punctuation_variant_is_still_an_echo(self):
        now = 1_800_000_000.0
        body = 'are you coming to dinner at the lake house saturday'
        self.rc.record_relayed_text(body, now, handle=A)
        self.assertTrue(self.rc.is_echoed_inbound(body + '.', body + '.', now + 5, handle=A))


# ---------- item N3: relay_post expands ~ and locates the upstream helper clone ----------
class XdgExpansion(unittest.TestCase):
    def _with_home(self, h, fn):
        old = os.environ.get('HOME')
        os.environ['HOME'] = h
        try:
            return fn()
        finally:
            if old is not None:
                os.environ['HOME'] = old

    def test_default_runner_expands_tilde(self):
        h = tempfile.mkdtemp()

        def go():
            runner = rp._default_runner({'xdg_config_home': '~/xchat/sender'})
            proc = runner([sys.executable, '-c', 'import os;print(os.environ.get("XDG_CONFIG_HOME",""))'])
            return proc.stdout.decode().strip()
        self.assertEqual(self._with_home(h, go), os.path.join(h, 'xchat/sender'))

    def test_locate_helper_finds_xchat_grokbot_helper(self):
        h = tempfile.mkdtemp()

        def go():
            root = os.path.join(h, 'xchat-grokbot-helper')
            os.makedirs(root)
            open(os.path.join(root, 'xchat_lite.py'), 'w').write('')
            _py, script = rp.locate_helper(None)
            return script
        self.assertEqual(self._with_home(h, go), os.path.join(h, 'xchat-grokbot-helper', 'xchat_lite.py'))


if __name__ == '__main__':
    unittest.main()
