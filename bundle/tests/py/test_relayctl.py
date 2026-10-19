"""relayctl: config editing, contacts, self-test (via the Chrome host and --direct), jobs, status."""
import contextlib
import io
import json
import os
import time
import unittest

from helpers import fresh, tmpdir, FakeRunner, ChatDb

OWNER = '+15555550199'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.ctl = self.m.ctl
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, {'ownerHandle': OWNER, 'customKey': 'kept'})

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.ctl.main(list(argv))
        return code, json.loads(buf.getvalue())

    def cfg(self):
        return self.m.rc.load_config()

    def raw(self):
        return self.m.rc.read_raw_config()


class Commands(Base):
    def test_pause_resume_dry_run_quiet_auto_reopen(self):
        self.assertEqual(self.run_cli('pause'), (0, {'ok': True, 'paused': True}))
        self.assertTrue(self.cfg()['paused'])
        self.run_cli('resume')
        self.assertFalse(self.cfg()['paused'])
        self.run_cli('set', 'ownerXUserId', '12345')   # owner identity required before going live
        self.run_cli('dry-run', 'off')
        self.assertFalse(self.cfg()['dryRun'])
        code, out = self.run_cli('dry-run', 'maybe')
        self.assertEqual(code, 1)
        self.run_cli('quiet-hours', '23:00', '06:30', '--tz', 'America/Chicago')
        self.assertEqual(self.cfg()['quietHours'], {'start': '23:00', 'end': '06:30', 'tz': 'America/Chicago'})
        self.run_cli('quiet-hours', 'off')
        self.assertIsNone(self.cfg()['quietHours'])
        self.assertEqual(self.run_cli('quiet-hours', '9pm')[0], 1)
        self.assertFalse(self.cfg()['autoReopenMessages'])
        self.run_cli('auto-reopen', 'yes')
        self.assertTrue(self.cfg()['autoReopenMessages'])
        self.assertEqual(self.m.rc.read_raw_config()['customKey'], 'kept', 'unknown keys survive edits')
        self.assertEqual(oct(os.stat(self.m.rc.CONFIG_PATH).st_mode & 0o777), '0o600')

    def test_set_validation(self):
        self.assertEqual(self.run_cli('set', 'webhookUrl', 'http://insecure')[0], 1)
        self.assertEqual(self.run_cli('set', 'webhookUrl', 'https://example.invalid/hook')[0], 0)
        self.assertEqual(self.run_cli('set', 'paused', 'true')[0], 1, 'not settable through set')
        self.assertEqual(self.run_cli('set', 'ownerHandle', 'nope')[0], 1)
        self.assertEqual(self.run_cli('set', 'webhookToken', 's3cret')[0], 1,
                         'the webhook key is not settable via argv; use set-webhook --token-stdin (item 8)')
        self.run_cli('set', 'maxSendsPerMinute', '4')
        self.assertEqual(self.cfg()['maxSendsPerMinute'], 4)

    def test_set_webhook_reads_token_from_stdin(self):
        out = self.ctl.cmd_set_webhook('https://example.invalid/hook', token_stdin=True, stdin=io.StringIO('  s3cret-key\n'))
        self.assertTrue(out['ok'])
        self.assertEqual(out['set'], ['webhookUrl', 'webhookToken'])
        self.assertNotIn('s3cret', json.dumps(out), 'the key is never echoed')
        self.assertEqual(self.cfg()['webhookUrl'], 'https://example.invalid/hook')
        self.assertEqual(self.cfg()['webhookToken'], 's3cret-key')
        self.assertEqual(self.m.rc.read_raw_config()['customKey'], 'kept', 'unknown keys survive')
        self.assertRaises(self.ctl.CtlError, self.ctl.cmd_set_webhook, 'http://insecure',
                          token_stdin=True, stdin=io.StringIO('x'))
        self.assertRaises(self.ctl.CtlError, self.ctl.cmd_set_webhook, 'https://ok.invalid/hook',
                          token_stdin=True, stdin=io.StringIO(''))
        self.assertRaises(self.ctl.CtlError, self.ctl.cmd_set_webhook, 'https://ok.invalid/hook')  # key must be on stdin
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO('piped-key')
        try:
            code, res = self.run_cli('set-webhook', '--url', 'https://h.invalid/x', '--token-stdin')
        finally:
            sys.stdin = old
        self.assertEqual(code, 0)
        self.assertEqual(self.cfg()['webhookToken'], 'piped-key')

    def test_contacts_add_label_remove(self):
        code, out = self.run_cli('add-contact', '--name', 'Test Contact', '--handle', '15555550123')
        self.assertEqual(out['contact'], {'name': 'Test Contact', 'handle': '+15555550123', 'label': 'Test'})
        self.run_cli('add-contact', '--name', 'Renamed', '--handle', '+1 555 555 0123')
        self.assertEqual([c['name'] for c in self.cfg()['contacts']], ['Renamed'], 're-adding updates, never duplicates')
        self.assertEqual(self.run_cli('add-contact', '--name', 'Me', '--handle', OWNER)[0], 1, 'owner cannot be a contact')
        self.assertEqual(self.run_cli('add-contact', '--name', 'Bad', '--handle', 'abc')[0], 1)
        # two contacts with one first name: automatic labels keep them apart; explicit labels win
        code, out = self.run_cli('add-contact', '--name', 'Alex Rivera', '--handle', '+15555550124')
        code, out = self.run_cli('add-contact', '--name', 'Alex Jordan', '--handle', '+15555550125')
        self.assertEqual(out['labels'], ['Renamed', 'Alex R.', 'Alex J.'])
        code, out = self.run_cli('label', 'alex j.', 'KJ')
        self.assertEqual((code, out['contact']['label']), (0, 'KJ'))
        self.assertEqual(self.run_cli('add-contact', '--name', 'Kim', '--handle', '+15555550126', '--label', 'kj')[0], 1, 'label taken')
        self.assertEqual(self.run_cli('add-contact', '--name', 'Kim', '--handle', '+15555550126', '--label', 'K: x')[0], 1, 'no colon')
        self.assertEqual(self.run_cli('label', 'Alex', 'Mom')[0], 1, 'ambiguous key')
        raw = self.raw()
        self.assertEqual([c.get('label') for c in raw['contacts']], [None, None, 'KJ'], 'only explicit labels are written')
        self.assertNotIn('xConversationId', json.dumps(raw))
        self.run_cli('add-contact', '--name', 'Mail', '--handle', 'Someone@Example.com')
        self.assertEqual(self.run_cli('remove-contact', 'renamed'), (0, {'ok': True, 'removed': 'Renamed', 'contacts': 3}))
        self.assertEqual(self.run_cli('remove-contact', 'KJ')[1]['contacts'], 2)
        self.assertEqual(self.run_cli('remove-contact', 'SOMEONE@example.com')[1]['contacts'], 1)
        self.assertEqual(self.run_cli('remove-contact', 'nobody')[0], 1)

    def test_relay_chat_and_routing_settings(self):
        self.assertIn('relayConversationId not set', ' | '.join(self.run_cli('status')[1]['problems']))
        self.assertEqual(self.run_cli('set', 'relayConversationId', 'has spaces')[0], 1)
        self.assertEqual(self.run_cli('set', 'relayConversationId', '1111-2222')[0], 0)
        self.run_cli('set', 'replyWindowMinutes', '30')
        self.run_cli('set', 'confirmSends', 'off')
        c = self.cfg()
        self.assertEqual((c['relayConversationId'], c['replyWindowMinutes'], c['confirmSends']), ('1111-2222', 30, False))
        self.assertNotIn('relayConversationId not set', ' | '.join(self.run_cli('status')[1]['problems']))

    def test_legacy_xconversationid_is_not_migrated_on_edit(self):
        # New-user template: a stray v1.0 per-contact id is ignored; relayConversationId stays unset.
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, {'contacts': [
            {'name': 'Old', 'handle': '+15555550124', 'xConversationId': '3333-4444'}]})
        self.run_cli('add-contact', '--name', 'New', '--handle', '+15555550125')
        raw = self.raw()
        self.assertEqual(raw.get('relayConversationId', ''), '')
        self.assertNotIn('xConversationId', json.dumps(raw))

    def test_status_lists_problems(self):
        code, out = self.run_cli('status')
        self.assertEqual(code, 0)
        joined = ' | '.join(out['problems'])
        for frag in ('no contacts', 'webhookUrl not set', 'native host manifest missing'):
            self.assertIn(frag, joined)
        self.assertEqual(out['config']['webhookToken'], '')


