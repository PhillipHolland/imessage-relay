"""v1.4.30: webhook outage in status.problems, roster labels that follow AddressBook, /who matching
routing, /add by name, a lower typo bar, and familiar roster people who are not asked every time."""
import time
import unittest

from helpers import fresh, tmpdir, FakeClock, FakeRunner

CONV = '1111-2222'
BRIAN = '+15555550188'
KENT = '+15555550177'


class Common(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.rc = self.m.rc
        self.clock = FakeClock()

    def cfg(self, **over):
        raw = {'relayConversationId': CONV, 'relayAll': True, 'dryRun': False,
               'webhookUrl': 'https://example.invalid/hook',
               'contacts': [{'name': 'Karen', 'handle': '+15555550123'}],
               'ownerHandle': '+15555550199', 'confirmGuessed': 'risky'}
        raw.update(over)
        return self.rc.sanitize_config(raw)


class Names(Common):
    def test_typo_bar_offers_karin_and_brain(self):
        self.assertGreaterEqual(self.rc.close_name_score('karin', 'karen'), self.rc.FUZZY_MIN)
        self.assertGreaterEqual(self.rc.close_name_score('brain', 'brian'), self.rc.FUZZY_MIN)
        self.assertLess(self.rc.close_name_score('sam', 'pat'), self.rc.FUZZY_MIN)

    def test_roster_label_upgrades_when_address_book_gains_a_name(self):
        cfg = self.cfg(contacts=[])
        roster = [{'handle': BRIAN, 'label': '+1 (555) 555-0188', 'lastInboundAt': self.clock.t}]
        label, changed = self.rc.roster_label(cfg, roster, BRIAN, 'Brian')
        self.assertTrue(changed)
        self.assertEqual(label, 'Brian')
        self.assertEqual(roster[0]['contactName'], 'Brian')
        label2, changed2 = self.rc.roster_label(cfg, roster, BRIAN, 'Brian Holt')
        self.assertTrue(changed2)
        self.assertEqual(roster[0]['contactName'], 'Brian Holt')
        self.assertNotIn('555', label2)
        label3, changed3 = self.rc.roster_label(cfg, roster, BRIAN, 'Sam')
        self.assertTrue(changed3)
        self.assertEqual(label3, 'Sam')

    def test_nickname_is_the_saved_label(self):
        people = [{'name': 'Brian Holt', 'first': 'Brian', 'nick': 'Brain',
                   'handles': [('phone', 'mobile', '5555550188')]}]
        hit = self.m.contacts.lookup('Brain', self.cfg(), people=people)['matches']
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]['saveAs'], 'Brain')
        self.assertEqual(hit[0]['name'], 'Brian Holt')
        full = self.m.contacts.lookup('Brian Holt', self.cfg(), people=people)['matches']
        self.assertNotIn('saveAs', full[0])


class Directory(Common):
    def test_who_and_routing_share_roster_people(self):
        cfg = self.cfg()
        self.rc.record_inbound(BRIAN, self.clock.t - 30, label='Brian')
        d = self.rc.build_directory(cfg, self.rc.load_roster())
        kind, val = self.rc.classify_bare_reply(cfg, d, self.clock.t)
        self.assertEqual(kind, 'send')
        self.assertEqual(val['label'], 'Brian')
        summary = self.m.host.who_summary(cfg, self.clock.t)
        self.assertIn('Replies go to Brian', summary)
        self.assertEqual(self.m.host.bare_reply_name(cfg, self.clock.t), 'Brian')

    def test_frequent_roster_person_is_hinted_not_added(self):
        cfg = self.cfg()
        for i in range(20):
            self.rc.record_inbound(BRIAN, self.clock.t - 30, label='Brian')
        summary = self.m.host.who_summary(cfg, self.clock.t)
        self.assertIn('/add Brian', summary)
        self.assertEqual(cfg['contacts'][0]['name'], 'Karen')

    def test_add_name_uses_the_named_roster_person(self):
        self.rc.record_inbound(KENT, self.clock.t - 10, label='Kent')
        self.rc.record_inbound(BRIAN, self.clock.t - 20, label='Brian')
        posts = []

        def poster(url, payload, token='', timeout=15):
            posts.append(payload)
            return True

        r = self.m.host.handle_request(
            {'type': 'send', 'text': '/add Brian', 'conversationId': CONV},
            cfg=self.cfg(), runner=FakeRunner([]), sleeper=lambda s: None, now=self.clock.now, poster=poster)
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertIn('0188', posts[-1]['detail'])
        self.assertNotIn('0177', posts[-1]['detail'])


