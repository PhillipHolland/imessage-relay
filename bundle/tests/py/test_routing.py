"""Reply routing in ONE relay chat: labels (shared first names), "<name>:" prefix parsing,
quote > prefix > most recent contact, reply window, ambiguity, and handle normalization."""
import unittest

from helpers import fresh, tmpdir, FakeClock

T0 = 1_800_000_000.0


class Base(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc

    def cfg(self, contacts, **over):
        raw = dict({'relayConversationId': '1111-2222', 'contacts': contacts}, **over)
        return self.rc.sanitize_config(raw)


class Labels(Base):
    def labels(self, contacts):
        return [c['label'] for c in self.cfg(contacts)['contacts']]

    def test_unique_first_names_are_the_label(self):
        self.assertEqual(self.labels([{'name': 'Alex Rivera', 'handle': '+15555550123'},
                                      {'name': 'Mike', 'handle': '+15555550124'}]), ['Alex', 'Mike'])

    def test_shared_first_name_gets_last_initial_then_digits(self):
        self.assertEqual(self.labels([{'name': 'Sam Ellis', 'handle': '+15555550123'},
                                      {'name': 'Sam Ortiz', 'handle': '+15555550124'},
                                      {'name': 'Sam Oakes', 'handle': '+15555550125'}]),
                         ['Sam E.', 'Sam O.', 'Sam 0125'])

    def test_explicit_label_wins_and_duplicates_fall_back(self):
        self.assertEqual(self.labels([{'name': 'Alex Rivera', 'handle': '+15555550123', 'label': 'Mom'},
                                      {'name': 'Alex Jordan', 'handle': '+15555550124', 'label': 'mom'},
                                      {'name': 'Mom', 'handle': '+15555550125'}]),
                         ['Mom', 'Alex J.', 'Mom 0125'])

    def test_label_is_cleaned(self):
        self.assertEqual(self.labels([{'name': 'A', 'handle': '+15555550123', 'label': 'Kay: the\nbest' + 'x' * 40}]),
                         ['Kay the best' + 'x' * 12])

    def test_name_defaults_to_handle(self):
        c = self.cfg([{'handle': 'someone@example.com'}])['contacts'][0]
        self.assertEqual((c['name'], c['label']), ('someone@example.com', 'someone@example.com'))


class Prefix(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cfg([{'name': 'Alex Rivera', 'handle': '+15555550123'},
                           {'name': 'Alex Jordan', 'handle': '+15555550124', 'label': 'KJ'},
                           {'name': 'Mike Lee', 'handle': '(555) 555-0125'}])

    def p(self, text):
        return self.rc.parse_prefix(text, self.c)

    def test_label_name_and_first_name_case_insensitive(self):
        self.assertEqual(self.p('kj: on my way'), ('+15555550124', 'on my way', 'match'))
        self.assertEqual(self.p('  MIKE :hi'), ('+15555550125', 'hi', 'match'))
        self.assertEqual(self.p('mike lee: hi'), ('+15555550125', 'hi', 'match'))
        self.assertEqual(self.p('Alex R.: hi')[0], '+15555550123')
        self.assertEqual(self.p('alex jordan: hi')[0], '+15555550124')

    def test_shared_first_name_is_ambiguous(self):
        self.assertEqual(self.p('Alex: hi')[2], 'ambiguous')

    def test_non_prefixes_are_left_alone(self):
        for t in ('Note: bring milk', '5:30 works', 'https://example.com', 'see you: later', 'hi'):
            self.assertEqual(self.p(t), (None, t, 'none'), t)

    def test_only_the_first_colon_counts_and_body_keeps_later_colons(self):
        self.assertEqual(self.p('Mike: meet at 5:30: ok?'), ('+15555550125', 'meet at 5:30: ok?', 'match'))

    def test_handle_prefix_is_normalized(self):
        self.assertEqual(self.p('+1 555-555-0125: hi')[0:2], ('+15555550125', 'hi'))
        self.assertEqual(self.p('555.555.0124: hi')[0], '+15555550124')
        self.assertEqual(self.p('+1 555-555-0150: hi')[2], 'unknown_handle', 'never texted in: refused, not ignored')


class Route(Base):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock(T0)
        self.contacts = [{'name': 'Alex Rivera', 'handle': '+15555550123'},
                         {'name': 'Alex Jordan', 'handle': '+15555550124', 'label': 'KJ'},
                         {'name': 'Mike', 'handle': 'Mike@Example.com'}]
        self.recent = []

    def inbound(self, handle, ago):
        self.rc.record_inbound(handle, T0 - ago)

    def route(self, text, quoted='', **over):
        return self.rc.route_reply(text, quoted, self.cfg(self.contacts, **over), self.rc.load_roster(), T0)

    def test_recent_contact_gets_a_bare_reply(self):
        self.inbound('+15555550124', 300)
        self.inbound('mike@example.com', 30)
        r = self.route('ok')
        self.assertEqual((r['contact']['label'], r['body'], r['by']), ('Mike', 'ok', 'recent'))

    def test_recent_file_handles_are_normalized(self):
        self.inbound('15555550124', 300)                   # chat.db style without "+"
        self.assertEqual(self.route('ok')['contact']['label'], 'KJ')
        self.inbound('MIKE@example.com', 10)
        self.assertEqual(self.route('ok')['contact']['label'], 'Mike')

    def test_reply_window(self):
        self.inbound('+15555550124', 119 * 60)
        self.assertEqual(self.route('ok')['by'], 'recent')
        self.assertTrue(self.route('ok', replyWindowMinutes=60)['needsTarget'])
        self.inbound('+15555550124', 121 * 60)
        r = self.route('ok')
        self.assertTrue(r['needsTarget'])
        self.assertIn('120 minutes', r['reason'])
        self.assertEqual(r['error'], 'Who\'s this for? Start with a name, like "Alex R.: \u2026" or "text Alex R. \u2026".')

    def test_two_contacts_close_together_is_ambiguous(self):
        self.inbound('+15555550123', 40)
        self.inbound('+15555550124', 10)
        self.assertTrue(self.route('ok')['needsTarget'])
        self.assertEqual(self.route('ok', ambiguousSeconds=0)['contact']['label'], 'KJ', '0 disables the check')
        self.assertEqual(self.route('Alex R.: ok')['contact']['label'], 'Alex R.', 'a prefix settles it')

    def test_removed_contact_in_recent_file_is_ignored(self):
        self.inbound('+15555550150', 5)
        self.assertTrue(self.route('ok')['needsTarget'])
        self.inbound('+15555550123', 600)
        self.assertEqual(self.route('ok')['contact']['label'], 'Alex R.')

    def test_prefix_beats_recent(self):
        self.inbound('mike@example.com', 5)
        r = self.route('kj: running late')
        self.assertEqual((r['contact']['label'], r['body'], r['by']), ('KJ', 'running late', 'prefix'))

    def test_ambiguous_prefix_never_falls_back_to_recent(self):
        self.inbound('+15555550123', 5)
        self.assertTrue(self.route('Alex: hi')['needsTarget'])

    def test_quote_is_top_priority(self):
        self.inbound('mike@example.com', 5)
        r = self.route('sure', quoted='KJ: dinner at 7?')
        self.assertEqual((r['contact']['label'], r['body'], r['by']), ('KJ', 'sure', 'quote'))
        r = self.route('kj: sure', quoted='KJ: dinner at 7?')
        self.assertEqual((r['contact']['label'], r['body']), ('KJ', 'sure'), 'agreeing prefix is stripped')
        self.assertTrue(self.route('Mike: sure', quoted='KJ: dinner?')['needsTarget'], 'conflict')
        self.assertTrue(self.route('Alex: sure', quoted='KJ: dinner?')['needsTarget'], 'ambiguous prefix')

    def test_quote_with_sender_account_name_and_ambiguous_quote(self):
        r = self.route('yes', quoted='SenderBot Alex R.: you up?', senderXHandle='@SenderBot')
        self.assertEqual(r['contact']['label'], 'Alex R.')
        self.inbound('mike@example.com', 5)
        # item 2a: a present but unresolvable quote asks; it must NEVER silently fall through to recent
        r = self.route('yes', quoted='Alex: hello')        # ambiguous quote -> ask, not recent
        self.assertTrue(r['needsTarget'])
        self.assertIn('more than one', r['reason'])
        r = self.route('yes', quoted='no name here')       # unresolvable quote -> ask, not recent
        self.assertTrue(r['needsTarget'])
        self.assertIn('quoted', r['reason'])

    def test_record_inbound_keeps_handles_labels_and_times_only(self):
        for i in range(3):
            self.rc.record_inbound('+1555555012%d' % i, T0 + i, label='P%d' % i)
        self.rc.record_inbound('+15555550120', T0 + 9, label='ignored, label kept')
        import json
        with open(self.rc.ROSTER_PATH) as f:
            data = json.load(f)
        self.assertEqual([(r['handle'], r['label'], r['lastInboundAt']) for r in data['entries']],
                         [('+15555550120', 'P0', T0 + 9), ('+15555550121', 'P1', T0 + 1), ('+15555550122', 'P2', T0 + 2)])
        self.assertEqual(set(data['entries'][0]), {'handle', 'label', 'lastInboundAt'})

class Roster(Base):
    """relayAll: roster people (texted in, not contacts) and read-only groups."""
    def setUp(self):
        super().setUp()
        self.c = self.cfg([{'name': 'Alex Rivera', 'handle': '+15555550123'}], relayAll=True)
        self.now = T0
        self.roster = [{'handle': '+15555550150', 'label': 'Pat', 'lastInboundAt': T0 - 3600},
                       {'handle': '+15555550151', 'label': 'Old Friend', 'lastInboundAt': T0 - 40 * 86400},
                       {'handle': '+15555550152', 'label': 'Alex', 'lastInboundAt': T0 - 7200},   # collides with the contact
                       {'handle': '+15555550153', 'label': 'Group only', 'lastInboundAt': None},
                       {'handle': 'group:chat0001', 'label': 'Family', 'lastInboundAt': T0 - 60}]

    def route(self, text, quoted='', cfg=None, now=None):
        return self.rc.route_reply(text, quoted, cfg or self.c, self.roster, now or self.now)

    def test_format_handle(self):
        self.assertEqual(self.rc.format_handle('+15555550123'), '(555) 555-0123')
        self.assertEqual(self.rc.format_handle('+445555550123'), '+445555550123')
        self.assertEqual(self.rc.format_handle('a@example.com'), 'a@example.com')

    def test_unique_labels_against_contacts_and_each_other(self):
        used = {'alex', 'sam'}
        self.assertEqual(self.rc.unique_label('Alex Jordan', '+15555550160', used, firsts=['alex']), 'Alex J.')
        self.assertEqual(self.rc.unique_label('', '+15555550160', used), '(555) 555-0160')
        self.assertEqual(self.rc.unique_label('Family', 'group:x', {'family'}, group=True), 'Family 2')
        self.assertEqual(self.rc.unique_label('', 'group:x', set(), group=True), 'Group')
        d = {e['handle']: e for e in self.rc.build_directory(self.c, self.roster)}
        self.assertEqual(d['+15555550123']['label'], 'Alex')
        self.assertNotEqual(d['+15555550152']['label'].lower(), 'alex', 'a colliding roster label is re-derived')
        self.assertEqual(d['group:chat0001']['kind'], 'group')

    def test_roster_person_by_prefix_within_roster_days(self):
        r = self.route('pat: see you soon')
        self.assertEqual((r['contact']['handle'], r['body'], r['by']), ('+15555550150', 'see you soon', 'prefix'))

    def test_roster_person_too_old_is_refused(self):
        r = self.route('Old Friend: hi')
        self.assertTrue(r['needsTarget'])
        self.assertIn('rosterDays', r['reason'])
        r = self.route('Old Friend: hi', cfg=self.cfg(self.c['contacts'], relayAll=True, rosterDays=60))
        self.assertEqual(r['contact']['handle'], '+15555550151')

    def test_roster_sends_need_relay_all(self):
        off = self.cfg(self.c['contacts'], relayAll=False)
        r = self.route('Pat: hi', cfg=off)
        self.assertIn('relayAll is off', r['reason'])
        r = self.route('just text', cfg=off, now=T0 - 3500)     # Pat was latest, but is not a target with relayAll off
        self.assertNotEqual(r.get('contact', {}).get('handle'), '+15555550150')

    def test_never_texted_handle_is_refused_not_ignored(self):
        r = self.route('+1 555 555 0170: hello')
        self.assertTrue(r['needsTarget'])
        self.assertIn('never texted', r['reason'])
        self.assertIn('(555) 555-0170', r['error'])
        r = self.route('(555) 555-0150: by number')                # a roster handle typed as a number works
        self.assertEqual(r['contact']['handle'], '+15555550150')
        r = self.route('Group only: hi')                           # rostered via a group, never relayed 1:1
        self.assertTrue(r['needsTarget'])

    def test_group_replies_are_refused_by_prefix_and_quote(self):
        for r in (self.route('Family: hi all'), self.route('hi all', quoted='Family · Pat: dinner?')):
            self.assertTrue(r['needsTarget'])
            self.assertEqual(r['reason'], "group replies aren't supported yet")
            self.assertIn("Group replies aren't supported yet", r['error'])
        # a bare reply never routes to a group (recent is contacts-only), so it just asks for a name
        r = self.route('ok')
        self.assertTrue(r['needsTarget'])
        self.assertIn('no one has written', r['reason'])

    def test_quote_of_a_roster_person(self):
        r = self.route('sounds good', quoted='Pat: lunch?')
        self.assertEqual((r['contact']['handle'], r['by']), ('+15555550150', 'quote'))

    def test_recent_roster_person_is_not_a_recent_target(self):
        # item 2c: under relay-all the recent-sender fallback is still contacts-only. A roster-only
        # person (here Pat) must be named or quoted; a bare reply never routes to them by recency.
        self.roster[-1]['lastInboundAt'] = T0 - 99999            # group not recent
        r = self.route('on my way')
        self.assertTrue(r['needsTarget'])
        self.assertEqual(r['contact']['handle'] if 'contact' in r else None, None)
        r = self.route('Pat: on my way')                         # naming them still works
        self.assertEqual((r['contact']['handle'], r['by']), ('+15555550150', 'prefix'))

    def test_roster_is_capped_and_handles_labels_only(self):
        entries = [{'handle': '+1555555%04d' % i, 'label': 'P%d' % i, 'lastInboundAt': float(i), 'text': 'secret'}
                   for i in range(self.rc.MAX_ROSTER + 5)]
        self.rc.save_roster(entries)
        got = self.rc.load_roster()
        self.assertEqual(len(got), self.rc.MAX_ROSTER)
        self.assertEqual(min(e['lastInboundAt'] for e in got), 5.0, 'oldest dropped')
        self.assertEqual(set(got[0]), {'handle', 'label', 'lastInboundAt'})


if __name__ == '__main__':
    unittest.main()
