"""relay_common: normalization, config, quiet hours, logging rules."""
import json
import os
import unittest
from datetime import datetime, timezone, timedelta

from helpers import fresh, tmpdir, read_log, ROOT


class Normalize(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())

    def test_shared_vectors_match_extension(self):
        with open(os.path.join(ROOT, 'tests', 'normalize_vectors.json')) as f:
            vectors = json.load(f)
        for raw, want in vectors:
            self.assertEqual(self.m.rc.normalize_handle(raw), want, raw)

    def test_bare_11_digit_regression_and_country_code(self):
        rc = self.m.rc
        self.assertEqual(rc.normalize_handle('15555550123'), '+15555550123')   # the v0.3 allowlist bug
        self.assertEqual(rc.normalize_handle('5555550123', '44'), '+445555550123')
        self.assertTrue(rc.valid_handle('+15555550123'))
        self.assertFalse(rc.valid_handle('+123'))
        self.assertFalse(rc.valid_handle('bob; rm -rf /'))
        self.assertEqual(rc.normalize_handle(None), '')


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.p = os.path.join(self.tmp, 'c.json')

    def write(self, obj):
        with open(self.p, 'w') as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_defaults_are_safe(self):
        c = self.m.rc.load_config(self.p)                          # missing file
        self.assertTrue(c['dryRun'])
        self.assertFalse(c['paused'])
        self.assertFalse(c['autoReopenMessages'])
        self.assertEqual(c['contacts'], [])
        self.assertEqual(c['maxSendsPerMinute'], 6)
        self.assertIsNone(c['quietHours'], 'quiet hours off by default (item 11); onboarding asks')

    def test_bad_json_stays_safe(self):
        self.write('{bad json')
        c = self.m.rc.load_config(self.p)
        self.assertTrue(c['dryRun'])
        self.assertEqual(c['contacts'], [])
        self.assertIn('_configError', c)

    def test_dry_run_parsing(self):
        for v, exp in [(False, False), (0, False), ('false', False), ('off', False), (True, True), ('maybe', True), (None, True)]:
            self.write({'dryRun': v})
            self.assertEqual(self.m.rc.load_config(self.p)['dryRun'], exp, v)

    def test_contacts_normalized_deduped_validated(self):
        self.write({'contacts': [
            {'name': 'A', 'handle': '15555550123', 'xConversationId': '1111-2222'},
            {'name': 'A again', 'handle': '+1 (555) 555-0123'},
            {'name': 'B', 'handle': 'Someone@Example.com'},
            {'name': 'C', 'handle': 'nope'}, 'junk']})
        cfg = self.m.rc.load_config(self.p)
        self.assertEqual(cfg['contacts'], [{'name': 'A', 'handle': '+15555550123', 'label': 'A'},
                                           {'name': 'B', 'handle': 'someone@example.com', 'label': 'B'}])
        self.assertEqual(cfg['relayConversationId'], '', 'legacy xConversationId is ignored, not migrated')
        self.assertEqual((cfg['replyWindowMinutes'], cfg['ambiguousSeconds'], cfg['confirmSends']), (120, 60, True))

    def test_owner_identity_sanitized(self):
        self.write({'ownerHandle': '5555550199', 'ownerXUserId': 'u 99-88-77',
                    'ownerHandles': ['(555) 555-0199', 'Owner@Example.com', 'nope']})
        c = self.m.rc.load_config(self.p)
        self.assertEqual(c['ownerXUserId'], '998877')                  # digits only
        self.assertEqual(c['ownerHandles'], ['+15555550199', 'owner@example.com'])  # ownerHandle first, aliases, invalid dropped
        self.assertEqual(self.m.rc.owner_handle_set(c), {'+15555550199', 'owner@example.com'})

    def test_clamps_and_quiet_hours_validation(self):
        self.write({'maxSendsPerMinute': 1000, 'pollSeconds': 0, 'quietHours': {'start': '25:00', 'end': '07:00'}})
        c = self.m.rc.load_config(self.p)
        self.assertEqual((c['maxSendsPerMinute'], c['pollSeconds'], c['quietHours']), (30, 2, None))