class Familiar(Common):
    def send(self, text, **kw):
        msg = {'type': 'send', 'text': text, 'conversationId': CONV}
        msg.update(kw)
        self.runner = FakeRunner([(0, '')] * 4)
        return self.m.host.handle_request(msg, cfg=self.cfg(), runner=self.runner, sleeper=lambda s: None,
                                          now=self.clock.now, poster=lambda *a, **k: True)

    def test_new_roster_person_is_asked_and_a_familiar_one_is_not(self):
        self.rc.record_inbound(BRIAN, self.clock.t - 30, label='Brian')
        r = self.send('on my way')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r.get('handle'), BRIAN)
        r = self.send('on my way', confirmed=True, handle=BRIAN)
        self.assertTrue(r.get('ok'), r)
        self.assertFalse(r.get('needsConfirm'))
        self.assertTrue(self.rc.load_roster()[0].get('trusted'))
        self.clock.t += 700   # past the post-prompt hold, still inside the reply window
        self.rc.record_inbound(BRIAN, self.clock.t - 30, label='Brian')
        r = self.send('see you there')
        self.assertTrue(r.get('ok') and r.get('sent'), r)
        self.assertFalse(r.get('needsConfirm'))

    def test_karin_offers_karen(self):
        posts = []

        def poster(url, payload, token='', timeout=15):
            posts.append(payload)
            return True

        r = self.m.host.handle_request(
            {'type': 'send', 'text': 'karin: hi', 'conversationId': CONV},
            cfg=self.cfg(), runner=FakeRunner([]), sleeper=lambda s: None, now=self.clock.now, poster=poster)
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertIn('Did you mean', posts[-1]['detail'])


class WebhookAlarm(Common):
    def test_stuck_retries_become_a_problem_until_healthy(self):
        cfg = self.cfg(webhookUrl='https://example.invalid/hook')
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, {
            'relayConversationId': CONV, 'webhookUrl': 'https://example.invalid/hook',
            'contacts': [{'name': 'Karen', 'handle': '+15555550123'}], 'ownerHandle': '+15555550199'})
        w = self.m.watch.Watcher(poster=lambda *a, **k: False, now=lambda: time.time(), sleeper=lambda s: None)
        w.retry = [{'rowid': 1}, {'rowid': 2}]
        w.note_webhook(self.rc.WebhookResult(True, relayed=False, status=200))
        w.webhook_fail_since = time.time() - 11 * 60
        w.settle_webhook()
        w.write_status(cfg)
        st = self.m.ctl.cmd_status(runner=FakeRunner([(0, '')]))
        hit = [p for p in st['problems'] if p.startswith('webhook has been failing')]
        self.assertEqual(len(hit), 1, st['problems'])
        self.assertIn('2 waiting', hit[0])
        self.assertIn('accepted the POST', hit[0])
        self.assertNotIn('hi', hit[0])
        w.retry = []
        w._webhook_failed_tick = False
        w.webhook_fail_streak = 0
        w.settle_webhook()
        w.write_status(cfg)
        st = self.m.ctl.cmd_status(runner=FakeRunner([(0, '')]))
        self.assertFalse(any(p.startswith('webhook has been failing') for p in st['problems']))

    def test_explicit_relayed_ack_is_distinct_from_http_accepted(self):
        self.assertEqual(self.rc.webhook_kind(self.rc.WebhookResult(True, relayed=False, status=200)), 'accepted')
        self.assertEqual(self.rc.webhook_kind(self.rc.WebhookResult(True, relayed=True, status=200)), 'relayed')
        self.assertEqual(self.rc.webhook_kind(self.rc.WebhookResult(False)), 'failed')
        self.assertTrue(self.rc.WebhookResult(True))
        self.assertFalse(self.rc.WebhookResult(False))


if __name__ == '__main__':
    unittest.main()
