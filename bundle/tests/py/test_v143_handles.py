"""v1.4.3: business / short-code / odd handles are never a last-texter guess; guessed prompts show the
Contacts name or a masked handle + "not in Contacts"; text-free quote diagnostics are logged."""
import json
import unittest

from helpers import read_log
from helpers import FakeRunner
from test_v140 import CONV, Base as _Base


class Base(_Base):
    def send(self, text, confirmed=False, handle=None, runner=None, **kw):
        msg = {'type': 'send', 'text': text, 'conversationId': CONV}
        if confirmed:
            msg.update(confirmed=True, handle=handle)
        msg.update(kw)
        self.runner = runner or FakeRunner([(0, '')] * 5)
        return self.m.host.handle_request(msg, cfg=self.cfg(), runner=self.runner, sleeper=self.sleeper,
                                          now=self.clock.now, poster=self.poster)

RORY = '+15555550133'
BIZ = '+1577000000000001'       # Apple Business Chat style over-long id (fictional)
SHORT = '24273'                  # a 5-digit short code (fictional)
NOBODY = '+15555550144'          # a person not in Contacts


class PersonHandle(unittest.TestCase):
    def test_classification(self):
        import relay_common as rc
        for h in ('+15555550133', '+447700900123', 'someone@example.com', '+12125550100'):
            self.assertTrue(rc.is_person_handle(h), h)
        for h in (BIZ, SHORT, '262966', 'urn:biz:1234abcd-0000-0000-0000-000000000000', '+1055555501',
                  '+15550123', '', None, 'not a handle', '+11555550133'):
            self.assertFalse(rc.is_person_handle(h), h)

    def test_guess_label(self):
        import relay_common as rc
        self.assertEqual(rc.guess_label({'label': 'Rory', 'handle': RORY}), 'Rory')
        self.assertEqual(rc.guess_label({'label': '(555) 555-0144', 'handle': NOBODY}),
                         '+1 \u2022\u2022\u20220144 (\u26a0\ufe0f not in Contacts)')

    def test_clean_probe_is_closed_and_masked(self):
        import relay_common as rc
        fp = rc.clean_probe('E=0.div@message-12345678-1234-1234-1234-123456789abc{aria-label}!RA "hi" Ünï 5551234 😀')
        self.assertNotIn('12345678-1234', fp)
        self.assertNotIn('"', fp)
        self.assertNotIn('5551234', fp)
        self.assertNotIn('\U0001f600', fp)
        self.assertIn('!RA', fp)
        self.assertLessEqual(len(rc.clean_probe('x' * 9000)), rc.PROBE_MAX)


class Odd(Base):
    def setUp(self):
        super().setUp()
        self.raw['relayAll'] = True
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.clock.t += 600

    def last(self, handle, label, ago=30):
        self.m.rc.record_inbound(handle, self.clock.t - ago, label=label)

    def targets(self):
        return [p for p, _ in self.posts if p['type'] == 'needs_target']

    def confirms(self):
        return [p for p, _ in self.posts if p['type'] == 'needs_confirm']

    def test_business_last_texter_is_never_guessed(self):
        self.last(RORY, 'Rory', ago=200)
        self.last(BIZ, '+1577000000000001')
        r = self.send('Sounds good thanks')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])
        self.assertEqual(self.targets()[-1]['detail'], 'Who\'s this for? Start with a name, like "Alex: \u2026"')
        self.assertFalse(self.confirms())
        self.assertNotIn('Sounds good', read_log())

    def test_yes_after_business_prompt_sends_nothing(self):
        self.last(BIZ, '+1577000000000001')
        self.send('Sounds good thanks')
        self.clock.t += 5
        r = self.send('yes')
        self.assertEqual(self.osa(), [])
        self.assertFalse(r.get('ok') and r.get('sent'), r)

    def test_short_code_last_texter_is_never_guessed(self):
        self.last(SHORT, SHORT)
        r = self.send('STOP please')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])

    def test_business_reachable_by_quote(self):
        self.last(BIZ, 'Acme Support')
        r = self.send('Thanks!', quotedText='Acme Support: Your order shipped')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][3], BIZ)

    def test_business_reachable_by_name(self):
        self.last(BIZ, 'Acme Support')
        r = self.send('Acme Support: thanks!')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2:4], ['thanks!', BIZ])

    def test_guess_prompt_shows_contacts_name(self):
        self.last(RORY, 'Rory')
        r = self.send('On my way')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(self.confirms()[-1]['detail'], '\u2753 Reply to Rory? Reply yes within 2 min.')

    def test_guess_prompt_masks_unknown_number(self):
        self.last(NOBODY, '(555) 555-0144')
        r = self.send('On my way')
        self.assertTrue(r.get('needsConfirm'), r)
        ev = self.confirms()[-1]
        self.assertEqual(ev['detail'], '\u2753 Reply to +1 \u2022\u2022\u20220144 (\u26a0\ufe0f not in Contacts)? Reply yes within 2 min.')
        self.assertNotIn('555-0144', json.dumps(ev))
        self.clock.t += 5
        r = self.send('On my way', confirmed=True, handle=NOBODY)
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][3], NOBODY)


class Probe(Base):
    FP = ('E=0.div@message-<self>{aria-label} 1.div#group@messageReplyPreview!RA+t 2.span+t | '
          'S-1(msg)=0.div@message-<uuid> | P1=0.div{data-x}!REF')

    def events(self):
        return [json.loads(l) for l in read_log().splitlines() if '"quote_probe"' in l]

    def test_probe_logged_without_text(self):
        self.clock.t += 10
        r = self.send('Ha, true', quotedText='Test: our secret plan', quoteProbe=self.FP)
        ev = self.events()[-1]
        self.assertTrue(ev['quoteSeen'])
        self.assertIn(ev['quoteResolved'], (True, False))
        self.assertIn('messageReplyPreview!RA', ev['fingerprint'])
        log = read_log()
        for s in ('Ha, true', 'secret plan'):
            self.assertNotIn(s, log)

    def test_no_quote_seen(self):
        self.send('Test: hello', quoteProbe=self.FP)
        ev = self.events()[-1]
        self.assertFalse(ev['quoteSeen'])
        self.assertFalse(ev['quoteResolved'])
        self.assertEqual(ev['routedBy'], 'prefix')

    def test_quote_resolved(self):
        self.send('cool', quotedText='Test: hi', quoteProbe=self.FP)
        ev = self.events()[-1]
        self.assertTrue(ev['quoteSeen'])
        self.assertTrue(ev['quoteResolved'])

    def test_free_text_in_probe_is_stripped(self):
        self.send('Test: hello', quoteProbe='E=0.div "Meet at 5pm at Joe\'s" 😀')
        fp = self.events()[-1]['fingerprint']
        self.assertNotIn('"', fp)
        self.assertNotIn("'", fp)

    def test_absent_probe_logs_nothing(self):
        self.send('Test: hello')
        self.assertEqual(self.events(), [])


if __name__ == '__main__':
    unittest.main()
