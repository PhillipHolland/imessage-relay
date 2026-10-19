"""v1.4.4: an unplaceable quote never falls back to a sender who is not in Contacts; a reply never
reaches a group (bare, quoted, or named) -- group replies are not supported, so it asks instead."""
import json

from helpers import read_log
from test_v143_handles import Base

SPAM = '+15555550166'      # fictional, not in Contacts
RORY = '+15555550133'
GROUP = 'group:chat0001'


class Safety(Base):
    def setUp(self):
        super().setUp()
        self.raw['relayAll'] = True
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.clock.t += 600

    def last(self, handle, label, ago=30):
        self.m.rc.record_inbound(handle, self.clock.t - ago, label=label)

    def kinds(self, t):
        return [p for p, _ in self.posts if p['type'] == t]

    def test_unresolved_quote_never_offers_a_non_contacts_sender(self):
        self.last(SPAM, '(555) 555-0166')
        r = self.send('Thanks!', quotedText='something we cannot place')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])
        self.assertFalse(self.kinds('needs_confirm'))
        self.assertEqual(self.kinds('needs_target')[-1]['detail'], 'Who\'s this for? Start with a name, like "Alex: \u2026"')

    def test_unresolved_quote_still_offers_a_contacts_name(self):
        self.last(RORY, 'Rory')
        r = self.send('Thanks!', quotedText='something we cannot place')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r['handle'], RORY)
        self.assertEqual(self.osa(), [])

    def test_bare_reply_with_no_quote_to_non_contacts_still_confirms(self):
        self.last(SPAM, '(555) 555-0166')
        r = self.send('Thanks!')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertIn('not in Contacts', self.kinds('needs_confirm')[-1]['detail'])
        self.assertEqual(self.osa(), [])

    def test_bare_reply_after_group_message_never_goes_to_the_group(self):
        self.last(GROUP, 'Family')
        r = self.send('sounds good')
        self.assertEqual(self.osa(), [])
        self.assertNotEqual(r.get('handle'), GROUP)

    def test_quoting_a_group_line_never_sends(self):
        self.last(GROUP, 'Family')
        r = self.send('sounds good', quotedText='Family \u00b7 Sam: dinner?')
        self.assertEqual(self.osa(), [])
        self.assertFalse(r.get('ok') and not r.get('needsConfirm'), r)

    def test_naming_a_group_never_sends(self):
        self.last(GROUP, 'Family')
        r = self.send('Family: sounds good')
        self.assertEqual(self.osa(), [])
        self.assertNotIn('sounds good', read_log())

    def test_confirmed_resend_to_a_group_handle_is_refused(self):
        self.last(GROUP, 'Family')
        r = self.send('sounds good', confirmed=True, handle=GROUP)
        self.assertEqual(self.osa(), [])
        self.assertFalse(r.get('ok'), r)
