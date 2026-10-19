"""v1.4.2: X's native Reply ("Replied to <name>" + preview) routes to the quoted person; a roster-only
last texter (relay-all) gets a confirm instead of "Who's this for?"; an unresolved quote falls back to the
last texter (always confirmed); the "Who's this for?" example is generic."""
from helpers import read_log
from test_v140 import Base

RORY = '+15555550133'


class Quote(Base):
    def setUp(self):
        super().setUp()
        self.raw['relayAll'] = True
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.clock.t += 600                  # the configured contact wrote 11 min ago, Rory 30 s ago
        self.m.rc.record_inbound(RORY, self.clock.t - 30, label='Rory')

    def send_q(self, text, quoted, **kw):
        msg = {'type': 'send', 'text': text, 'conversationId': '1111-2222', 'quotedText': quoted}
        msg.update(kw)
        from helpers import FakeRunner
        self.runner = FakeRunner([(0, '')] * 5)
        return self.m.host.handle_request(msg, cfg=self.cfg(), runner=self.runner, sleeper=self.sleeper,
                                          now=self.clock.now, poster=self.poster)

    def test_native_reply_preview_routes_to_tim(self):
        r = self.send_q('Ha, true', 'Rory: Not since the jump')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2:4], ['Ha, true', RORY])
        self.assertNotIn('Ha, true', read_log())

    def test_preview_with_reply_label_and_bot_name(self):
        r = self.send_q('Ha, true', 'iMessage Rory: Not since the jump')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][3], RORY)

    def test_unresolved_quote_falls_back_to_last_texter_with_confirm(self):
        r = self.send_q('Ha, true', 'something we cannot place')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r['handle'], RORY)
        self.assertEqual(self.osa(), [])
        ev = [p for p, _ in self.posts if p['type'] == 'needs_confirm'][-1]
        self.assertEqual(ev['detail'], '\u2753 Reply to Rory? Reply yes within 2 min.')
        self.assertFalse([p for p, _ in self.posts if p['type'] == 'needs_target'])

    def test_bare_reply_to_roster_last_texter_asks_not_whos_this_for(self):
        r = self.send('Ha, true')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r['handle'], RORY)
        self.assertEqual(self.osa(), [])
        self.clock.t += 10
        r = self.send('Ha, true', confirmed=True, handle=RORY)
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][3], RORY)

    def test_roster_fallback_needs_relay_all(self):
        self.raw['relayAll'] = False
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        r = self.send('Ha, true')
        self.assertNotEqual(r.get('handle'), RORY)


class GenericExample(Base):
    def test_whos_this_for_uses_a_generic_name(self):
        self.clock.t += 3 * 3600           # nobody recent
        r = self.send('hello there')
        self.assertTrue(r.get('needsTarget'), r)
        ev = [p for p, _ in self.posts if p['type'] == 'needs_target'][-1]
        self.assertIn('"Alex: \u2026"', ev['detail'])
        self.assertNotIn('Test Contact', ev['detail'])
        self.assertNotIn('Test', ev['detail'])


class FastDeliveryCheck(Base):
    """v1.4.2 latency: with a send awaiting proof, the watcher re-checks chat.db every ~2 s between polls."""
    def test_pending_checked_every_two_seconds(self):
        import os

        class Stop(Exception):
            pass
        w = self.watcher
        calls, sleeps = [], []
        w.tick = lambda: None
        w.process_pending = lambda cfg: calls.append(len(sleeps))

        def sl(s):
            sleeps.append(s)
            if len(sleeps) >= 10:
                raise Stop()
        w.sleeper = sl
        os.makedirs(self.m.rc.PENDING_DIR, exist_ok=True)
        open(os.path.join(self.m.rc.PENDING_DIR, '1.json'), 'w').write('{}')
        import signal as _s
        old = _s.getsignal(_s.SIGTERM)
        try:
            w.run_forever()
        except Stop:
            pass
        finally:
            _s.signal(_s.SIGTERM, old)
        self.assertGreaterEqual(len(calls), 4, calls)      # 10 s of sleeping -> ~5 checks, not 0-1

    def test_no_pending_no_extra_checks(self):
        class Stop(Exception):
            pass
        w = self.watcher
        calls, sleeps = [], []
        w.tick = lambda: None
        w.process_pending = lambda cfg: calls.append(1)

        def sl(s):
            sleeps.append(s)
            if len(sleeps) >= 10:
                raise Stop()
        w.sleeper = sl
        import signal as _s
        old = _s.getsignal(_s.SIGTERM)
        try:
            w.run_forever()
        except Stop:
            pass
        finally:
            _s.signal(_s.SIGTERM, old)
        self.assertEqual(calls, [])
