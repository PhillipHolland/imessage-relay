"""v1.3.8: the /test end-to-end check (host, watcher probe, relay_post rendering, never texts a
contact, every step time-boxed) and the guided setup helpers (owner-handle detection from chat.db,
relayctl setup / test-result, relay-all pending until the owner's handles are known)."""
import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest

from helpers import fresh, tmpdir, read_log, FakeClock, FakeRunner, ChatDb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

CONV = '1111-2222'
CONTACT = '+15555550123'
OWNER = '+15555550199'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.h, self.rc = self.m.host, self.m.rc
        self.clock = FakeClock()
        self.raw = {'dryRun': True, 'relayConversationId': CONV, 'contacts': [{'name': 'Test Contact', 'handle': CONTACT}],
                    'ownerHandle': OWNER, 'webhookUrl': 'https://example.invalid/hook', 'webhookToken': 'tok'}
        self.posts = []
        self.post_ok = True
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.db.add(CONTACT, text='old history')
        self.runner = FakeRunner(results=[(0, '')] * 4)
        self.watcher = self.m.watch.Watcher(poster=lambda *a, **k: True, runner=self.runner, now=self.clock.now,
                                            config_loader=lambda: self.cfg(), sleeper=lambda s: None)
        self.watcher_up = True

    def cfg(self, **over):
        return self.rc.sanitize_config(dict(self.raw, **over))

    def poster(self, url, payload, token='', timeout=15):
        self.posts.append(json.loads(json.dumps(payload)))
        self.timeouts = getattr(self, 'timeouts', []) + [timeout]
        return self.post_ok

    def sleeper(self, s):
        if self.watcher_up:                          # the watcher polls jobs every second
            self.watcher.process_jobs(self.cfg())
        self.clock.sleep(s)

    def run_test_cmd(self, send=False, conv=CONV, **over):
        msg = {'type': 'test', 'testId': 'abcdef123456', 'send': send, 'extVersion': '1.3.8', 'conversationId': conv}
        return self.h.handle_request(msg, cfg=self.cfg(**over), runner=self.runner, sleeper=self.sleeper,
                                     now=self.clock.now, poster=self.poster)

    def steps(self, r):
        return {s['name']: s for s in r['steps']}


