"""v1.4.13: an answer to a pending pick prompt is checked against the pick first, never routed as a new text."""
from helpers import FakeRunner, read_log
from test_v140 import Base, CONV


class PendingPick(Base):
    def test_index_reply_never_becomes_a_new_guess(self):
        r = self.send('Jordan Rivera: hey there')
        self.assertTrue(r.get('needsConfirm') and r.get('options'), r)
        n = len(self.posts)
        r2 = self.send('2')
        self.assertTrue(r2.get('ok') and r2.get('skipped'), r2)
        self.assertFalse(r2.get('needsConfirm'), r2)
        self.assertEqual(self.osa(), [])
        self.assertFalse([p for p, _ in self.posts[n:] if p['type'] == 'needs_confirm'])

    def test_four_digits_matching_no_option_is_ordinary_text(self):
        import imessage_relay_host as h
        self.assertEqual(h.pick_answer('2026', ['+15555550188', '+15555550101']), (False, ''))
        self.assertEqual(h.pick_answer('0101', ['+15555550188', '+15555550101']), (True, '+15555550101'))
        self.assertEqual(h.pick_answer('7', ['+15555550188']), (True, ''))


    def test_bare_number_after_any_prompt_never_goes_to_recent(self):
        import imessage_relay_host as h
        h._note_prompt(self.clock.now())
        r = self.send('2')
        self.assertTrue(r.get('ok') and r.get('skipped'), r)
        self.assertFalse(r.get('needsConfirm'), r)
        self.assertEqual(self.osa(), [])


    def test_v1415_short_reply_after_prompt_is_never_texted(self):
        import imessage_relay_host as h
        h._note_prompt(self.clock.now())
        for t in ('lol', '\U0001F44D', 'alex@', '12345'):
            r = self.send(t)
            self.assertTrue(r.get('ok') and r.get('skipped'), (t, r))
        self.assertEqual(self.osa(), [])
        self.assertTrue(h.short_answer('ok') and not h.short_answer('see you at noon'))
        self.assertTrue(h.emoji_only('\U0001F680'))
        self.assertFalse(h.emoji_only('ok') or h.emoji_only('2') or h.emoji_only('alex@') or h.emoji_only('...'))
        # v1.4.21: emoji-only native Reply whose quote names a contact still sends during the window.
        self.runner = FakeRunner([(0, '')] * 5)
        sent = self.m.host.handle_request(
            {'type': 'send', 'text': '\U0001F680', 'quotedText': 'Test Contact: on my way',
             'conversationId': CONV},
            cfg=self.cfg(), runner=self.runner, sleeper=self.sleeper, now=self.clock.now, poster=self.poster)
        self.assertTrue(sent.get('ok') and sent.get('sent'), sent)
        self.assertEqual(sent.get('routedBy'), 'quote')
        self.assertEqual(self.osa()[0][2], '\U0001F680')
        self.assertEqual(self.osa()[0][3], '+15555550123')
        self.assertNotIn('on my way', read_log())
        for t in ('ok', '2'):
            r = self.send(t)
            self.assertTrue(r.get('ok') and r.get('skipped'), (t, r))
            self.assertEqual(self.osa(), [])
        n = len(self.posts)
        sig = self.m.host.handle_request(
            {'type': 'signal', 'status': 'skipped', 'reason': 'empty'},
            cfg=self.cfg(), runner=FakeRunner([(0, '')] * 3), sleeper=self.sleeper,
            now=self.clock.now, poster=self.poster)
        self.assertTrue(sig.get('ok'), sig)
        refused = [p for p, _ in self.posts[n:] if p.get('type') == 'refused']
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].get('reason'), 'not_sent')
        self.assertEqual(refused[0].get('detail') or '', 'There was nothing to send.')
        self.assertNotIn('text', refused[0])
        n = len(self.posts)
        self.m.host.handle_request(
            {'type': 'signal', 'status': 'skipped', 'reason': 'see you at noon'},
            cfg=self.cfg(), runner=FakeRunner([(0, '')] * 3), sleeper=self.sleeper,
            now=self.clock.now, poster=self.poster)
        other = [p for p, _ in self.posts[n:] if p.get('type') == 'refused']
        self.assertEqual(other[0]['detail'], 'That reply was not sent.')
        self.assertNotIn('noon', read_log())

    def test_v1415_prompt_row_text_refused(self):
        r = self.send('Text Jordan Rivera at which?\n1 \u00b7 \u2022\u2022\u20220188 mobile')
        self.assertFalse(r.get('ok'), r)
        self.assertEqual(self.osa(), [])


del Base
