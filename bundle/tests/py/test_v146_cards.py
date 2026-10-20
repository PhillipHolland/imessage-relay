"""v1.4.6: a shared post arrives as its URL; every refusal tells him why (text line + ⚠️ on his message).
Fictional names only."""
import os
import sys
import unittest

from helpers import FakeRunner
from test_v146 import HostBase, XU, CONV

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

POST = 'https://x.com/ExampleNews/status/1850000000000000001'
WARN = '\u26a0\ufe0f'


class Cards(HostBase):
    def test_name_plus_post_url_sends_the_url(self):
        runner = FakeRunner([(0, '')])
        r = self.req({'type': 'send', 'text': 'Jordan: ' + POST, 'cards': 1, 'quoteProbe': 'E=0.div'}, runner=runner)
        self.assertTrue(r.get('sent'), r)
        osa = [c for c in runner.calls if c[0] == '/usr/bin/osascript'][0]
        self.assertEqual(osa[2], POST)

    def test_empty_after_prefix_says_why_with_a_warning_reaction(self):
        r = self.req({'type': 'send', 'text': 'Jordan:', 'ui': True, 'xUuid': XU}, runner=FakeRunner([]))
        self.assertFalse(r['ok'])
        self.assertEqual(r['ui']['react'], [{'emoji': WARN, 'uuid': XU}])
        [p] = self.posts
        self.assertEqual((p['type'], p['reason']), ('refused', 'not_sent'))
        line = rp.format_event(p)['lines'][0]['text']
        self.assertEqual(line, WARN + ' Not sent. Nothing came after \u201cJordan:\u201d.')

    def test_paused_and_other_refusals_also_say_why(self):
        self.req({'type': 'send', 'text': 'Jordan: hi'}, paused=True)
        self.assertIn('paused', self.posts[-1]['detail'])
        self.req({'type': 'send', 'text': 'x' * 4001})
        self.assertEqual(self.posts[-1]['detail'], 'Over 4000 characters.')

    def test_not_his_relay_chat_stays_silent(self):
        self.h.handle_request({'type': 'send', 'text': 'Jordan: hi', 'conversationId': '9999-8888'}, cfg=self.cfg(),
                              runner=FakeRunner([]), sleeper=self.clock.sleep, now=self.clock.now, poster=self.poster)
        self.assertEqual(self.posts, [])

    def test_notice_is_body_free(self):
        self.req({'type': 'send', 'text': 'Jordan:   ', 'ui': True, 'xUuid': XU})
        self.assertNotIn('   ', self.posts[0]['detail'])

    def test_probe_logs_the_card_count(self):
        from helpers import read_log
        self.req({'type': 'send', 'text': 'Jordan: ' + POST, 'cards': 1, 'quoteProbe': 'E=0.div'})
        self.assertIn('"cards": 1', read_log() if isinstance(read_log(), str) else str(read_log()))


if __name__ == '__main__':
    unittest.main()