class HostTest(Base):
    def test_all_green_reports_every_step_and_posts_one_body_free_relay_test(self):
        r = self.run_test_cmd()
        st = self.steps(r)
        self.assertTrue(r['ok'], r)
        self.assertEqual([s['name'] for s in r['steps']], ['extension', 'host', 'watcher', 'messages', 'webhook', 'routine'])
        for n in ('extension', 'host', 'watcher', 'messages', 'webhook'):
            self.assertIs(st[n]['ok'], True, n)
        self.assertIsNone(st['routine']['ok'])           # decided by the extension when the report shows up
        self.assertIn('v1.3.8', st['extension']['detail'])
        self.assertIn(self.rc.VERSION, st['host']['detail'])
        self.assertEqual(len(self.posts), 1)
        p = self.posts[0]
        self.assertEqual((p['type'], p['id'], p['tag']), ('relay_test', 'relay_test:abcdef123456', 'abcdef'))
        self.assertEqual([s['name'] for s in p['steps']], ['extension', 'host', 'watcher', 'messages'])
        self.assertNotIn('text', p)
        self.assertEqual(self.runner.count('/usr/bin/osascript'), 0, 'a plain /test never sends an iMessage')
        last = self.rc.read_json(os.path.join(self.rc.APP_DIR, 'selftest', 'test-last.json'))
        self.assertEqual(last['testId'], 'abcdef123456')
        self.assertLessEqual(max(self.timeouts), 6, 'webhook POST is time-boxed')

    def test_watcher_down_is_reported_failed_after_its_timeout_not_a_hang(self):
        self.watcher_up = False
        r = self.run_test_cmd()
        st = self.steps(r)
        self.assertFalse(r['ok'])
        self.assertIs(st['watcher']['ok'], False)
        self.assertIn('did not answer', st['watcher']['detail'])
        self.assertLessEqual(sum(self.clock.sleeps), self.m.rtest.WATCHER_WAIT_S + 1)
        self.assertIs(st['messages']['ok'], True, 'falls back to the host pgrep')
        self.assertEqual([n for n in os.listdir(os.path.join(self.rc.APP_DIR, 'jobs'))], [], 'stale probe removed')
        self.assertIs(st['webhook']['ok'], True, 'the report still reaches X Chat with the failure in it')

    def test_watcher_without_full_disk_access(self):
        os.unlink(self.db.path)
        st = self.steps(self.run_test_cmd())
        self.assertIs(st['watcher']['ok'], False)
        self.assertIn('Full Disk Access', st['watcher']['detail'])

    def test_messages_not_running(self):
        self.runner.messages_running = False
        st = self.steps(self.run_test_cmd())
        self.assertIs(st['messages']['ok'], False)

    def test_no_webhook_and_rejected_webhook(self):
        st = self.steps(self.run_test_cmd(webhookUrl=''))
        self.assertIs(st['webhook']['ok'], False)
        self.assertIs(st['routine']['ok'], False)
        self.assertEqual(self.posts, [])
        self.post_ok = False
        r = self.run_test_cmd()
        self.assertFalse(r['webhookAccepted'])
        self.assertIs(self.steps(r)['webhook']['ok'], False)

    def test_send_texts_only_the_owners_own_handle_and_honors_dry_run(self):
        st = self.steps(self.run_test_cmd(send=True))
        self.assertIsNone(st['send']['ok'])
        self.assertIn('dry run', st['send']['detail'])
        self.assertEqual(self.runner.count('/usr/bin/osascript'), 0)
        st = self.steps(self.run_test_cmd(send=True, dryRun=False))
        self.assertIs(st['send']['ok'], True)
        sends = [c for c in self.runner.calls if c[0] == '/usr/bin/osascript']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][3], OWNER)
        self.assertNotIn(CONTACT, json.dumps(self.runner.calls))
        self.assertEqual(sends[0][2], self.m.rtest.TEST_TEXT)

    def test_send_refused_without_owner_handle_or_when_it_is_a_contact(self):
        st = self.steps(self.run_test_cmd(send=True, dryRun=False, ownerHandle=''))
        self.assertIs(st['send']['ok'], False)
        st = self.steps(self.run_test_cmd(send=True, dryRun=False, ownerHandle=CONTACT))
        self.assertIs(st['send']['ok'], False)
        self.assertEqual(self.runner.count('/usr/bin/osascript'), 0)

    def test_only_from_the_relay_chat(self):
        r = self.run_test_cmd(conv='9999-8888')
        self.assertFalse(r['ok'])
        self.assertEqual(r['error'], 'not the relay conversation')
        self.assertEqual(self.posts, [])

    def test_runs_while_paused_and_with_no_contacts(self):
        r = self.run_test_cmd(paused=True, contacts=[])
        self.assertEqual(r['command'], 'test')
        self.assertTrue(r['ok'])

    def test_old_extension_forwarding_slash_test_as_a_reply_is_never_routed(self):
        for paused in (False, True):
            r = self.h.handle_request({'type': 'send', 'text': '/test', 'conversationId': CONV, 'ts': 'x'},
                                      cfg=self.cfg(paused=paused, dryRun=False), runner=self.runner, sleeper=self.sleeper,
                                      now=self.clock.now, poster=self.poster)
            self.assertEqual(r['command'], 'test')
        self.assertEqual(self.runner.count('/usr/bin/osascript'), 0, 'never texted the last sender "/test"')
        r = self.h.handle_request({'type': 'send', 'text': '/test send', 'conversationId': CONV, 'ts': 'x'},
                                  cfg=self.cfg(dryRun=False), runner=self.runner, sleeper=self.sleeper,
                                  now=self.clock.now, poster=self.poster)
        sends = [c for c in self.runner.calls if c[0] == '/usr/bin/osascript']
        self.assertEqual([c[3] for c in sends], [OWNER])

    def test_parser_and_help(self):
        p = self.h.parse_owner_command
        self.assertEqual(p('/test'), {'cmd': 'test', 'send': False})
        self.assertEqual(p('  /TEST send '), {'cmd': 'test', 'send': True})
        self.assertIsNone(p('/testing 123'))
        self.assertIsNone(p('test'))
        self.assertIn('/test', self.h.HELP_TEXT)

    def test_routine_signal_updates_last_result_locally_only(self):
        self.run_test_cmd()
        n = len(self.posts)
        r = self.h.handle_request({'type': 'signal', 'status': 'test_seen', 'testId': 'abcdef123456'}, cfg=self.cfg(),
                                  now=self.clock.now, poster=self.poster)
        self.assertTrue(r['ok'])
        last = self.rc.read_json(os.path.join(self.rc.APP_DIR, 'selftest', 'test-last.json'))
        self.assertIs([s for s in last['steps'] if s['name'] == 'routine'][0]['ok'], True)
        self.assertEqual(len(self.posts), n, 'a routine signal never posts to the webhook')
        r = self.h.handle_request({'type': 'signal', 'status': 'test_unseen', 'testId': 'nope'}, cfg=self.cfg(),
                                  now=self.clock.now, poster=self.poster)
        self.assertFalse(r['ok'])

    def test_bad_test_id_is_replaced(self):
        r = self.h.handle_request({'type': 'test', 'testId': '../../etc', 'conversationId': CONV}, cfg=self.cfg(),
                                  runner=self.runner, sleeper=self.sleeper, now=self.clock.now, poster=self.poster)
        self.assertRegex(r['testId'], r'^[0-9a-f]{12}$')

    def test_extension_version_is_noted_throttled(self):
        path = os.path.join(self.rc.APP_DIR, 'ext_seen.json')
        self.h.handle_request({'type': 'get_config', 'extVersion': '1.3.7'}, cfg=self.cfg(), now=self.clock.now)
        self.assertEqual(self.rc.read_json(path)['extVersion'], '1.3.7')
        at = self.rc.read_json(path)['at']
        self.clock.t += 10
        self.h.handle_request({'type': 'get_config', 'extVersion': '1.3.7'}, cfg=self.cfg(), now=self.clock.now)
        self.assertEqual(self.rc.read_json(path)['at'], at, 'throttled')
        self.h.handle_request({'type': 'get_config', 'extVersion': '1.3.8'}, cfg=self.cfg(), now=self.clock.now)
        self.assertEqual(self.rc.read_json(path)['extVersion'], '1.3.8', 'a version change is written at once')

    def test_no_message_bodies_written(self):
        self.db.add(CONTACT, text='secret body zz9')
        self.run_test_cmd(send=True)
        for root, _, files in os.walk(self.tmp):
            for f in files:
                p = os.path.join(root, f)
                if p != self.db.path:
                    with open(p, 'rb') as fh:
                        self.assertNotIn(b'secret body zz9', fh.read(), p)


