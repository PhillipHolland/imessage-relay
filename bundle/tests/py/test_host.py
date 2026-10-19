"""Native host: framing, get_config, send gating (paused / relay chat / routing / normalization /
rate limit), confirmations and "who is this for?" webhooks, dry run, -1712 retry, self-test, and
an end-to-end run of the real script."""
import io
import json
import os
import struct
import subprocess
import sys
import time
import unittest

from helpers import fresh, tmpdir, read_log, FakeClock, FakeRunner, ChatDb, MAC

CONV = '1111-2222'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.h = self.m.host
        self.clock = FakeClock()
        self.raw = {'dryRun': True, 'relayConversationId': CONV, 'contacts': [{'name': 'Test Contact', 'handle': '15555550123'}],
                    'ownerHandle': '+15555550199', 'confirmSends': False}
        self.posts = []
        # the watcher just relayed a message from the test contact (handle + time only)
        self.m.rc.record_inbound('+15555550123', self.clock.t - 60)

    def cfg(self, **over):
        return self.m.rc.sanitize_config(dict(self.raw, **over))

    def poster(self, url, payload, token='', timeout=15):
        self.posts.append(payload)
        return True

    def req(self, text='hello there', conv=CONV, handle=None, runner=None, quoted=None, **over):
        msg = {'type': 'send', 'text': text, 'conversationId': conv, 'ts': '2026-10-18T21:32:00Z'}
        if handle is not None:
            msg['handle'] = handle
        if quoted is not None:
            msg['quotedText'] = quoted
        return self.h.handle_request(msg, cfg=self.cfg(**over), runner=runner or FakeRunner(), sleeper=self.clock.sleep,
                                     now=self.clock.now, poster=self.poster)


class Framing(Base):
    def test_roundtrip_and_errors(self):
        obj = {'text': 'héllo 👋'}
        b = self.h.encode_message(obj)
        self.assertEqual(struct.unpack('<I', b[:4])[0], len(b) - 4)
        self.assertEqual(self.h.read_message(io.BytesIO(b)), obj)
        self.assertIsNone(self.h.read_message(io.BytesIO(b'')))
        for bad in (b'\x05\x00', struct.pack('<I', 10) + b'{}', struct.pack('<I', 5 * 1024 * 1024)):
            with self.assertRaises(ValueError):
                self.h.read_message(io.BytesIO(bad))


class GetConfig(Base):
    def test_get_config_is_the_source_of_truth(self):
        r = self.h.handle_request({'type': 'get_config'}, cfg=self.cfg(ownerXHandle='OwnerAcct', senderXHandle='SenderBot',
                                                                        ownerXUserId='99887766', webhookToken='tok'), now=self.clock.now)
        self.assertEqual(r['ok'], True)
        self.assertEqual(r['version'], '1.4.3')
        self.assertEqual((r['paused'], r['dryRun']), (False, True))
        self.assertEqual(r['contacts'], [{'name': 'Test Contact', 'label': 'Test', 'handle': '+15555550123'}])
        self.assertEqual(r['relayConversationId'], CONV)
        self.assertEqual((r['ownerXHandle'], r['senderXHandle']), ('OwnerAcct', 'SenderBot'))
        self.assertEqual(r['ownerXUserId'], '99887766')   # extension owner-identity gate reads this
        self.assertIsNone(r['pendingSelfTest'])
        self.assertNotIn('tok', json.dumps(r), 'webhook token never goes to the extension')
        self.assertNotIn('webhookUrl', r)

    def test_legacy_xconversationid_is_ignored_not_migrated(self):
        # v1.3.2 is a new-user template: a stray v1.0 per-contact id is dropped, not promoted.
        raw = {'contacts': [{'name': 'Old', 'handle': '+15555550124', 'xConversationId': '3333-4444'}]}
        r = self.h.handle_request({'type': 'get_config'}, cfg=self.m.rc.sanitize_config(raw), now=self.clock.now)
        self.assertEqual(r['relayConversationId'], '')
        self.assertNotIn('xConversationId', json.dumps(r))

    def test_pending_selftest_visible_until_result(self):
        d = os.path.join(self.m.rc.APP_DIR, 'selftest')
        self.m.rc.write_json_atomic(os.path.join(d, 'request.json'), {'id': 'abc', 'createdAt': self.clock.t})
        get = lambda: self.h.handle_request({'type': 'get_config'}, cfg=self.cfg(), now=self.clock.now)['pendingSelfTest']  # noqa: E731
        self.assertEqual(get(), {'id': 'abc'})
        self.clock.t += 16 * 60
        self.assertIsNone(get(), 'stale request ignored')

    def test_ping_and_unknown(self):
        self.assertEqual(self.h.handle_request({'type': 'ping'}, cfg=self.cfg())['ok'], True)
        self.assertEqual(self.h.handle_request({'type': 'nope'}, cfg=self.cfg())['ok'], False)
        self.assertEqual(self.h.handle_request('x', cfg=self.cfg())['ok'], False)