class QuietHours(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc

    def at(self, hh, mm, tz=timezone.utc):
        return datetime(2026, 10, 18, hh, mm, tzinfo=tz)

    def test_wraps_midnight(self):
        qh = {'start': '22:00', 'end': '07:00', 'tz': 'UTC'}
        for (h, m), want in {(21, 59): False, (22, 0): True, (23, 30): True, (0, 0): True, (6, 59): True, (7, 0): False, (12, 0): False}.items():
            self.assertEqual(self.rc.in_quiet_hours(qh, self.at(h, m)), want, (h, m))

    def test_same_day_window_and_disabled(self):
        qh = {'start': '13:00', 'end': '14:00', 'tz': 'UTC'}
        self.assertTrue(self.rc.in_quiet_hours(qh, self.at(13, 30)))
        self.assertFalse(self.rc.in_quiet_hours(qh, self.at(14, 0)))
        self.assertFalse(self.rc.in_quiet_hours(None, self.at(23, 0)))
        self.assertFalse(self.rc.in_quiet_hours({'start': '09:00', 'end': '09:00', 'tz': 'UTC'}, self.at(9, 0)))

    def test_named_timezone(self):
        qh = {'start': '22:00', 'end': '07:00', 'tz': 'America/Chicago'}
        # 04:00 UTC = 23:00 in Chicago (CDT, UTC-5) -> quiet; 15:00 UTC = 10:00 -> not quiet
        self.assertTrue(self.rc.in_quiet_hours(qh, self.at(4, 0)))
        self.assertFalse(self.rc.in_quiet_hours(qh, self.at(15, 0)))
        self.assertTrue(self.rc.in_quiet_hours(qh, self.at(4, 0).timestamp()))   # unix seconds also accepted


class Logging(unittest.TestCase):
    def test_bodies_refused(self):
        rc = fresh(tmpdir()).rc
        for k in ('text', 'body', 'message', 'outgoing'):
            with self.assertRaises(ValueError):
                rc.log_event('x', **{k: 'secret'})
        rc.log_event('ok', textLength=6)
        self.assertIn('"textLength": 6', read_log())



class RosterTolerance(unittest.TestCase):
    """v1.3.7: a malformed roster.json must never crash a watcher tick. load_roster accepts the normal
    object, converts a bare list, and falls back to empty otherwise, logging the problem once."""
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc

    def write(self, text):
        with open(self.rc.ROSTER_PATH, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_good_object_loads(self):
        self.write('{"version":1,"entries":[{"handle":"+15555550150","label":"Pat","lastInboundAt":1.0}]}')
        r = self.rc.load_roster()
        self.assertEqual([e['handle'] for e in r], ['+15555550150'])

    def test_bare_list_is_converted(self):
        self.write('[{"handle":"+15555550151","label":"Sam","lastInboundAt":2.0}]')
        r = self.rc.load_roster()
        self.assertEqual([e['handle'] for e in r], ['+15555550151'], 'a bare list is treated as the entries')
        self.assertIn('"event": "roster_malformed"', read_log())
        self.assertIn('"shape": "bare_list"', read_log())

    def test_bad_json_is_empty_not_a_crash(self):
        self.write('{not valid json at all')
        self.assertEqual(self.rc.load_roster(), [])        # no exception
        self.assertIn('"shape": "bad_json"', read_log())

    def test_non_object_values_are_empty(self):
        for text, shape in (('42', 'not_object'), ('"x"', 'not_object'), ('{"entries":"oops"}', 'entries_not_list')):
            self.write(text)
            self.assertEqual(self.rc.load_roster(), [], text)
        self.assertIn('"shape": "entries_not_list"', read_log())

    def test_missing_file_is_silent_and_empty(self):
        import os
        if os.path.exists(self.rc.ROSTER_PATH):
            os.remove(self.rc.ROSTER_PATH)
        self.assertEqual(self.rc.load_roster(), [])
        self.assertNotIn('roster_malformed', read_log())

    def test_logged_once_not_every_call(self):
        self.write('[{"handle":"+15555550151","lastInboundAt":2.0}]')
        for _ in range(5):
            self.rc.load_roster()
        self.assertEqual(read_log().count('roster_malformed'), 1, 'warned once, not every tick')


if __name__ == '__main__':
    unittest.main()
