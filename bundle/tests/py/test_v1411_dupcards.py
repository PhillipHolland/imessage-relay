"""v1.4.11: duplicate Contacts cards with the same full name are one person (handles merged, deduped);
several numbers -> a numbered pick prompt (most recently used first), never "start with the full name"."""
from helpers import read_log
from test_v140 import Base, make_ab

SAME = [('Jordan', 'Rivera', [('_$!<Mobile>!$_', '(555) 555-0101')]),
        ('Jordan', 'Rivera', [('', '555-555-0101')]),
        ('Jordan', 'Rivera', [('iPhone', '+1 555 555 0101')])]
DIFF = [('Jordan', 'Rivera', [('_$!<Mobile>!$_', '(555) 555-0101')]),
        ('Jordan', 'Rivera', [('', '555-555-0102')]),
        ('Jordan', 'Rivera', [('iPhone', '555-555-0103'), ('', '555-555-0101')])]


class DupCards(Base):
    def details(self):
        return ' | '.join(p.get('detail', '') for p, _ in self.posts)

    def test_full_name_three_cards_one_number_is_one_match(self):
        make_ab(self.tmp, SAME)
        r = self.send('Jordan Rivera: hi there')
        self.assertNotIn('full name', self.details())
        self.assertNotIn('more than one', self.details())
        if r.get('ok'):                                    # exact-skip: sent and saved
            self.assertEqual(self.osa()[0][2:4], ['hi there', '+15555550101'])
        else:
            self.assertTrue(r.get('needsConfirm'), r)
            self.assertEqual(r['handle'], '+15555550101')
            self.assertNotIn('options', r)

    def test_first_name_three_cards_one_number_confirms(self):
        make_ab(self.tmp, SAME)
        r = self.send('Jordan: hi')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual((r['label'], r['handle']), ('Jordan Rivera', '+15555550101'))
        self.assertEqual(self.osa(), [])

    def test_full_name_three_numbers_pick_prompt_recent_first_then_pick_sends(self):
        make_ab(self.tmp, DIFF)
        self.m.rc.record_inbound('+15555550103', self.clock.t - 5)
        r = self.send('Jordan Rivera: hi there')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r['options'][0], '+15555550103')           # most recently used is 1
        self.assertEqual(sorted(r['options']), ['+15555550101', '+15555550102', '+15555550103'])
        d = self.details()
        for t in ('1) \u2022\u2022\u20220103', '2) ', '3) ', 'Reply yes', 'reply 2-3'):
            self.assertIn(t, d)
        self.assertNotIn('full name', d)
        self.assertNotIn('5550102', d)
        self.assertEqual(self.osa(), [])
        r = self.send('Jordan Rivera: hi there', confirmed=True, handle=r['options'][2])   # he answered "3"
        self.assertTrue(r['ok'], r)
        self.assertIn(self.osa()[0][3], ('+15555550101', '+15555550102'))
        self.assertNotIn('hi there', read_log())

    def test_unoffered_handle_still_refused(self):
        make_ab(self.tmp, DIFF)
        self.send('Jordan Rivera: hi')
        r = self.send('Jordan Rivera: hi', confirmed=True, handle='+15555550141')
        self.assertFalse(r['ok'])
        self.assertEqual(self.osa(), [])

    def test_full_name_of_different_people_never_says_full_name(self):
        r = self.m.host._merge_same_name([{'name': 'Jordan Rivera', 'handle': 'a', 'handles': [{'handle': 'a'}]},
                                          {'name': 'jordan  rivera', 'handle': 'b', 'handles': [{'handle': 'b'}, {'handle': 'a'}]}])
        self.assertEqual(len(r), 1)
        self.assertEqual([x['handle'] for x in r[0]['handles']], ['a', 'b'])
