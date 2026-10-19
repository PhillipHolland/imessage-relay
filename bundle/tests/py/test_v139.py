"""v1.3.9: X Chat time/status chips ("9:07 AM", shown twice) appended to a reply by an older extension
are stripped by the native host before Messages.app ever sees the text. Only trailing chip lines go;
clock times inside the message stay."""
import unittest

from helpers import fresh, tmpdir, read_log, FakeClock, FakeRunner

CONV = '1111-2222'


class StripUnit(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc

    def s(self, t):
        return self.rc.strip_trailing_chips(t)

    def test_exact_examples(self):
        self.assertEqual(self.s('Good morning - love you so!!\u2764\ufe0f\n9:07 AM\n\n9:07 AM'),
                         'Good morning - love you so!!\u2764\ufe0f')
        self.assertEqual(self.s('test\n10:54 PM\n\n10:54 PM'), 'test')

    def test_variants(self):
        self.assertEqual(self.s('hi\n21:07'), 'hi')
        self.assertEqual(self.s('hi\n21:07:33\n\n\n21:07:33\n21:07:33'), 'hi')
        self.assertEqual(self.s('hi\n9:07 a.m.\n9:07pm'), 'hi')
        self.assertEqual(self.s('hi\n9:07\u202fAM\n\n9:07\u00a0AM'), 'hi')
        self.assertEqual(self.s('hi\nEdited\n\nSent\nSeen\nDelivered'), 'hi')
        self.assertEqual(self.s('hi\nEdited \u00b7 9:07 AM\n\n9:07 AM\n'), 'hi')

    def test_times_inside_message_kept(self):
        self.assertEqual(self.s('meet at 9:07 AM ok?\n9:07 AM'), 'meet at 9:07 AM ok?')
        self.assertEqual(self.s('line one\n9:07 AM\nline three\n9:08 AM\n\n9:08 AM'), 'line one\n9:07 AM\nline three')
        self.assertEqual(self.s('see you at 10:54 PM'), 'see you at 10:54 PM')
        self.assertEqual(self.s('hello\n\nworld'), 'hello\n\nworld')
        self.assertEqual(self.s('I sent it\nseen it?'), 'I sent it\nseen it?')

    def test_time_only_message_keeps_one(self):
        self.assertEqual(self.s('9:07 AM\n9:07 AM\n\n9:07 AM'), '9:07 AM')
        self.assertEqual(self.s('9:07 AM'), '9:07 AM')
        self.assertEqual(self.s('Sent'), 'Sent')
        self.assertEqual(self.s(''), '')
        self.assertIsNone(self.s(None))


class HostSendPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.clock = FakeClock()
        self.raw = {'dryRun': True, 'relayConversationId': CONV,
                    'contacts': [{'name': 'Test Contact', 'handle': '15555550123'}],
                    'ownerHandle': '+15555550199', 'confirmSends': False, 'confirmGuessed': 'off'}
        self.m.rc.record_inbound('+15555550123', self.clock.t - 60)

    def send(self, text, runner, **over):
        cfg = self.m.rc.sanitize_config(dict(self.raw, **over))
        return self.m.host.handle_request({'type': 'send', 'text': text, 'conversationId': CONV}, cfg=cfg, runner=runner,
                                          sleeper=self.clock.sleep, now=self.clock.now, poster=lambda *a, **k: True)

    def test_old_extension_chips_never_reach_messages(self):
        for i, (raw, want) in enumerate([('Good morning - love you so!!\u2764\ufe0f\n9:07 AM\n\n9:07 AM',
                                          'Good morning - love you so!!\u2764\ufe0f'),
                                         ('test\n10:54 PM\n\n10:54 PM', 'test'),
                                         ('call me at 9:30 PM\n9:07 PM', 'call me at 9:30 PM')]):
            self.clock.t += 120
            r = FakeRunner([(0, '')])
            resp = self.send(raw, r, dryRun=False)
            self.assertTrue(resp['ok'], resp)
            osa = [c for c in r.calls if c[0] == '/usr/bin/osascript']
            self.assertEqual(len(osa), 1)
            self.assertEqual(osa[0][2], want)
        log = read_log()
        self.assertNotIn('love you', log)
        self.assertNotIn('9:07', log)

    def test_name_prefix_still_routes_after_strip(self):
        r = FakeRunner([(0, '')])
        resp = self.send('Test: on my way\n9:07 AM\n\n9:07 AM', r, dryRun=False)
        self.assertTrue(resp['ok'], resp)
        self.assertEqual([c for c in r.calls if c[0] == '/usr/bin/osascript'][0][2], 'on my way')

    def test_dry_run_sends_nothing(self):
        r = FakeRunner([])
        resp = self.send('test\n10:54 PM\n\n10:54 PM', r)
        self.assertTrue(resp['ok'] and resp['dryRun'], resp)
        self.assertEqual(r.count('/usr/bin/osascript'), 0)


if __name__ == '__main__':
    unittest.main()