class Send(Base):
    def test_dry_run_logs_length_only(self):
        r = FakeRunner()
        resp = self.req(text='do not log me', runner=r)
        self.assertEqual(resp, {'ok': True, 'dryRun': True, 'contact': 'Test', 'routedBy': 'recent', 'textLength': 13})
        self.assertEqual(r.calls, [], 'dry run never calls osascript')
        self.assertNotIn('+15555550123', read_log(), 'handles are scrubbed from the log (item 7)')
        self.assertIn('"textLength": 13', read_log())
        self.assertNotIn('do not log me', read_log())

    def test_recipient_comes_from_config_and_normalized_handles_match(self):
        r = FakeRunner([(0, '')])
        resp = self.req(handle='1 (555) 555-0123', runner=r, dryRun=False)
        self.assertTrue(resp['sent'])
        self.assertEqual(r.calls[0][3], '+15555550123')

    def test_refusals(self):
        r = FakeRunner([(0, '')] * 5)
        self.assertIn('not the relay conversation', self.req(conv='9999-1111', runner=r, dryRun=False)['error'])
        self.assertIn('relayConversationId not set', self.req(runner=r, dryRun=False, relayConversationId='')['error'])
        self.assertIn('does not match', self.req(handle='+15555550150', runner=r, dryRun=False)['error'])
        self.assertEqual(self.req(runner=r, dryRun=False, paused=True)['error'], 'paused')
        self.assertEqual(self.req(text='  ', runner=r, dryRun=False)['error'], 'empty text')
        self.assertEqual(self.req(text='x' * 4001, runner=r, dryRun=False)['error'], 'text too long')
        self.assertEqual(r.calls, [])
        log = read_log()
        self.assertIn('not the relay conversation', log)
        self.assertIn('"paused"', log)

    def test_never_relays_to_owner_handle(self):
        self.raw['contacts'] = [{'name': 'Me', 'handle': '+15555550199'}]
        self.assertIn('owner', self.req(text='Me: hi', dryRun=False)['error'])

    def test_quiet_hours_do_not_block_owner_replies(self):
        r = FakeRunner([(0, '')])
        resp = self.req(runner=r, dryRun=False, quietHours={'start': '00:00', 'end': '23:59', 'tz': 'UTC'})
        self.assertTrue(resp['sent'])

    def test_dedupe_and_rate_limit_window(self):
        self.assertTrue(self.req(text='same')['dryRun'])
        self.clock.t += 10
        self.assertEqual(self.req(text='same')['skipped'], 'duplicate within 60s')
        for i in range(5):
            self.clock.t += 1
            self.assertTrue(self.req(text='m%d' % i)['ok'])
        self.clock.t += 1
        resp = self.req(text='seventh')
        self.assertFalse(resp['ok'])
        self.assertIn('rate limited', resp['error'])
        self.clock.t += 60
        self.assertTrue(self.req(text='seventh')['ok'], 'window slides')
        self.assertIn('rate_limited', read_log())

    def test_real_send_argv_footer_delivery(self):
        r = FakeRunner([(0, '')])
        evil = 'He said "hi" & then\n" end tell\ndo shell script "rm -rf ~"'
        resp = self.req(text=evil, runner=r, dryRun=False, footer='- sent via relay')
        # Delivery is now proven by the watcher (it has Full Disk Access); the host returns pending.
        self.assertEqual(resp, {'ok': True, 'dryRun': False, 'sent': True, 'delivered': None, 'deliveryCheck': 'pending',
                                'contact': 'Test', 'routedBy': 'recent'})
        cmd = r.calls[0]
        self.assertEqual(cmd[0], '/usr/bin/osascript')
        self.assertTrue(cmd[1].endswith('send_imessage.applescript'))
        self.assertEqual(cmd[2], evil + '\n- sent via relay')
        self.assertEqual(cmd[4], 'auto')
        self.assertEqual(len(cmd), 5)
        self.assertNotIn('rm -rf', read_log())
        # the host left a body-free pending record for the watcher: handle, length, label -- no body, no hash
        pend = self.m.rc.load_pending()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]['handle'], '+15555550123')
        self.assertEqual(pend[0]['textLength'], len(evil + '\n- sent via relay'))
        self.assertNotIn('hash', pend[0], 'the body hash was removed from pending records (item 5)')
        self.assertEqual(pend[0]['label'], 'Test')
        import glob
        for pf in glob.glob(os.path.join(self.m.rc.PENDING_DIR, '*.json')):
            with open(pf, encoding='utf-8') as fh:
                self.assertNotIn(evil, fh.read())

    def test_retry_once_on_1712_then_give_up(self):
        r = FakeRunner([(1, 'execution error: Messages got an error: AppleEvent timed out. (-1712)'), (0, '')])
        self.assertTrue(self.req(runner=r, dryRun=False)['sent'])
        self.assertEqual([c[0] for c in r.calls], ['/usr/bin/osascript', '/usr/bin/open', '/usr/bin/osascript'])
        self.assertIn(2, self.clock.sleeps)
        r2 = FakeRunner([(1, '(-1712)'), (1, '(-1712)')])
        self.clock.t += 5
        self.assertFalse(self.req(text='x1', runner=r2, dryRun=False)['ok'])
        self.assertEqual(r2.count('/usr/bin/osascript'), 2)
        r3 = FakeRunner([(1, 'Not authorized to send Apple events to Messages. (-1743)')])
        self.clock.t += 5
        resp = self.req(text='x2', runner=r3, dryRun=False)
        self.assertIn('Automation permission for Messages is off', resp['error'])
        self.assertEqual(r3.count('/usr/bin/osascript'), 1, 'no retry for other errors')
        r4 = FakeRunner([(0, '')])
        self.clock.t += 5
        self.assertTrue(self.req(text='x2', runner=r4, dryRun=False)['sent'], 'failed text can be retried')

    def test_typeless_request_is_rejected(self):
        # v0.3 type-less "send" was removed (item 16): a request must name its type explicitly.
        resp = self.h.handle_request({'text': 'hi', 'conversationId': CONV}, cfg=self.cfg(), runner=FakeRunner(),
                                     sleeper=self.clock.sleep, now=self.clock.now)
        self.assertEqual(resp, {'ok': False, 'error': 'unknown request type'})


