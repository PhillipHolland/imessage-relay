"""v1.4.17: conversation headers ("\U0001f4ac <label> \u00b7 7:20 PM") and their guardrails. Fictional names."""
import os
import sys
import unittest

from test_v140 import Base

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

T0 = 1_800_000_000


def line(i, who, at, group=None, reaction=False):
    m = {'id': 'm%d' % i, 'label': who, 'handle': '+1555555%04d' % (abs(hash(who)) % 10000), 'text': 'x',
         'receivedAt': '', 'localTime': '7:20 PM'}
    if group:
        m['group'] = {'label': group}
    if reaction:
        m['reaction'] = True
    ln = {'id': m['id'], 'text': rp._message_line(m), 'body': True, 'hdr': rp._hdr_meta(m)}
    ln['hdr']['at'] = at
    return ln


class Headers(unittest.TestCase):
    def test_person_change_and_gap(self):
        lines = [line(1, 'Alex', T0), line(2, 'Alex', T0 + 60), line(3, 'Sam', T0 + 120), line(4, 'Alex', T0 + 180)]
        out, st = rp.add_headers(lines, 30, {})
        heads = [ln['text'] for ln in out if ln.get('header')]
        self.assertEqual(heads, ['\U0001f4ac Alex \u00b7 7:20 PM', '\U0001f4ac Sam \u00b7 7:20 PM', '\U0001f4ac Alex \u00b7 7:20 PM'])
        self.assertTrue(all(not ln['body'] for ln in out if ln.get('header')))
        self.assertNotIn('Alex', str(st))                     # state holds a hash and a time only
        out2, _ = rp.add_headers([line(5, 'Alex', T0 + 240)], 30, st)
        self.assertFalse([ln for ln in out2 if ln.get('header')])
        out3, _ = rp.add_headers([line(6, 'Alex', T0 + 180 + 1800)], 30, st)
        self.assertEqual(len([ln for ln in out3 if ln.get('header')]), 1)

    def test_off_reaction_and_group(self):
        self.assertEqual(len(rp.add_headers([line(1, 'Alex', T0)], 0, {})[0]), 1)
        self.assertEqual(len(rp.add_headers([line(1, 'Alex', T0, reaction=True)], 30, {})[0]), 1)
        out, _ = rp.add_headers([line(1, 'Sam', T0, group='Family')], 30, {})
        self.assertEqual(out[0]['text'], '\U0001f4ac Family \u00b7 7:20 PM')


class HeaderGuards(Base):
    def test_quoted_header_asks_first_and_header_text_is_refused(self):
        import relay_common as rc
        r = rc.route_reply('see you soon', '\U0001f4ac Test Contact \u00b7 7:20 PM', self.cfg(), [], self.clock.now())
        self.assertEqual(r.get('by'), 'quote')
        self.assertTrue(r.get('confirm'), r)                  # mapped to that person, but never sent without yes
        r2 = self.send('\U0001f4ac Test Contact \u00b7 7:20 PM')
        self.assertFalse(r2.get('ok'), r2)
        self.assertEqual(self.osa(), [])


if __name__ == '__main__':
    unittest.main()
