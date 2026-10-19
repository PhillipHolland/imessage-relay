"""v1.4.2: smart name recognition (any case; a name only when the relay list or Contacts know it, close
typos always ask) and thumbs-up / thumbs-down reactions on a pending prompt."""
import os
import sqlite3

from helpers import read_log
from test_v140 import Base, make_ab

PEOPLE = [('Jordan', 'Rivera', [('_$!<Mobile>!$_', '(555) 555-0188')]),
          ('Jo', 'Park', [('', '555-555-0161')]),
          ('Robert', 'Quill', [('', '555-555-0162')])]


def add_nick(tmp, first, nick):
    path = os.path.join(tmp, 'ab', 'x.abcddb')
    con = sqlite3.connect(path)
    con.execute('ALTER TABLE ZABCDRECORD ADD COLUMN ZNICKNAME TEXT')
    con.execute('UPDATE ZABCDRECORD SET ZNICKNAME=? WHERE ZFIRSTNAME=?', (nick, first))
    con.commit()
    con.close()


class Names(Base):
    def setUp(self):
        super().setUp()
        make_ab(self.tmp, PEOPLE)

    def sent(self):
        return [(c[2], c[3]) for c in self.osa()]

    def confirms(self):
        return [p for p, _ in self.posts if p['type'] == 'needs_confirm']

    def test_lowercase_exact_full_name_sends(self):
        r = self.send('jordan rivera: on my way')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.sent(), [('on my way', '+15555550188')])
        self.assertEqual(r.get('note'), 'new contact, \u2022\u2022\u20220188')
        self.assertIn('Jordan Rivera', [c['name'] for c in self.cfg()['contacts']])
        for s in ('on my way', 'Rivera', 'jordan', '5550188'):
            self.assertNotIn(s, read_log())

    def test_verb_and_comma_forms(self):
        r = self.send('text jordan rivera hey there')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.sent(), [('hey there', '+15555550188')])

    def test_comma_form(self):
        r = self.send('jordan rivera, running late')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.sent(), [('running late', '+15555550188')])

    def test_lowercase_first_name_asks(self):
        r = self.send('jo: lunch?')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(self.osa(), [])
        self.clock.t += 20
        r = self.send('jo: lunch?', confirmed=True, handle='+15555550161')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.sent(), [('lunch?', '+15555550161')])

    def test_tell_first_name_picks_the_known_word(self):
        r = self.send('tell jo I am late')
        self.assertTrue(r.get('needsConfirm'), r)
        self.clock.t += 5
        r = self.send('tell jo I am late', confirmed=True, handle='+15555550161')
        self.assertEqual(self.sent(), [('I am late', '+15555550161')])

    def test_non_names_stay_ordinary(self):
        for t in ('Breakfast plan: eggs', 'breakfast menu ideas: eggs', 'sounds good, see you', 'text me later',
                  'Zed Quark: hi', 'note to self: milk'):
            self.posts.clear()
            r = self.send(t)
            self.assertTrue(r['ok'], (t, r))
            self.assertEqual(self.sent(), [(t, '+15555550123')], t)
            self.assertFalse([p for p, _ in self.posts if p['type'] in ('needs_target', 'needs_confirm')], t)

    def test_typo_in_contacts_asks_never_sends(self):
        r = self.send('jordn rivera: hi')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(self.osa(), [])
        self.assertIn('Did you mean Jordan Rivera?', self.confirms()[-1]['detail'])
        self.clock.t += 10
        r = self.send('jordn rivera: hi', confirmed=True, handle='+15555550188')
        self.assertEqual(self.sent(), [('hi', '+15555550188')])

    def test_typo_never_skips_even_when_exact_skip(self):
        self.assertEqual(self.cfg()['confirmNewContacts'], 'exact-skip')
        r = self.send('Robrt Quill: hi')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(self.osa(), [])

    def test_typo_of_relay_contact_asks(self):
        self.raw['contacts'].append({'name': 'Samantha Vale', 'label': 'Samantha', 'handle': '+15555550144'})
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        r = self.send('samanta: hi there')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertIn('Did you mean Samantha?', self.confirms()[-1]['detail'])
        self.assertEqual(self.osa(), [])
        self.clock.t += 10
        r = self.send('samanta: hi there', confirmed=True, handle='+15555550144')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.sent(), [('hi there', '+15555550144')])
        self.assertEqual(len([c for c in self.cfg()['contacts'] if c['handle'] == '+15555550144']), 1)

    def test_lowercase_relay_label_routes_directly(self):
        r = self.send('test contact: hi')
        self.assertEqual(self.sent(), [('hi', '+15555550123')])

    def test_nickname_asks(self):
        add_nick(self.tmp, 'Robert', 'Bobby')
        r = self.send('bobby: hi')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r['handle'], '+15555550162')
        self.assertEqual(self.osa(), [])

    def test_contacts_match_already_a_contact_under_another_label(self):
        make_ab(self.tmp, PEOPLE + [('Pat', 'Testcard', [('', '555-555-0123')])])
        r = self.send('pat testcard: hi')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.sent(), [('hi', '+15555550123')])
        self.assertFalse(r.get('note'))
        self.assertEqual(len(self.cfg()['contacts']), 1)

    def test_watcher_down_lowercase_is_ordinary_and_quick(self):
        self.watcher_up = False
        t0 = self.clock.t
        r = self.send('breakfast menu ideas: eggs')
        self.assertTrue(r['ok'], r)
        self.assertLessEqual(self.clock.t - t0, 4)
        self.assertEqual(self.sent(), [('breakfast menu ideas: eggs', '+15555550123')])


class Reactions(Base):
    def react(self, word, conv=None):
        return self.m.host.handle_request({'type': 'reaction', 'word': word, 'conversationId': conv or '1111-2222'},
                                          cfg=self.cfg(), now=self.clock.now, poster=self.poster)

    def test_thumbs_up_confirms_block(self):
        self.send('/block Test Contact')
        self.clock.t += 10
        self.react('yes')
        self.assertTrue(self.cfg().get('mute'))

    def test_thumbs_down_cancels_block(self):
        self.send('/block Test Contact')
        self.clock.t += 10
        self.react('no')
        self.clock.t += 5
        self.send('/block yes')
        self.assertFalse(self.cfg().get('mute'))

    def test_nothing_pending_is_ignored_silently(self):
        n = len(self.posts)
        r = self.react('yes')
        self.assertTrue(r.get('ignored'), r)
        self.assertEqual(len(self.posts), n)

    def test_expired_prompt_ignored(self):
        self.send('/block Test Contact')
        self.clock.t += 200
        r = self.react('yes')
        self.assertTrue(r.get('ignored'), r)
        self.assertFalse(self.cfg().get('mute'))

    def test_other_conversation_refused(self):
        self.send('/block Test Contact')
        r = self.react('yes', conv='9999-0000')
        self.assertFalse(r['ok'])
        self.assertFalse(self.cfg().get('mute'))
