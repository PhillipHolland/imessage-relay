"""v1.4.31: the host records the X Chat tab heartbeat, and relayctl status says when that tab is quiet."""
import time
import unittest

from helpers import fresh, tmpdir, FakeClock


class Stamp(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.clock = FakeClock()

    def cfg(self):
        return self.m.rc.sanitize_config({'relayConversationId': '1111-2222', 'dryRun': True})

    def test_content_stamp_is_stored_and_a_move_is_not_fully_throttled(self):
        h = self.m.host
        now = self.clock.now
        base_ms = 1_700_000_000_000
        h.handle_request({'type': 'get_config', 'extVersion': '1.4.31', 'contentState': 'ok', 'contentAt': base_ms},
                         cfg=self.cfg(), now=now)
        e = self.m.rc.read_json(h.EXT_SEEN_PATH)
        self.assertEqual(e['extVersion'], '1.4.31')
        self.assertEqual(e['contentState'], 'ok')
        self.assertEqual(e['contentAt'], 1_700_000_000.0)
        at = e['at']
        self.clock.t += 10
        h.handle_request({'type': 'get_config', 'extVersion': '1.4.31', 'contentState': 'ok',
                          'contentAt': base_ms + 10_000}, cfg=self.cfg(), now=now)
        e2 = self.m.rc.read_json(h.EXT_SEEN_PATH)
        self.assertEqual(e2['at'], at, 'a few seconds of the same stamp does not rewrite')
        self.clock.t += 10
        h.handle_request({'type': 'get_config', 'extVersion': '1.4.31', 'contentState': 'stale', 'contentAt': base_ms},
                         cfg=self.cfg(), now=now)
        e3 = self.m.rc.read_json(h.EXT_SEEN_PATH)
        self.assertEqual(e3['contentState'], 'stale')
        self.assertGreater(e3['at'], at)
        self.assertNotIn('text', e3)

    def test_status_names_a_stale_tab_and_a_missing_tab(self):
        ctl = self.m.ctl
        rc = self.m.rc
        now = time.time()
        rc.write_json_atomic(ctl.EXT_SEEN_PATH, {
            'extVersion': rc.EXTENSION_VERSION, 'at': now, 'contentAt': now - 400, 'contentState': 'stale'})
        st = ctl.cmd_status()
        self.assertTrue(any('X Chat tab' in p for p in st['problems']))
        self.assertEqual(st['extension']['contentState'], 'stale')
        self.assertGreater(st['extension']['contentAgoSeconds'], 300)
        rc.write_json_atomic(ctl.EXT_SEEN_PATH, {
            'extVersion': rc.EXTENSION_VERSION, 'at': now, 'contentAt': 0, 'contentState': 'no-tab'})
        st2 = ctl.cmd_status()
        self.assertTrue(any('no x.com/i/chat tab' in p for p in st2['problems']))
        rc.write_json_atomic(ctl.EXT_SEEN_PATH, {
            'extVersion': rc.EXTENSION_VERSION, 'at': now, 'contentAt': now, 'contentState': 'ok'})
        st3 = ctl.cmd_status()
        self.assertFalse(any('X Chat tab' in p or 'no x.com/i/chat' in p for p in st3['problems']))

    def test_an_older_stamp_without_content_adds_no_tab_problem(self):
        ctl = self.m.ctl
        rc = self.m.rc
        rc.write_json_atomic(ctl.EXT_SEEN_PATH, {'extVersion': rc.EXTENSION_VERSION, 'at': time.time()})
        st = ctl.cmd_status()
        self.assertEqual(st['extension']['contentState'], '')
        self.assertFalse(any('X Chat tab' in p or 'no x.com/i/chat' in p for p in st['problems']))
