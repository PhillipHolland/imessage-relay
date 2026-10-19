"""v1.4.1: a plain "yes" / "no" answers a pending confirmation (never texted while one is pending),
and an exact full-name match to one Contacts card with one number sends without asking."""
import json
import os

from helpers import read_log
from test_v140 import Base, make_ab

SOLO = [('Jordan', 'Rivera', [('_$!<Mobile>!$_', '(555) 555-0188')]),
        ('Jordan', 'Avery', [('', '555-555-0178')]),
        ('Jo', 'Park', [('', '555-555-0161')])]


class BareWords(Base):
    def test_word_table(self):
        b = self.m.host.bare_confirm_word
        for t in ('yes', 'Yes', 'YES!', 'y', 'yep', 'ok', 'OK.', 'okay', 'send', 'Send it', 'do it', 'yes \U0001F44D'):
            self.assertEqual(b(t), 'yes', t)
        for t in ('no', 'No.', 'cancel', 'nope', "don't send"):
            self.assertEqual(b(t), 'no', t)
        for t in ('yes I will be there', 'ok see you at 5', 'sounds good', '', 'Sam: yes', 'yesterday'):
            self.assertIsNone(b(t), t)

    def test_plain_yes_confirms_block(self):
        self.send('/block Test Contact')
        self.assertNotIn('+15555550123', self.cfg().get('mute', []))
        self.assertIn('reply yes within 2 min', self.posts[-1][0].get('detail', ''))
        self.clock.t += 10
        r = self.send('Yes')
        self.assertEqual(self.osa(), [], 'the word is never texted')
        self.assertTrue(r.get('ok'), r)
        self.assertTrue(self.cfg().get('mute'), 'blocked on a plain yes')

    def test_plain_no_cancels_block(self):
        self.send('/block Test Contact')
        self.clock.t += 10
        self.send('no')
        self.assertEqual(self.osa(), [])
        self.assertFalse(self.cfg().get('mute'))
        self.assertIn('Cancelled', self.posts[-1][0]['detail'])
        self.clock.t += 5
        self.send('/block yes')                 # nothing staged any more
        self.assertFalse(self.cfg().get('mute'))

    def test_late_yes_is_swallowed_not_texted(self):
        self.send('/block Test Contact')
        self.clock.t += 300                     # prompt lapsed
        r = self.send('yes')
        self.assertEqual(self.osa(), [])
        self.assertFalse(self.cfg().get('mute'))
        self.assertIn('Nothing is waiting', self.posts[-1][0]['detail'])
        self.assertFalse(r.get('needsTarget'))

    def test_ok_with_no_prompt_is_a_normal_reply(self):
        r = self.send('ok')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2], 'ok')

    def test_new_contact_prompt_gives_one_notice(self):
        self.send('Jordan: hey')
        types = [p['type'] for p, _ in self.posts]
        self.assertEqual(types.count('needs_confirm'), 1)
        self.assertNotIn('needs_target', types, 'no "Who\u2019s this for?" for the same message')

    def test_no_bodies_in_log(self):
        self.send('/block Test Contact')
        self.send('yes')
        self.assertNotIn('Test Contact', read_log())


class ExactSkip(Base):
    def setUp(self):
        super().setUp()
        make_ab(self.tmp, SOLO)

    def test_exact_full_name_one_number_sends_now(self):
        r = self.send('Jordan RIVERA: on my way')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2], 'on my way')
        self.assertEqual(self.osa()[0][3], '+15555550188')
        self.assertIn('Jordan Rivera', [c['name'] for c in self.cfg()['contacts']])
        self.assertFalse([p for p, _ in self.posts if p['type'] in ('needs_confirm', 'needs_target')])
        self.assertEqual(r.get('note'), 'new contact, \u2022\u2022\u20220188')
        log = read_log()
        for s in ('on my way', 'Rivera', '5550188'):
            self.assertNotIn(s, log)

    def test_first_name_only_still_confirms(self):
        r = self.send('Jo: hi')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(self.osa(), [])

    def test_two_cards_same_first_name_confirms(self):
        r = self.send('Jordan: hi')
        self.assertFalse(r.get('ok'))
        self.assertEqual(self.osa(), [])

    def test_always_setting_confirms(self):
        raw = dict(self.raw, confirmNewContacts='always')
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, raw)
        r = self.send('Jordan Rivera: on my way')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(self.osa(), [])

    def test_setting_default_and_sanitized(self):
        self.assertEqual(self.cfg()['confirmNewContacts'], 'exact-skip')
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, dict(self.raw, confirmNewContacts='bogus'))
        self.assertEqual(self.cfg()['confirmNewContacts'], 'exact-skip')


class Routine(Base):
    def test_sent_note_renders(self):
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('rp141', os.path.join(here, '..', '..', 'routine', 'relay_post.py'))
        rp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rp)
        out = json.dumps(rp.format_event({'type': 'sent', 'label': 'Jordan Rivera', 'note': 'new contact, \u2022\u2022\u20220188',
                                          'at': '2026-01-01T00:00:00Z'}), ensure_ascii=False)
        self.assertIn('Jordan Rivera \u2713 (new contact, \u2022\u2022\u20220188)', out)
        out = json.dumps(rp.format_event({'type': 'needs_confirm', 'label': 'Sam', 'at': '2026-01-01T00:00:00Z'}), ensure_ascii=False)
        self.assertIn('Reply yes within 2 min.', out)