# v1.4.22 rewrote the chat lines. The golden file still stores the v1.3.7 wording; these are the
# same payloads rendered in the current voice. /test rows are not in this map.
_V1422_LINE = {
    '\u2192 Alex \u2713\u2713 delivered': '\u2192 Alex \u2713\u2713',
    '\u2192 Alex \u2713 sent': '\u2192 Alex \u2713',
    '\u26a0\ufe0f Couldn\u2019t deliver your reply to Alex \u2014 it may not have sent (not_delivered).':
        '\u26a0\ufe0f Didn\u2019t reach Alex.',
    '\u26a0\ufe0f Couldn\u2019t confirm your reply to Alex was delivered.':
        '\u26a0\ufe0f Couldn\u2019t confirm it reached Alex.',
    '\u26a0\ufe0f Correction: your reply to Alex actually failed to deliver \u2014 disregard the earlier \u2713 sent (not_delivered).':
        '\u26a0\ufe0f The earlier \u2713 to Alex was wrong. It did not deliver.',
    '\u2753 Send to Alex? Reply yes within 2 min.': 'Send to Alex? Reply yes within 2 min.',
    '\u2753 Reply to Alex? Reply yes within 2 min.': 'Reply to Alex? Reply yes within 2 min.',
    '\U0001f6ab Muted Alex. Say "/unblock Alex" to undo.': 'Muted Alex. /unblock Alex',
    '\u2705 Unmuted Alex.': 'Unmuted Alex.',
    '\u23f3 Catching up \u2014 relaying a burst in batches (nothing dropped).':
        'Catching up. Nothing is dropped.',
    '\U0001f504 Resynced after a long gap; older messages were skipped.':
        'Older messages were skipped after a gap.',
    '\u26a0\ufe0f Messages isn\u2019t running on the Mac, so replies can\u2019t be sent right now.':
        '\u26a0\ufe0f Messages isn\u2019t open, so replies can\u2019t send.',
    '\u2139\ufe0f Paused. I won\u2019t relay.': 'Paused. I won\u2019t relay.',
    '\u2139\ufe0f Commands: /who': 'Commands: /who',
    '\u26a0\ufe0f The relay tab is signed in to the wrong X account \u2014 replies are paused until you switch back to the owner account.':
        '\u26a0\ufe0f Wrong X account in this tab. Replies are paused.',
    '\u231b Not sent to Alex.': 'Not sent to Alex. The question timed out.',
    '\u231b Not sent.': 'Not sent. The question timed out.',
    '\u26a0\ufe0f Didn\u2019t send that to Alex \u2014 it looked like a relayed message bounced straight back.':
        '\u26a0\ufe0f Not sent to Alex. That looked like a relayed text.',
    '\u26a0\ufe0f A reply wasn\u2019t sent to Alex.': '\u26a0\ufe0f Not sent to Alex.',
}


