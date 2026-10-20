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
        for t in ('1 \u00b7 \u2022\u2022\u20220103', '\n2 \u00b7 ', '\n3 \u00b7 ', 'Reply 1\u20133, or no.'):
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


MANY = [('Jordan', 'Rivera', [('_$!<Home>!$_', '555-555-0201'), ('_$!<Work>!$_', '555-555-0202')]),
        ('Jordan', 'Rivera', [('_$!<Mobile>!$_', '555-555-0203'), ('pref', '555-555-0204')]),
        ('Jordan', 'Rivera', [('iPhone', '555-555-0205'), ('', '555-555-0206')])]


class Rank(Base):
    """v1.4.12: phones first (recent, then mobile/iPhone/pref), business emails dropped, at most 4 shown."""
    def test_rank_trim_and_drop_business_mail(self):
        hs = [{'handle': 'jr@k12.example.org', 'label': 'work'}, {'handle': '+15555550201', 'label': 'home'},
              {'handle': '+15555550203', 'label': 'mobile'}, {'handle': 'jr@icloud.com', 'label': ''},
              {'handle': '+15555550204', 'label': 'pref'}, {'handle': '+15555550205', 'label': 'iphone'},
              {'handle': '+15555550202', 'label': 'work'}]
        self.m.rc.record_inbound('+15555550201', self.clock.t - 5)
        out = [x['handle'] for x in self.m.host._rank_options(hs, '+15555550203')]
        self.assertEqual(out, ['+15555550201', '+15555550203', '+15555550205', '+15555550204'])
        self.assertNotIn('jr@k12.example.org', out)
        only_mail = [{'handle': 'jr@k12.example.org', 'label': ''}]
        self.assertEqual(self.m.host._rank_options(only_mail)[0]['handle'], 'jr@k12.example.org')   # nothing better

    def test_six_numbers_show_four_one_per_line(self):
        make_ab(self.tmp, MANY)
        r = self.send('Jordan Rivera: hi')
        self.assertEqual(len(r['options']), 4)
        ev = [p for p, _ in self.posts if p['type'] == 'needs_confirm'][-1]['detail']
        self.assertEqual(ev.count('\n'), 5)
        self.assertTrue(ev.startswith('Text Jordan Rivera at which?\n1 \u00b7 '), ev)
        self.assertTrue(ev.endswith('\nReply 1\u20134, or no.'), ev)

    def test_confirm_prompt_is_typed_text_not_a_reaction(self):
        make_ab(self.tmp, MANY)
        msg = {'type': 'send', 'text': 'Jordan Rivera: hi', 'conversationId': '1111-2222', 'ui': True,
               'xUuid': 'df48633d-6588-4b18-bc77-000000000001'}
        from helpers import FakeRunner
        r = self.m.host.handle_request(msg, cfg=self.cfg(), runner=FakeRunner([(0, '')] * 5), sleeper=self.sleeper,
                                       now=self.clock.now, poster=self.poster)
        ui = r.get('ui') or {}
        self.assertFalse(ui.get('react'), ui)
        self.assertTrue(ui['type'][0]['text'].startswith('\U0001f916 Text Jordan Rivera at which?'))
        self.assertFalse([p for p, _ in self.posts if p['type'] == 'needs_confirm'], 'routine copy is deferred')