class RelayAllCommands(Base):
    def test_relay_all_toggle_and_set_keys(self):
        self.assertFalse(self.cfg()['relayAll'], 'template default: only people you pick')
        self.run_cli('set', 'ownerXUserId', '12345')   # ownerHandles[] + ownerXUserId required before relay-all
        self.assertEqual(self.run_cli('relay-all', 'on'), (0, {'ok': True, 'relayAll': True}))
        self.assertTrue(self.cfg()['relayAll'])
        self.run_cli('relay-all', 'off')
        self.assertFalse(self.cfg()['relayAll'])
        self.assertEqual(self.run_cli('relay-all', 'sometimes')[0], 1)
        self.assertEqual(self.run_cli('set', 'rosterDays', '14')[0], 0)
        self.assertEqual(self.cfg()['rosterDays'], 14)
        self.run_cli('set', 'skipShortCodes', 'off')
        self.assertFalse(self.cfg()['skipShortCodes'])

    def test_owner_handles_and_live_guard(self):
        # ownerHandle from setUp is already an owner alias; add a second, list, then clear back to it.
        self.assertIn(OWNER, self.run_cli('owner-handles', 'list')[1]['ownerHandles'])
        code, out = self.run_cli('owner-handles', 'add', 'owner@example.com')
        self.assertEqual(code, 0)
        self.assertIn('owner@example.com', out['ownerHandles'])
        # going live is blocked until ownerXUserId is set (the extension owner gate needs it).
        self.assertEqual(self.run_cli('relay-all', 'on')[0], 1)
        self.assertEqual(self.run_cli('dry-run', 'off')[0], 1)
        self.run_cli('set', 'ownerXUserId', 'u 12-345')
        self.assertEqual(self.cfg()['ownerXUserId'], '12345')        # digits only
        self.assertEqual(self.run_cli('relay-all', 'on')[0], 0)
        self.assertEqual(self.run_cli('dry-run', 'off')[0], 0)
        self.run_cli('owner-handles', 'clear')
        self.assertEqual(self.run_cli('owner-handles', 'list')[1]['ownerHandles'], [OWNER])

    def test_mute_and_unmute_by_label_number_or_short_code(self):
        rc = self.m.rc
        rc.save_roster([{'handle': '+15555550150', 'label': 'Pat', 'lastInboundAt': time.time()}])
        code, out = self.run_cli('mute', 'pat')
        self.assertEqual((code, out['muted']), (0, '+15555550150'))
        self.run_cli('mute', '(555) 555-0151')
        self.run_cli('mute', '12345')
        self.assertEqual(self.cfg()['mute'], ['+15555550150', '+15555550151', '12345'])
        self.run_cli('mute', 'Pat')                                   # idempotent
        self.assertEqual(len(self.raw()['mute']), 3)
        self.run_cli('unmute', '555-555-0150')
        self.assertEqual(self.cfg()['mute'], ['+15555550151', '12345'])
        self.assertEqual(self.run_cli('mute', 'Nobody Known')[0], 1)

    def test_roster_lists_labels_handles_and_reply_rights_only(self):
        rc = self.m.rc
        t = time.time()
        rc.write_json_atomic(rc.CONFIG_PATH, {'ownerHandle': OWNER, 'relayAll': True, 'mute': ['+15555550151'],
                                              'contacts': [{'name': 'Test Contact', 'handle': '+15555550123'}]})
        rc.save_roster([{'handle': '+15555550150', 'label': 'Pat', 'lastInboundAt': t - 60},
                        {'handle': '+15555550151', 'label': 'Old Friend', 'lastInboundAt': t - 40 * 86400},
                        {'handle': '+15555550123', 'label': '', 'lastInboundAt': t - 120},
                        {'handle': 'group:chat0001', 'label': 'Family', 'lastInboundAt': t - 30}])
        code, out = self.run_cli('roster')
        self.assertEqual(code, 0)
        got = {e['label']: (e['kind'], e['canReply'], e['muted']) for e in out['roster']}
        self.assertEqual(got, {'Family': ('group', False, False), 'Pat': ('roster', True, False),
                               'Test': ('contact', True, False), 'Old Friend': ('roster', False, True)})
        self.assertEqual([e['label'] for e in out['roster']][:2], ['Family', 'Pat'], 'newest first')
        code, out = self.run_cli('roster', '--days', '7')
        self.assertNotIn('Old Friend', [e['label'] for e in out['roster']])

    def test_status_no_contacts_problem_only_when_relay_all_off(self):
        probs = lambda: ' | '.join(self.run_cli('status')[1]['problems'])  # noqa: E731
        self.assertIn('no contacts yet', probs())
        self.run_cli('set', 'ownerXUserId', '12345')
        self.run_cli('relay-all', 'on')
        self.assertNotIn('no contacts yet', probs())
        self.assertTrue(self.run_cli('status')[1]['config']['relayAll'])