class RelayPostTest(unittest.TestCase):
    def payload(self, steps, tag='abcdef'):
        return {'type': 'relay_test', 'id': 'relay_test:abcdef123456', 'tag': tag, 'steps': steps}

    def test_renders_one_silent_message_one_line_per_step(self):
        steps = [{'name': 'extension', 'ok': True, 'detail': 'extension saw /test (v1.3.8)'},
                 {'name': 'host', 'ok': True, 'detail': 'native host reachable (v1.3.8)'},
                 {'name': 'watcher', 'ok': True, 'detail': 'watcher alive, can read chat.db'},
                 {'name': 'messages', 'ok': True, 'detail': 'Messages is running'}]
        ev = rp.format_event(self.payload(steps))
        self.assertEqual(ev['kind'], 'relay_test')
        self.assertEqual(len(ev['lines']), 1)
        ln = ev['lines'][0]
        self.assertFalse(ln['body'], 'silent (should_notify false) and not a relayed body')
        rows = ln['text'].split('\n')
        self.assertEqual(rows[0], '\U0001f9ea relay test #abcdef \u2014 all checks passed')
        self.assertEqual(len(rows), 1 + 6)
        self.assertEqual(rows[5], '\u2705 5 webhook POST accepted')
        self.assertEqual(rows[6], '\u2705 6 routine posted this report')

    def test_failures_dry_send_last_and_sanitized_details(self):
        steps = [{'name': 'watcher', 'ok': False, 'detail': 'watcher did\nnot answer ' + 'x' * 300},
                 {'name': 'send', 'ok': None, 'detail': 'dry run: would text your own number, nothing sent'},
                 {'name': 'messages', 'ok': True, 'detail': 'Messages is running'}, 'junk']
        rows = rp.format_event(self.payload(steps))['lines'][0]['text'].split('\n')
        self.assertTrue(rows[0].endswith('1 failed'))
        self.assertTrue(rows[1].startswith('\u274c 1 watcher did not answer'))
        self.assertLessEqual(len(rows[1]), 110)
        self.assertTrue(rows[-1].startswith('\u2796 5 dry run'))

    def test_minimal_payload_still_renders(self):
        ev = rp.format_event({'type': 'relay_test'})
        self.assertTrue(ev['lines'][0]['text'].startswith('\U0001f9ea relay test \u2014 all checks passed'))
        self.assertEqual(ev['lines'][0]['id'], 'relay_test:')

    def test_report_is_recognized_as_bot_output_so_never_relayed_back(self):
        import relay_echo
        ev = rp.format_event(self.payload([]))
        self.assertTrue(relay_echo.looks_like_bot_output(ev['lines'][0]['text']))

    def test_every_pre_138_payload_renders_exactly_as_in_137(self):
        with open(os.path.join(HERE, 'fixtures', 'relay_post_golden_137.json'), encoding='utf-8') as fh:
            golden = json.load(fh)
        self.assertGreaterEqual(len(golden), 30)
        for g in golden:
            if g['payload'].get('type') == 'digest':
                continue        # v1.4.6: the overnight recap is now ONE post (test_relay_post covers it)
            if 'error' in g:
                with self.assertRaises(ValueError):
                    rp.format_event(g['payload'])
            else:
                # v1.4.1: confirmation prompts now say "Reply yes" (a plain yes confirms).
                # v1.4.20: fda_missing renders the fix sentence (older Macs send no ⚠️ detail).
                old_fda = ('\u26a0\ufe0f The watcher can\u2019t read Messages yet (Full Disk Access). '
                           'New texts aren\u2019t coming through.')
                want = json.loads(json.dumps(g['expect'], ensure_ascii=False).replace(
                    'Say /yes within 2 min.', 'Reply yes within 2 min.').replace(old_fda, rp.FDA_LINE))
                for ln in want.get('lines') or []:
                    if ln.get('text') in _V1422_LINE:
                        ln['text'] = _V1422_LINE[ln['text']]
                self.assertEqual(rp.format_event(g['payload']), want, g['payload']['type'])

    def test_relay_test_through_process_is_silent(self):
        calls = []

        class P:
            returncode, stderr = 0, b''
            stdout = json.dumps({'message_id': 'm', 'encoded_message_create_event': 'E',
                                 'encoded_message_event_signature': 'S'}).encode()

        def runner(argv, input=b''):
            calls.append(json.loads(input.decode()))
            return P()
        d = tmpdir()
        r = rp.process({'payload': self.payload([]), 'conversation_id': 'AAA-BBB', 'xchat': {'events': [], 'conversation_key_events': ['K'], 'signing_keys': ['S']},
                        'dedupe_path': os.path.join(d, 's.json')}, runner=runner)
        self.assertEqual(r['action'], 'send')
        self.assertIs(r['sends'][0]['should_notify'], False)
        self.assertTrue(calls[0]['text'].startswith('\U0001f9ea relay test #abcdef'))