class RoutingAndWebhooks(Base):
    def setUp(self):
        super().setUp()
        self.raw.update(contacts=[{'name': 'Alex Rivera', 'handle': '+15555550123'},
                                  {'name': 'Alex Jordan', 'handle': '+15555550124', 'label': 'KJ'},
                                  {'name': 'Mike', 'handle': 'mike@example.com'}],
                        webhookUrl='https://hook.invalid/x', webhookToken='tok', confirmSends=True)

    def test_prefix_is_stripped_and_routes(self):
        r = FakeRunner([(0, '')])
        resp = self.req(text='kj:  on my way', runner=r, dryRun=False)
        self.assertEqual((resp['contact'], resp['routedBy']), ('KJ', 'prefix'))
        self.assertEqual(r.calls[0][2:], ['on my way', '+15555550124', 'auto'])
        # item 6: with deliveryCheck on, the host no longer posts the confirmation; the watcher posts the
        # single '✓✓ delivered' / '✓ sent' line after checking chat.db. The host just records a pending send.
        self.assertFalse([p for p in self.posts if p.get('type') == 'sent'], 'host does not post sent for a live send')
        self.assertNotIn('on my way', json.dumps(self.posts), 'nothing the host posts carries the body')

    def test_bare_reply_goes_to_most_recent_contact(self):
        self.m.rc.record_inbound('mike@example.com', self.clock.t)   # 60 s after Alex R.
        # confirmGuessed defaults to 'risky'; with a second contact texting recently this reply would
        # need confirmation (item 3). Turn it off here to assert the pure recent-routing behavior.
        resp = self.req(text='ok see you', confirmGuessed='off')
        self.assertEqual((resp['ok'], resp['contact'], resp['routedBy']), (True, 'Mike', 'recent'))
        sent = dict(self.posts[-1])
        self.assertTrue(sent.pop('id', ''), 'every webhook payload carries a unique id (item B)')
        self.assertEqual(sent, {'type': 'sent', 'label': 'Mike', 'contactName': 'Mike', 'dryRun': True,
                                'delivered': None, 'routedBy': 'recent', 'at': self.m.rc.iso_utc(self.clock.t)})

    def test_needs_target_sends_nothing_and_asks(self):
        r = FakeRunner()
        self.clock.t += 3 * 3600                     # nobody wrote within replyWindowMinutes (120)
        resp = self.req(text='sounds good', runner=r, dryRun=False)
        self.assertEqual((resp['ok'], resp['needsTarget']), (False, True))
        self.assertTrue(resp['error'].startswith("Who's this for? Start with a name, like "))
        self.assertEqual(r.calls, [])
        self.assertEqual(self.posts[-1]['type'], 'needs_target')
        self.assertNotIn('sounds good', json.dumps(self.posts))
        self.assertIn('"needs_target"', read_log())
        self.assertNotIn('sounds good', read_log())

    def test_shared_first_name_prefix_is_ambiguous(self):
        resp = self.req(text='Alex: hi')
        self.assertTrue(resp['needsTarget'])
        self.assertEqual(self.req(text='alex r.: hi')['contact'], 'Alex R.')

    def test_quote_beats_prefix_conflict_and_recent(self):
        self.m.rc.record_inbound('mike@example.com', self.clock.t - 5)
        resp = self.req(text='yes', quoted='Alex R.: are you coming?')
        self.assertEqual((resp['contact'], resp['routedBy']), ('Alex R.', 'quote'))
        self.assertTrue(self.req(text='Mike: yes', quoted='KJ: dinner?')['needsTarget'], 'quote vs prefix conflict')

    def test_confirm_off_and_no_webhook(self):
        self.req(text='Mike: a', confirmSends=False)
        self.assertEqual(self.posts, [])
        self.clock.t += 1
        self.assertTrue(self.req(text='Mike: b', webhookUrl='')['ok'])
        self.assertEqual(self.posts, [])

    def test_refusals_do_not_confirm(self):
        self.req(text='Mike: a', paused=True)
        self.req(text='Mike: ', dryRun=False)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.req(text='Mike:   ')['error'], 'empty text after the name prefix')