class SelfTestAndJobs(Base):
    def setUp(self):
        super().setUp()
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.runner = FakeRunner([(0, '')])
        self.watcher = self.m.watch.Watcher(poster=lambda *a: True, runner=FakeRunner(), sleeper=lambda s: None)

    def deliver(self):
        self.db.add(OWNER, text='Relay is set up', from_me=1, delivered=1)

    def test_selftest_through_chrome_host_then_watcher_confirms(self):
        host = self.m.host
        cfg = self.m.rc.load_config()
        real_check = self.m.rc.check_delivery_once
        calls = {'n': 0}

        def check(*a, **k):                     # this process has no Full Disk Access on the first look ...
            calls['n'] += 1
            return ('unavailable', None, None) if calls['n'] == 1 else real_check(*a, **k)
        self.m.rc.check_delivery_once = check

        def sleeper(_):                         # ... the extension + host and the watcher act meanwhile
            p = host.pending_selftest()
            if p:
                host.handle_request({'type': 'selftest', 'id': p['id']}, cfg=cfg, runner=self.runner, sleeper=lambda s: None)
                self.deliver()
            self.watcher.process_jobs(cfg)
        try:
            out = self.ctl.cmd_selftest(sleeper=sleeper)
        finally:
            self.m.rc.check_delivery_once = real_check
        self.assertEqual(out['via'], 'chrome-host')
        self.assertEqual((out['ok'], out['sent'], out['delivered'], out['confirmedBy']), (True, True, True, 'watcher'))
        self.assertEqual(self.runner.calls[0][2:], ['Relay is set up \u2705', OWNER, 'auto'])
        self.assertFalse(os.path.exists(os.path.join(self.ctl.SELFTEST_DIR, 'request.json')), 'request cleaned up')

    def test_selftest_direct(self):
        self.deliver()
        out = self.ctl.cmd_selftest(direct=True, runner=self.runner, sleeper=lambda s: None)
        self.assertEqual((out['ok'], out['via'], out['delivered']), (True, 'direct', True))

    def test_selftest_extension_not_loaded_times_out_with_hint(self):
        clock = [time.time()]
        out = self.ctl.cmd_selftest(wait_s=3, sleeper=lambda s: clock.__setitem__(0, clock[0] + s), now=lambda: clock[0])
        self.assertFalse(out['ok'])
        self.assertIn('--direct', out['error'])

    def test_selftest_1712_hint_and_missing_owner(self):
        r = FakeRunner([(1, '(-1712)'), (1, '(-1712)')])
        out = self.ctl.cmd_selftest(direct=True, runner=r, sleeper=lambda s: None)
        self.assertIn('Automation prompt', out['hint'])
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, {})
        with self.assertRaises(self.ctl.CtlError):
            self.ctl.cmd_selftest(direct=True)

    def test_recent_senders_job_roundtrip(self):
        self.db.add('+15555550188', text='hi')
        cfg = self.m.rc.load_config()
        out = self.ctl.cmd_recent_senders(sleeper=lambda s: self.watcher.process_jobs(cfg))
        self.assertTrue(out['ok'])
        self.assertEqual([s['handle'] for s in out['senders']], ['+15555550188'])
        self.assertEqual(os.listdir(self.ctl.JOBS_DIR), [], 'job and result files removed')

    def test_job_without_watcher_times_out(self):
        clock = [time.time()]
        out = self.ctl.run_job({'type': 'recent_senders'}, wait_s=2, sleeper=lambda s: clock.__setitem__(0, clock[0] + s),
                               now=lambda: clock[0])
        self.assertIn('watcher did not answer', out['error'])


if __name__ == '__main__':
    unittest.main()