def add_account_cols(path):
    con = sqlite3.connect(path)
    con.execute('ALTER TABLE message ADD COLUMN account TEXT')
    con.execute('ALTER TABLE message ADD COLUMN destination_caller_id TEXT')
    con.commit()
    con.close()


def set_account(path, mid, account=None, caller=None):
    con = sqlite3.connect(path)
    con.execute('UPDATE message SET account = ?, destination_caller_id = ? WHERE ROWID = ?', (account, caller, mid))
    con.commit()
    con.close()


class OwnerDetect(Base):
    def setUp(self):
        super().setUp()
        add_account_cols(self.db.path)
        for _ in range(3):
            set_account(self.db.path, self.db.add(CONTACT, text='a', from_me=1), 'P:+1 (555) 555-0199', '+15555550199')
        set_account(self.db.path, self.db.add(CONTACT, text='b', from_me=1), 'E:Me@iCloud.example', 'me@icloud.example')
        set_account(self.db.path, self.db.add(CONTACT, text='c', from_me=0), 'E:inbound@other.example', None)
        set_account(self.db.path, self.db.add(CONTACT, text='d', from_me=1), 'P:' + CONTACT, None)   # odd but possible

    def test_detect_reads_only_sent_rows_most_used_first_no_text(self):
        con = sqlite3.connect(self.db.path)
        found = self.m.setup.detect_owner_handles(con, '1')
        con.close()
        self.assertEqual(found[0], OWNER)
        self.assertIn('me@icloud.example', found)
        self.assertNotIn('inbound@other.example', found)

    def test_detect_tolerates_old_schema_without_columns(self):
        d = ChatDb(os.path.join(self.tmp, 'old.db'))
        d.add(CONTACT, text='x', from_me=1)
        con = sqlite3.connect(d.path)
        self.assertEqual(self.m.setup.detect_owner_handles(con, '1'), [])
        con.close()

    def write_raw(self, **over):
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, dict(self.raw, **over))

    def test_merge_never_adds_a_contact_and_sets_owner_handle(self):
        self.write_raw(ownerHandle='', ownerHandles=[])
        res = self.m.setup.merge_owner_handles([OWNER, 'me@icloud.example', CONTACT])
        self.assertEqual(res['skippedContacts'], [CONTACT])
        self.assertEqual(sorted(res['ownerHandles']), sorted([OWNER, 'me@icloud.example']))
        self.assertEqual(res['ownerHandle'], OWNER)

    def test_watcher_auto_detects_once_only_when_no_owner_handle_is_set(self):
        self.write_raw(ownerHandle='', ownerHandles=[])
        w = self.m.watch.Watcher(poster=lambda *a, **k: True, runner=self.runner, now=self.clock.now, sleeper=lambda s: None)
        w.tick()
        cfg = self.rc.load_config()
        self.assertIn(OWNER, cfg['ownerHandles'])
        self.assertNotIn(CONTACT, cfg['ownerHandles'])
        self.assertIn('owner_handles_detected', read_log())
        # an explicit choice is never overridden: removing one sticks across restarts
        raw = self.rc.read_raw_config()
        raw['ownerHandles'] = [OWNER]
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, raw)
        w2 = self.m.watch.Watcher(poster=lambda *a, **k: True, runner=self.runner, now=self.clock.now, sleeper=lambda s: None)
        w2.tick()
        self.assertEqual(self.rc.load_config()['ownerHandles'], [OWNER])

    def test_relay_all_pending_turns_on_only_after_owner_handles_are_known(self):
        self.write_raw(ownerHandle='', ownerHandles=[], ownerXUserId='42', relayAll=False, relayAllPending=True)
        self.assertFalse(self.rc.load_config()['relayAll'])
        w = self.m.watch.Watcher(poster=lambda *a, **k: True, runner=self.runner, now=self.clock.now, sleeper=lambda s: None)
        w.tick()
        self.assertTrue(self.rc.load_config()['relayAll'])
        self.assertNotIn('relayAllPending', self.rc.read_raw_config())

    def test_detect_job_and_probe_job(self):
        self.write_raw(ownerHandle='', ownerHandles=[])
        r = self.watcher.run_job({'type': 'detect_owner_handles'}, self.cfg())
        self.assertEqual(r['detected'][0], OWNER)
        self.assertEqual(self.rc.load_config()['ownerHandles'], [], 'detect without --apply changes nothing')
        r = self.watcher.run_job({'type': 'detect_owner_handles', 'apply': True}, self.cfg())
        self.assertIn(OWNER, r['ownerHandles'])
        p = self.watcher.run_job({'type': 'test_probe'}, self.cfg())
        self.assertEqual((p['chatDb'], p['messagesRunning'], p['alive']), ('ok', True, True))

    def test_recent_senders_on_a_busy_db_returns_an_error_not_a_crash(self):
        orig = self.rc.open_chat_db

        def busy(path=None):
            raise self.rc.ChatDbBusy('database is locked')
        self.m.watch.rc.open_chat_db = busy
        try:
            r = self.watcher.recent_senders(self.cfg(), 30, 20)
        finally:
            self.m.watch.rc.open_chat_db = orig
        self.assertEqual(r, {'ok': False, 'error': 'chat.db is busy, try again in a moment'})