class RelayAllSends(Base):
    def setUp(self):
        super().setUp()
        self.raw['relayAll'] = True
        rc = self.m.rc
        rc.save_roster(rc.load_roster() + [
            {'handle': '+15555550150', 'label': 'Pat', 'lastInboundAt': self.clock.t - 3600},
            {'handle': '+15555550151', 'label': 'Old Friend', 'lastInboundAt': self.clock.t - 40 * 86400},
            {'handle': 'group:chat0001', 'label': 'Family', 'lastInboundAt': self.clock.t - 600}])

    def test_get_config_reports_relay_all(self):
        r = self.h.handle_request({'type': 'get_config'}, cfg=self.cfg())
        self.assertTrue(r['relayAll'])

    def test_roster_person_within_roster_days(self):
        r = FakeRunner([(0, '')])
        resp = self.req(text='Pat: see you at 6', runner=r, dryRun=False)
        self.assertTrue(resp['sent'], resp)
        self.assertEqual((r.calls[0][3], resp['contact'], resp['routedBy']), ('+15555550150', 'Pat', 'prefix'))

    def test_old_roster_never_texted_and_group_are_refused(self):
        r = FakeRunner([(0, '')] * 3)
        for text, why in (('Old Friend: hi', 'rosterDays'), ('(555) 555-0170: hi', 'never texted'),
                          ('Family: hi all', "group replies aren't supported yet")):
            resp = self.req(text=text, runner=r, dryRun=False)
            self.assertTrue(resp.get('needsTarget'), resp)
            self.assertIn(why, resp['reason'])
        self.assertEqual(r.calls, [], 'nothing sent')

    def test_roster_send_refused_when_relay_all_is_off(self):
        r = FakeRunner([(0, '')])
        resp = self.req(text='Pat: hi', runner=r, dryRun=False, relayAll=False)
        self.assertTrue(resp.get('needsTarget'))
        self.assertEqual(r.calls, [])

    def test_relay_all_works_without_contacts(self):
        self.raw['contacts'] = []
        r = FakeRunner([(0, '')])
        resp = self.req(text='Pat: hi', runner=r, dryRun=False)
        self.assertTrue(resp['sent'], resp)
        self.assertIn('no contacts', self.req(text='Pat: hi', runner=r, dryRun=False, relayAll=False)['error'])


