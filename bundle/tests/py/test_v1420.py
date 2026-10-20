"""v1.4.20: /status, health that repeats and recovers, short /help, /snooze hold."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

from helpers import ChatDb, FakeRunner, fresh, read_log, tmpdir
from test_watch import ALLOWED, Base as WatchBase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

ALEX = '+15555550188'
SAM = '+15555550124'
OWNER = '+15550000001'
CONV = '1111-2222'


def _local(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s).timestamp()


class Parse(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc
        self.now = _local(2026, 6, 15, 10, 0)

    def test_default_hours_and_clock(self):
        early = _local(2026, 6, 15, 6, 30)
        u, err = self.rc.parse_snooze('', early)
        self.assertIsNone(err)
        self.assertEqual(u, self.rc.next_clock(7, 0, early))
        self.assertEqual(datetime.fromtimestamp(u).date(), datetime.fromtimestamp(early).date())
        self.assertEqual(self.rc.format_clock(u), '7:00 AM')
        seven = _local(2026, 6, 15, 7, 0)
        u, err = self.rc.parse_snooze('', seven)
        self.assertEqual(datetime.fromtimestamp(u).date(), datetime.fromtimestamp(seven).date() + timedelta(days=1))
        self.assertEqual(self.rc.format_clock(u), '7:00 AM')
        u, err = self.rc.parse_snooze('8h', self.now)
        self.assertEqual((u, err), (self.now + 8 * 3600, None))
        u, err = self.rc.parse_snooze('until 19:30', self.now)
        self.assertIsNone(err)
        self.assertEqual(self.rc.format_clock(u), '7:30 PM')
        u, err = self.rc.parse_snooze('7pm', self.now)
        self.assertEqual(datetime.fromtimestamp(u).hour, 19)
        u, err = self.rc.parse_snooze('until 12:00 AM', self.now)
        self.assertEqual(datetime.fromtimestamp(u).hour, 0)
        u, err = self.rc.parse_snooze('until 12:00 PM', self.now)
        self.assertEqual(datetime.fromtimestamp(u).hour, 12)
        u, err = self.rc.parse_snooze('until 7:00 AM', self.now)
        self.assertEqual(self.rc.format_clock(u), '7:00 AM')
        self.assertGreater(u, self.now)

    def test_bad_args_and_cap(self):
        for bad in ('0h', '25h', 'tomorrow', 'until 25:00', 'until 7:99'):
            u, err = self.rc.parse_snooze(bad, self.now)
            self.assertIsNone(u, bad)
            self.assertEqual(err, self.rc.SNOOZE_USAGE)
        now = 1_700_000_000.0
        self.assertTrue(self.rc.snoozing({'snoozeUntil': now + 36 * 3600}, now))
        self.assertFalse(self.rc.snoozing({'snoozeUntil': now + 36 * 3600 + 1}, now))
        self.assertFalse(self.rc.snoozing({'snoozeUntil': now - 1}, now))
        self.assertFalse(self.rc.holding_inbound({'quietHours': None, 'snoozeUntil': 0}, now))
        self.assertEqual(self.rc.sanitize_config({'snoozeUntil': now - 100})['snoozeUntil'], now - 100)
        self.assertEqual(self.rc.sanitize_config({'snoozeUntil': 'nope'})['snoozeUntil'], 0)
        self.assertEqual(self.rc.sanitize_config({'snoozeUntil': -3})['snoozeUntil'], 0)
        qh = self.rc.sanitize_config({'quietHours': {'start': '00:00', 'end': '23:59', 'tz': 'UTC'}, 'snoozeUntil': 0})
        noon = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertTrue(self.rc.holding_inbound(qh, noon))


class Host(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.h, self.rc = self.m.host, self.m.rc
        self.cfgd = {
            'contacts': [{'name': 'Alex', 'handle': ALEX}, {'name': 'Sam', 'handle': SAM}],
            'relayConversationId': CONV, 'webhookUrl': 'https://x.invalid/h', 'webhookToken': 't',
            'ownerHandle': OWNER, 'ownerHandles': [OWNER], 'quietHours': None, 'dryRun': False,
        }
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, self.cfgd)
        self.posts = []
        self.runner = FakeRunner([(0, '')] * 20)
        self.t = _local(2026, 6, 15, 22, 15)

    def send(self, text, t=None):
        if t is not None:
            self.t = t
        return self.h.handle_request(
            {'type': 'send', 'text': text, 'conversationId': CONV, 'ts': ''},
            cfg=self.rc.load_config(), runner=self.runner, sleeper=lambda x: None, now=lambda: self.t,
            poster=lambda u, p, token='', timeout=4: self.posts.append(p) or True)

    def osa(self):
        return [c for c in self.runner.calls if c[0] == '/usr/bin/osascript']

    def write_status(self, **over):
        st = {
            'lastTickAt': self.rc.iso_utc(self.t - 4), 'chatDb': 'ok', 'messagesRunning': True,
            'lastForwardAt': self.rc.iso_utc(self.t - 120),
        }
        st.update(over)
        if over.get('lastForwardAt', 1) is None:
            st.pop('lastForwardAt', None)
        self.rc.write_json_atomic(os.path.join(self.rc.APP_DIR, 'status.json'), st)

    def test_parser_rejects_testing(self):
        p = self.h.parse_owner_command
        self.assertIsNone(p('/testing 123'))
        self.assertEqual(p('/SNOOZE'), {'cmd': 'snooze', 'rest': ''})
        self.assertEqual(p('/snooze until 7:00 AM'), {'cmd': 'snooze', 'rest': 'until 7:00 AM'})
        self.assertEqual(p('/status'), {'cmd': 'status'})

    def test_status_line_has_no_message_text(self):
        self.write_status()
        self.rc.record_inbound(ALEX, self.t - 30, label='Alex')
        n = len(self.osa())
        r = self.send('/status')
        self.assertEqual(r.get('command'), 'status')
        self.assertEqual(len(self.osa()), n)
        detail = self.posts[-1]['detail']
        self.assertEqual(
            detail,
            'On \u00b7 v1.4.28 \u00b7 watcher 4s \u00b7 Messages open \u00b7 last text 2m \u00b7 replies to Alex')
        self.assertNotIn('text', self.posts[-1])
        self.assertNotIn('bare reply', read_log())
        self.assertIn('status_cmd', read_log())

    def test_status_variants_and_paused(self):
        self.write_status(chatDb='unavailable')
        self.send('/status')
        self.assertIn('watcher can\u2019t read', self.posts[-1]['detail'])
        self.assertNotIn('4s ago', self.posts[-1]['detail'])
        os.unlink(os.path.join(self.rc.APP_DIR, 'status.json'))
        self.send('/status')
        d = self.posts[-1]['detail']
        self.assertIn('watcher not seen', d)
        self.assertIn('Messages not checked', d)
        self.assertIn('no texts yet', d)
        self.assertIn('name someone', d)
        self.assertNotIn('bare reply', d)
        self.write_status(messagesRunning=False, lastForwardAt=self.rc.iso_utc(self.t - 10))
        self.send('/status')
        self.assertIn('Messages closed', self.posts[-1]['detail'])
        self.assertIn('last text just now', self.posts[-1]['detail'])
        self.rc.record_inbound(ALEX, self.t - 20, label='Alex')
        self.rc.record_inbound(SAM, self.t - 40, label='Sam')
        self.write_status()
        self.send('/status')
        self.assertIn('replies unclear', self.posts[-1]['detail'])
        self.send('/pause')
        r = self.send('/status')
        self.assertEqual(r.get('command'), 'status')
        self.assertTrue(self.posts[-1]['detail'].startswith('Paused \u00b7 v1.4.28 \u00b7'))
        self.assertEqual(self.send('ok').get('error'), 'paused')
        self.assertEqual(self.osa(), [])

    def test_help_and_pause_wording(self):
        self.assertEqual(self.h.HELP_TEXT, (
            '/status \u00b7 /who\n'
            '/pause \u00b7 /resume \u00b7 /snooze\n'
            '/block name \u00b7 /unblock name\n'
            '/add name\n'
            'name: your message\n'
            '/test \u00b7 /help'))
        n = len(self.osa())
        self.send('/help')
        self.assertEqual(self.posts[-1]['detail'], self.h.HELP_TEXT)
        self.assertIn('\n', self.posts[-1]['detail'])
        self.send('/pause')
        self.assertEqual(self.posts[-1]['detail'], self.h.PAUSE_DETAIL)
        self.assertIn('not kept', self.posts[-1]['detail'])
        self.assertIn('/snooze', self.posts[-1]['detail'])
        self.assertEqual(len(self.osa()), n)
        self.assertTrue(self.rc.load_config()['paused'])

    def test_snooze_holds_config_and_resume_clears_it(self):
        n = len(self.osa())
        r = self.send('/snooze')
        self.assertTrue(r.get('ok'))
        self.assertEqual(r.get('command'), 'snooze')
        self.assertEqual(len(self.osa()), n)
        cfg = self.rc.load_config()
        want = int(round(self.rc.next_clock(7, 0, self.t)))
        self.assertFalse(cfg['paused'])
        self.assertEqual(cfg['snoozeUntil'], want)
        self.assertEqual(self.rc.format_clock(want), '7:00 AM')
        detail = 'Snoozing until 7:00 AM. Texts arrive as one digest. /resume'
        self.assertEqual(self.posts[-1]['detail'], detail)
        self.assertNotIn('text', self.posts[-1])
        log = read_log()
        self.assertIn('"event": "snoozed"', log)
        self.assertIn('"seconds":', log)
        self.assertNotIn('Tonight', log)
        self.send('/resume')
        self.assertEqual(self.posts[-1]['detail'], 'Relaying is back on.')
        self.assertNotIn('snoozeUntil', self.rc.read_raw_config())
        self.assertFalse(self.rc.load_config()['paused'])

    def test_snooze_8h_until_bad_and_pause_clears(self):
        self.send('/snooze 8h')
        stored = self.rc.load_config()['snoozeUntil']
        self.assertEqual(stored, int(round(self.t + 8 * 3600)))
        self.assertTrue(self.posts[-1]['detail'].startswith('Snoozing until %s.' % self.rc.format_clock(stored)))
        self.assertIn('Texts arrive as one digest. /resume', self.posts[-1]['detail'])
        before = json.dumps(self.rc.read_raw_config(), sort_keys=True)
        r = self.send('/snooze tomorrow')
        self.assertFalse(r.get('ok'))
        self.assertEqual(self.posts[-1]['detail'], self.rc.SNOOZE_USAGE)
        self.assertEqual(json.dumps(self.rc.read_raw_config(), sort_keys=True), before)
        self.send('/pause')
        self.assertTrue(self.rc.load_config()['paused'])
        self.assertNotIn('snoozeUntil', self.rc.read_raw_config())
        self.assertEqual(self.posts[-1]['detail'], self.h.PAUSE_DETAIL)

    def test_snooze_until_and_dry_run_still_writes(self):
        morning = _local(2026, 6, 15, 10, 0)
        self.send('/snooze until 7:00 AM', t=morning)
        self.assertEqual(self.rc.format_clock(self.rc.load_config()['snoozeUntil']), '7:00 AM')
        self.assertGreater(self.rc.load_config()['snoozeUntil'] - morning, 12 * 3600)
        raw = self.rc.read_raw_config()
        raw['dryRun'] = True
        raw['paused'] = True
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, raw)
        self.send('/snooze 8h')
        got = self.rc.read_raw_config()
        self.assertFalse(got['paused'])
        self.assertTrue(got['dryRun'])
        self.assertGreater(got['snoozeUntil'], self.t)
        self.assertEqual(self.osa(), [])

    def test_snoozing_status_and_relayctl_clears(self):
        self.send('/snooze')
        self.write_status()
        self.rc.record_inbound(ALEX, self.t - 30, label='Alex')
        self.send('/status')
        self.assertTrue(self.posts[-1]['detail'].startswith(
            'Snoozing until 7:00 AM \u00b7 v1.4.28 \u00b7 watcher 4s \u00b7'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.ctl.main(['pause']), 0)
        raw = self.rc.read_raw_config()
        self.assertTrue(raw['paused'])
        self.assertNotIn('snoozeUntil', raw)
        raw['paused'] = False
        raw['snoozeUntil'] = int(self.t + 3600)
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, raw)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.ctl.main(['resume']), 0)
        raw = self.rc.read_raw_config()
        self.assertFalse(raw['paused'])
        self.assertNotIn('snoozeUntil', raw)


class SnoozeHold(WatchBase):
    def test_snooze_holds_then_digests(self):
        self.w.tick()
        self.cfg['snoozeUntil'] = self.now + 3600
        a = self.db.add(ALLOWED, text='held-one')
        self.db.add(ALLOWED, text='held-two')
        c = self.w.tick()
        self.assertEqual((self.poster.calls, c['held']), ([], 2))
        self.assertEqual(self.state()['lastRowid'], a - 1)
        self.assert_no_bodies_on_disk('held-one', 'held-two')
        self.assertNotIn('held-one', read_log())
        st = self.m.rc.read_json(self.m.watch.STATUS_PATH)
        self.assertTrue(st['snooze'])
        self.assertFalse(st['quiet'])
        self.now = self.cfg['snoozeUntil'] + 5
        self.db.add(ALLOWED, text='after-snooze')
        self.w.tick()
        self.assertEqual(self.poster.types(), ['digest', 'message'])
        d = self.poster.calls[0][1]
        self.assertEqual((d['count'], [x['text'] for x in d['messages']]), (2, ['held-one', 'held-two']))
        self.assertEqual(self.poster.calls[1][1]['text'], 'after-snooze')
        self.assertFalse(self.m.rc.read_json(self.m.watch.STATUS_PATH)['snooze'])
        self.assert_no_bodies_on_disk('held-one', 'held-two', 'after-snooze')

    def test_pause_drops_a_snooze_hold(self):
        self.w.tick()
        self.cfg['snoozeUntil'] = self.now + 3600
        self.db.add(ALLOWED, text='held-one')
        self.w.tick()
        self.cfg['paused'] = True
        self.cfg['snoozeUntil'] = 0
        self.w.tick()
        self.cfg['paused'] = False
        self.now += 7200
        self.w.tick()
        self.assertEqual(self.poster.calls, [])
        self.assert_no_bodies_on_disk('held-one')

    def test_overlap_waits_for_both(self):
        self.w.tick()
        self.now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc).timestamp()
        self.cfg['quietHours'] = {'start': '00:00', 'end': '06:00', 'tz': 'UTC'}
        self.cfg['snoozeUntil'] = self.now + 8 * 3600
        self.db.add(ALLOWED, text='held-one')
        self.w.tick()
        self.now = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc).timestamp()
        self.w.tick()
        self.assertEqual(self.poster.calls, [])
        self.now = self.cfg['snoozeUntil'] + 5
        self.w.tick()
        self.assertEqual(self.poster.types(), ['digest'])
        self.assert_no_bodies_on_disk('held-one')

    def test_snooze_ending_inside_quiet_hours_stays_held(self):
        self.w.tick()
        self.now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc).timestamp()
        self.cfg['quietHours'] = {'start': '00:00', 'end': '06:00', 'tz': 'UTC'}
        self.cfg['snoozeUntil'] = self.now + 3600
        self.db.add(ALLOWED, text='held-one')
        self.w.tick()
        self.now = datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc).timestamp()
        self.w.tick()
        self.assertEqual(self.poster.calls, [])
        self.now = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc).timestamp()
        self.w.tick()
        self.assertEqual(self.poster.types(), ['digest'])

    def test_far_future_snooze_does_not_hold(self):
        self.w.tick()
        self.cfg['snoozeUntil'] = self.now + 40 * 3600
        self.db.add(ALLOWED, text='held-one')
        self.w.tick()
        self.assertEqual(self.poster.types(), ['message'])
        self.assertNotIn('held-one', read_log())
        self.assert_no_bodies_on_disk('held-one')


class Health(WatchBase):
    def statuses(self):
        return [p for _, p, _ in self.poster.calls if p.get('type') == 'status']

    def seen(self, at):
        self.m.rc.write_json_atomic(self.m.watch.EXT_SEEN_PATH, {'extVersion': '1.4.16', 'at': at})

    def test_fda_repeats_then_recovers(self):
        self.w.tick()
        t0 = self.now
        os.unlink(self.db.path)
        self.w.tick()
        self.w.tick()
        fda = [p for p in self.statuses() if p.get('status') == 'fda_missing']
        self.assertEqual(len(fda), 1)
        self.assertEqual(fda[0]['detail'], self.m.watch.FDA_DETAIL)
        self.assertNotIn('text', fda[0])
        self.now = t0 + 29 * 60
        self.w.tick()
        self.assertEqual(len([p for p in self.statuses() if p.get('status') == 'fda_missing']), 1)
        self.now = t0 + 30 * 60
        self.w.tick()
        self.assertEqual(len([p for p in self.statuses() if p.get('status') == 'fda_missing']), 2)
        self.now = t0 + 30 * 60 + 3600
        self.w.tick()
        self.assertEqual(len([p for p in self.statuses() if p.get('status') == 'fda_missing']), 3)
        ChatDb(self.db.path)
        self.w.tick()
        self.assertEqual([p['detail'] for p in self.statuses() if p.get('status') == 'recovered'],
                         ['Watching Messages again.'])
        self.assertFalse(self.w.fda_warned)
        self.w.tick()
        self.assertEqual(len([p for p in self.statuses() if p.get('status') == 'recovered']), 1)

    def test_busy_is_not_fda_or_recovery(self):
        os.unlink(self.db.path)
        self.w.tick()
        self.assertTrue(self.w.fda_warned)
        ChatDb(self.db.path)
        lk = sqlite3.connect(self.db.path, timeout=0)
        lk.execute('PRAGMA locking_mode=EXCLUSIVE')
        lk.execute('BEGIN EXCLUSIVE')
        try:
            counts = self.w.tick()
        finally:
            lk.rollback()
            lk.close()
        self.assertGreaterEqual(counts.get('dbBusy', 0), 1)
        self.assertTrue(self.w.fda_warned)
        self.assertNotIn('recovered', [p.get('status') for p in self.statuses()])
        self.assertEqual(len([p for p in self.statuses() if p.get('status') == 'fda_missing']), 1)
        self.w.tick()
        self.assertEqual([p['detail'] for p in self.statuses() if p.get('status') == 'recovered'],
                         ['Watching Messages again.'])

    def test_paused_posts_no_fda(self):
        self.cfg['paused'] = True
        os.unlink(self.db.path)
        self.w.tick()
        self.now += 4000
        self.w.tick()
        self.assertEqual(self.statuses(), [])
        self.assertFalse(self.w.fda_warned)

    def test_failed_fda_post_does_not_advance(self):
        os.unlink(self.db.path)
        self.poster.ok = False
        self.w.tick()
        self.assertFalse(self.w.fda_warned)
        self.assertEqual(self.w.fda_phase, 0)
        self.poster.ok = True
        self.w.tick()
        self.assertEqual(self.w.fda_phase, 1)
        self.assertTrue(self.w.fda_warned)

    def test_chrome_arms_warns_and_recovers(self):
        self.seen(0)
        self.now += 7200
        self.w.tick()
        self.assertEqual(self.statuses(), [])
        self.seen(self.now - 30)
        self.w.tick()
        self.assertEqual(self.statuses(), [])
        self.seen(self.now - self.m.watch.CHROME_STALE_S - 5)
        self.w.tick()
        self.assertEqual(self.statuses(), [])
        self.now = self.w.chrome_arm + self.m.watch.CHROME_ARM_S - 1
        self.seen(self.now - self.m.watch.CHROME_STALE_S - 5)
        self.w.tick()
        self.assertEqual(self.statuses(), [])
        self.now = self.w.chrome_arm + self.m.watch.CHROME_ARM_S
        self.seen(self.now - self.m.watch.CHROME_STALE_S - 5)
        self.w.tick()
        self.assertEqual([p['status'] for p in self.statuses()], ['chrome_away'])
        self.assertEqual(self.statuses()[0]['detail'], self.m.watch.CHROME_AWAY)
        warned = self.now
        self.now = warned + 29 * 60
        self.seen(self.now - self.m.watch.CHROME_STALE_S - 5)
        self.w.tick()
        self.assertEqual(len(self.statuses()), 1)
        self.now = warned + 30 * 60
        self.seen(self.now - self.m.watch.CHROME_STALE_S - 5)
        self.w.tick()
        self.assertEqual(len([p for p in self.statuses() if p['status'] == 'chrome_away']), 2)
        self.seen(self.now)
        self.w.tick()
        self.assertEqual(self.statuses()[-1]['detail'], self.m.watch.CHROME_BACK)
        n = len(self.statuses())
        self.w.tick()
        self.assertEqual(len(self.statuses()), n)
        self.assertFalse(self.w.chrome_warned)

    def test_chrome_opening_during_the_arm_stays_quiet(self):
        self.seen(self.now - self.m.watch.CHROME_STALE_S - 5)
        self.w.tick()
        self.seen(self.now)
        self.now += self.m.watch.CHROME_ARM_S + 50
        self.seen(self.now - 20)
        self.w.tick()
        self.assertEqual(self.statuses(), [])

    def test_paused_skips_a_new_chrome_warning(self):
        self.cfg['paused'] = True
        self.seen(self.now - 99999)
        self.w.tick()
        self.now += 3600
        self.w.tick()
        self.assertEqual(self.statuses(), [])
        self.assertFalse(self.w.chrome_warned)


class Routine(unittest.TestCase):
    def line(self, payload):
        ev = rp.format_event(payload)
        self.assertEqual(set(ev['lines'][0]), {'id', 'text', 'body'})
        return ev['lines'][0]['text']

    def _post(self, payload):
        class P:
            returncode, stderr = 0, b''
            stdout = json.dumps({'message_id': 'm', 'encoded_message_create_event': 'E',
                                 'encoded_message_event_signature': 'S'}).encode()

        def runner(argv, input=b''):
            return P()

        d = tmpdir()
        # A named helper skips the on-disk xchat_lite search; the fake runner never executes it.
        return rp.process({'payload': payload, 'conversation_id': 'C',
                           'xchat': {'events': [], 'conversation_key_events': ['K'], 'signing_keys': ['S']},
                           'helper': {'python': sys.executable, 'script': os.path.join(d, 'xchat_lite.py')},
                           'dedupe_path': os.path.join(d, 's.json')}, runner=runner)

    def test_wording_and_notify(self):
        m = fresh(tmpdir())
        self.assertEqual(m.watch.FDA_DETAIL, rp.FDA_LINE)
        self.assertEqual(m.watch.RECOVERED_DETAIL, 'Watching Messages again.')
        self.assertEqual(m.watch.CHROME_BACK, 'Chrome is open again. Replies will send.')
        self.assertEqual(
            m.watch.CHROME_AWAY,
            'Chrome hasn\u2019t checked in, so replies won\u2019t send. Open Chrome on the Mac.')
        self.assertEqual(self.line({'type': 'status', 'status': 'fda_missing', 'id': 'a'}), rp.FDA_LINE)
        custom = '\u26a0\ufe0f Texts stopped: custom.'
        self.assertEqual(self.line({'type': 'status', 'status': 'fda_missing', 'detail': custom, 'id': 'b'}), custom)
        self.assertEqual(self.line({'type': 'status', 'status': 'fda_missing', 'detail': 'plain prose', 'id': 'c'}),
                         rp.FDA_LINE)
        self.assertEqual(self.line({'type': 'status', 'status': 'recovered', 'detail': 'Watching Messages again.', 'id': 'd'}),
                         'Watching Messages again.')
        away = self.line({'type': 'status', 'status': 'chrome_away', 'detail': m.watch.CHROME_AWAY, 'id': 'e'})
        self.assertEqual(away, m.watch.CHROME_AWAY)
        self.assertNotIn('\u26a0\ufe0f\u26a0', away)
        self.assertEqual(self.line({'type': 'status', 'status': 'chrome_back', 'detail': m.watch.CHROME_BACK, 'id': 'f'}),
                         'Chrome is open again. Replies will send.')
        self.assertEqual(self.line({'type': 'status', 'detail': m.host.HELP_TEXT, 'id': 'h'}),
                         m.host.HELP_TEXT)
        self.assertGreaterEqual(m.host.HELP_TEXT.count('\n'), 5)
        self.assertEqual(self.line({'type': 'status', 'status': 'paused', 'detail': m.host.PAUSE_DETAIL, 'id': 'p'}),
                         m.host.PAUSE_DETAIL)
        snooze = 'Snoozing until 7:00 AM. Texts arrive as one digest. /resume'
        self.assertEqual(self.line({'type': 'status', 'status': 'snoozing', 'detail': snooze, 'id': 's'}),
                         snooze)
        fda = self._post({'type': 'status', 'status': 'fda_missing', 'id': 'fda1'})
        self.assertTrue(fda['sends'][0]['should_notify'])
        self.assertIsNone(fda['sends'][0]['reply_to'])
        self.assertFalse(self._post({'type': 'status', 'status': 'paused', 'id': 'p1',
                                   'detail': m.host.PAUSE_DETAIL})['sends'][0]['should_notify'])
        self.assertTrue(self._post({'type': 'status', 'status': 'recovered', 'id': 'r1',
                                  'detail': 'Watching Messages again.'})['sends'][0]['should_notify'])
        self.assertTrue(self._post({'type': 'status', 'status': 'chrome_away', 'id': 'c1',
                                  'detail': m.watch.CHROME_AWAY})['sends'][0]['should_notify'])
        self.assertTrue(self._post({'type': 'status', 'status': 'chrome_back', 'id': 'c2',
                                  'detail': m.watch.CHROME_BACK})['sends'][0]['should_notify'])
        self.assertFalse(self._post({'type': 'status', 'status': 'status', 'id': 'st1',
                                   'detail': 'On \u00b7 v1.4.20'})['sends'][0]['should_notify'])


if __name__ == '__main__':
    unittest.main()