def setup_args(**kw):
    base = dict(owner_x_handle=None, sender_x_handle=None, owner_x_user_id=None, relay_conversation_id=None,
                relay_all=None, webhook_url=None, owner_handle=None, webhook_key_stdin=False,
                webhook_key_prompt=False, open_windows=False)
    base.update(kw)
    return argparse.Namespace(**base)


class RelayctlSetup(Base):
    def setUp(self):
        super().setUp()
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, {'dryRun': True, 'contacts': [], 'quietHours': None})
        self.ctl = self.m.ctl

    def test_setup_applies_everything_in_one_edit_and_keeps_safe_defaults(self):
        out = self.ctl.cmd_setup(setup_args(owner_x_handle='@OwnerAcct', sender_x_handle='SenderBot',
                                            owner_x_user_id='1234', relay_conversation_id='1234-5678',
                                            relay_all='off', webhook_url='https://example.invalid/h',
                                            webhook_key_stdin=True), stdin=io.StringIO('k3y-value\n'))
        cfg = self.rc.load_config()
        self.assertEqual((cfg['ownerXHandle'], cfg['senderXHandle'], cfg['ownerXUserId'], cfg['relayConversationId']),
                         ('OwnerAcct', 'SenderBot', '1234', '1234-5678'))
        self.assertEqual(cfg['webhookToken'], 'k3y-value')
        self.assertTrue(cfg['dryRun'])
        self.assertIsNone(cfg['quietHours'])
        self.assertEqual(cfg['contacts'], [])
        self.assertFalse(cfg['relayAll'])
        self.assertNotIn('k3y-value', json.dumps(out), 'the key is never echoed')
        self.assertEqual([i['id'] for i in out['checklist']], ['fda', 'extension', 'reload_chat', 'test'])

    def test_setup_validation_writes_nothing(self):
        before = self.rc.read_raw_config()
        for bad in (dict(owner_x_handle='same', sender_x_handle='SAME'), dict(owner_x_handle='not a handle!'),
                    dict(relay_conversation_id='bad id with spaces'), dict(webhook_url='http://plain.example'),
                    dict(owner_x_user_id='abc')):
            with self.assertRaises(self.ctl.CtlError):
                self.ctl.cmd_setup(setup_args(**bad))
        self.assertEqual(self.rc.read_raw_config(), before)
        with self.assertRaises(self.ctl.CtlError):
            self.ctl.cmd_setup(setup_args(webhook_key_stdin=True), stdin=io.StringIO(''))
        with self.assertRaises(self.ctl.CtlError):
            self.ctl.cmd_setup(setup_args(webhook_key_stdin=True), stdin=io.StringIO('k'))   # key without a URL

    def test_relay_all_on_waits_for_owner_handles(self):
        out = self.ctl.cmd_setup(setup_args(owner_x_user_id='1234', relay_all='on'))
        self.assertTrue(out['relayAllPending'])
        self.assertFalse(self.rc.load_config()['relayAll'])
        self.m.setup.merge_owner_handles([OWNER])
        self.assertTrue(self.rc.load_config()['relayAll'])

    def test_relay_all_on_is_immediate_when_identity_is_known_and_off_clears_pending(self):
        self.ctl.cmd_setup(setup_args(owner_x_user_id='1234', relay_all='on'))
        self.ctl.cmd_setup(setup_args(relay_all='off'))
        self.assertNotIn('relayAllPending', self.rc.read_raw_config())
        self.ctl.cmd_owner_handles('add', OWNER)
        self.ctl.cmd_setup(setup_args(relay_all='on'))
        self.assertTrue(self.rc.load_config()['relayAll'])
        self.ctl.cmd_setup(setup_args(owner_x_user_id='1234', relay_all='on'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.ctl.main(['relay-all', 'off'])
        self.assertNotIn('relayAllPending', self.rc.read_raw_config())

    def test_setup_detects_owner_handles_when_the_watcher_can_read(self):
        add_account_cols(self.db.path)
        set_account(self.db.path, self.db.add(CONTACT, text='a', from_me=1), 'P:' + OWNER, None)
        self.rc.log_event('chat_db_ok')
        self.ctl.run_job = lambda job, **kw: self.watcher.run_job(job, self.rc.load_config())
        out = self.ctl.cmd_setup(setup_args(owner_x_user_id='1234'))
        self.assertEqual(out['ownerHandlesDetected'], [OWNER])
        self.assertTrue(out['checklist'][0]['done'], 'FDA already works')

    def test_owner_handles_detect_cli(self):
        self.ctl.run_job = lambda job, **kw: dict(ok=True, detected=[OWNER], apply=job.get('apply'))
        self.assertFalse(self.ctl.cmd_owner_handles('detect')['applied'])
        self.assertTrue(self.ctl.cmd_owner_handles('detect', apply=True)['applied'])

    def test_checklist_and_status_flag_a_stale_extension(self):
        self.rc.write_json_atomic(os.path.join(self.rc.APP_DIR, 'ext_seen.json'), {'extVersion': '0.0.1', 'at': self.clock.t})
        items = {i['id']: i for i in self.ctl.setup_checklist(self.rc.load_config(), now=self.clock.now)}
        self.assertFalse(items['extension']['done'])
        self.assertIn('reload', items['extension']['text'])
        st = self.ctl.cmd_status(runner=FakeRunner())
        self.assertTrue(any('still v0.0.1' in p for p in st['problems']))
        self.rc.write_json_atomic(os.path.join(self.rc.APP_DIR, 'ext_seen.json'), {'extVersion': self.rc.EXTENSION_VERSION, 'at': self.clock.t})
        items = {i['id']: i for i in self.ctl.setup_checklist(self.rc.load_config(), now=self.clock.now)}
        self.assertTrue(items['extension']['done'])

    def test_test_result(self):
        self.assertFalse(self.ctl.cmd_test_result()['ok'])
        self.raw['webhookUrl'] = ''
        self.run_test_cmd()
        r = self.ctl.cmd_test_result()
        self.assertIn('webhook', r['failed'])
        self.assertEqual(r['test']['testId'], 'abcdef123456')

    def test_cli_parses_setup_and_owner_detect(self):
        self.ctl.run_job = lambda job, **kw: dict(ok=True, detected=[])
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc1 = self.ctl.main(['setup', '--owner-x-handle', 'OwnerAcct', '--relay-all', 'off'])
            rc2 = self.ctl.main(['owner-handles', 'detect', '--apply'])
            rc3 = self.ctl.main(['test-result'])
        finally:
            sys.stdout = old
        self.assertEqual((rc1, rc2, rc3), (0, 0, 1))


if __name__ == '__main__':
    unittest.main()