class SelfTest(Base):
    def test_selftest_via_host_sends_to_owner_even_in_dry_run(self):
        d = os.path.join(self.m.rc.APP_DIR, 'selftest')
        self.m.rc.write_json_atomic(os.path.join(d, 'request.json'), {'id': 'st1', 'createdAt': self.clock.t})
        r = FakeRunner([(0, '')])
        resp = self.h.handle_request({'type': 'selftest', 'id': 'st1'}, cfg=self.cfg(), runner=r, sleeper=self.clock.sleep, now=self.clock.now)
        self.assertTrue(resp['ok'])
        self.assertEqual(r.calls[0][2:], ['Relay is set up \u2705', '+15555550199', 'auto'])
        res = self.m.rc.read_json(os.path.join(d, 'result.json'))
        self.assertEqual((res['id'], res['ok'], res['via']), ('st1', True, 'chrome-host'))
        again = self.h.handle_request({'type': 'selftest', 'id': 'st1'}, cfg=self.cfg(), runner=r, now=self.clock.now)
        self.assertFalse(again['ok'], 'one send per request')

    def test_selftest_without_request_refused(self):
        r = FakeRunner()
        resp = self.h.handle_request({'type': 'selftest', 'id': 'zzz'}, cfg=self.cfg(), runner=r, now=self.clock.now)
        self.assertFalse(resp['ok'])
        self.assertEqual(r.calls, [])


class Delivery(Base):
    def test_delivery_check_against_temp_chat_db(self):
        db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        db.add('+15555550123', text='x', from_me=1, delivered=1)
        rc = self.m.rc
        status, err, rowid = rc.check_delivery_once('15555550123', time.time() - 30)
        self.assertEqual((status, err), ('delivered', None))
        self.assertIsNotNone(rowid)
        self.assertEqual(rc.check_delivery_once('+15555550150', time.time() - 30)[0], 'pending')
        os.unlink(db.path)
        self.assertEqual(rc.check_delivery_once('+15555550123', time.time() - 30)[0], 'unavailable')


class EndToEnd(Base):
    def test_real_script_like_chrome(self):
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        script = os.path.join(MAC, 'imessage_relay_host.py')
        frames = (self.h.encode_message({'type': 'get_config'}) +
                  self.h.encode_message({'type': 'send', 'text': 'test: secret body words', 'conversationId': CONV, 'ts': 'x'}))
        p = subprocess.run([sys.executable, script], input=frames, capture_output=True, env=os.environ.copy(), timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = io.BytesIO(p.stdout)
        cfg = self.h.read_message(out)
        sent = self.h.read_message(out)
        self.assertEqual(cfg['contacts'][0]['handle'], '+15555550123')
        self.assertEqual((sent['ok'], sent['dryRun'], sent['routedBy']), (True, True, 'prefix'))
        self.assertIn('"dry_run"', read_log())
        self.assertNotIn('secret body words', read_log())


if __name__ == '__main__':
    unittest.main()
