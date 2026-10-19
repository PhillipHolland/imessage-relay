"""v1.4.0: multi-word names, a reply to someone new (Contacts lookup + /yes), and host notifications
that no longer vanish when the webhook is slow (the needs_target notify ok:false bug)."""
import json
import os
import sqlite3
import unittest

from helpers import fresh, tmpdir, read_log, FakeClock, FakeRunner

CONV = '1111-2222'
PEOPLE = [  # (first, last, [(label, number)])
    ('Jordan', 'Rivera', [('_$!<Home>!$_', '(555) 555-0101'), ('_$!<Mobile>!$_', '(555) 555-0188')]),
    ('Sam', 'Lee', [('', '555-555-0141')]),
    ('Sam', 'Ortiz', [('iPhone', '555-555-0142')]),
    ('Mary Ann', 'Jones', [('', '555-555-0150')]),
]


def make_ab(tmp, people=PEOPLE):
    os.makedirs(os.path.join(tmp, 'ab'), exist_ok=True)
    path = os.path.join(tmp, 'ab', 'x.abcddb')
    if os.path.exists(path):
        os.unlink(path)
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZORGANIZATION TEXT);
      CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT, ZLABEL TEXT);
      CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT, ZLABEL TEXT);""")
    for i, (first, last, nums) in enumerate(people, 1):
        con.execute('INSERT INTO ZABCDRECORD VALUES (?,?,?,NULL)', (i, first, last))
        for lab, n in nums:
            con.execute('INSERT INTO ZABCDPHONENUMBER VALUES (?,?,?)', (i, n, lab))
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        make_ab(self.tmp)
        self.clock = FakeClock()
        self.posts = []
        self.post_ok = True
        self.raw = {'dryRun': False, 'relayConversationId': CONV, 'webhookUrl': 'https://example.invalid/hook',
                    'contacts': [{'name': 'Test Contact', 'handle': '15555550123'}],
                    'ownerHandle': '+15555550199', 'confirmSends': False, 'confirmGuessed': 'off'}
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.m.rc.record_inbound('+15555550123', self.clock.t - 60)
        self.watcher = self.m.watch.Watcher(db_path=os.path.join(self.tmp, 'chat.db'), poster=self.poster,
                                            now=self.clock.now, sleeper=self.clock.sleep)
        self.watcher_up = True

    def poster(self, url, payload, token='', timeout=None):
        self.posts.append((dict(payload), timeout))
        return self.post_ok

    def sleeper(self, s):
        self.clock.sleep(s)
        if self.watcher_up:                    # the watcher answers jobs once a second in real life
            self.watcher.process_jobs(self.cfg())

    def cfg(self):
        return self.m.rc.load_config()

    def send(self, text, confirmed=False, handle=None, runner=None):
        msg = {'type': 'send', 'text': text, 'conversationId': CONV}
        if confirmed:
            msg.update(confirmed=True, handle=handle)
        self.runner = runner or FakeRunner([(0, '')] * 5)
        return self.m.host.handle_request(msg, cfg=self.cfg(), runner=self.runner, sleeper=self.sleeper,
                                          now=self.clock.now, poster=self.poster)

    def osa(self):
        return [c for c in self.runner.calls if c[0] == '/usr/bin/osascript']


class MultiWordNames(Base):
    def test_full_name_of_a_contact_routes(self):
        r = self.send('Test Contact: on my way')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2], 'on my way')

    def test_name_like_detection(self):
        u = self.m.rc.unknown_name_prefix
        self.assertEqual(u('Jordan Rivera: hey')[0], 'Jordan Rivera')
        self.assertEqual(u("Mary Ann O'Neil: hi")[0], "Mary Ann O'Neil")
        for t in ('Note: buy milk', 'ok: sure', 'Update: running late', 'FYI: x', 'meet at 5:30', 'a b c d: x',
                  'Breakfast plan for today: eggs'):
            self.assertIsNone(u(t), t)

    def test_unknown_name_never_falls_to_recent(self):
        self.watcher_up = False
        r = self.send('Jordan: hey there')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])


class NewContact(Base):
    def test_unique_full_name_with_two_numbers_asks_then_yes_sends(self):
        r = self.send('Jordan Rivera: hey there')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r['reason'], 'new_contact')
        self.assertEqual(r['handle'], '+15555550188')          # the mobile number, not home
        self.assertEqual(self.osa(), [])
        ev = [p for p, _ in self.posts if p['type'] == 'needs_confirm'][-1]
        self.assertIn('Text Jordan Rivera at which?', ev['detail'])   # v1.4.12: one choice per line (masked)
        self.assertIn('\n1 \u00b7 \u2022\u2022\u20220188 mobile\n', ev['detail'])
        self.assertIn('\n2 \u00b7 \u2022\u2022\u20220101 home\n', ev['detail'])
        self.assertIn('Reply 1\u20132, or no.', ev['detail'])
        self.assertNotIn('5550188', ev['detail'])              # masked
        self.assertNotIn('hey there', json.dumps(ev))
        self.clock.t += 30
        r = self.send('Jordan Rivera: hey there', confirmed=True, handle='+15555550188')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2], 'hey there')
        self.assertEqual(self.osa()[0][3], '+15555550188')
        names = [c['name'] for c in self.cfg()['contacts']]
        self.assertIn('Jordan Rivera', names)
        log = read_log()
        self.assertNotIn('hey there', log)
        self.assertNotIn('Rivera', log)
        self.assertNotIn('5550188', log)
        # no Contacts answer left behind on disk
        self.assertFalse([n for n in os.listdir(self.m.watch.JOBS_DIR) if n.startswith('contacts')])

    def test_unique_first_name(self):
        r = self.send('Jordan: hey')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r['label'], 'Jordan Rivera')

    def test_yes_after_window_refused(self):
        self.send('Jordan: hey')
        self.clock.t += 200
        r = self.send('Jordan: hey', confirmed=True, handle='+15555550188')
        self.assertFalse(r['ok'])
        self.assertEqual(self.osa(), [])

    def test_yes_with_other_handle_refused(self):
        self.send('Jordan: hey')
        r = self.send('Jordan: hey', confirmed=True, handle='+15555550141')
        self.assertFalse(r['ok'])
        self.assertEqual(self.osa(), [])

    def test_ambiguous_lists_names_not_numbers(self):
        r = self.send('Sam: hi')
        self.assertTrue(r.get('needsTarget'), r)
        ev = [p for p, _ in self.posts if p['type'] == 'needs_target'][-1]
        self.assertIn('Sam Lee', ev['detail'])
        self.assertIn('Sam Ortiz', ev['detail'])
        self.assertNotIn('555', ev['detail'])
        self.assertEqual(self.osa(), [])

    def test_no_match_is_ordinary_text(self):
        # v1.4.2: a prefix nobody knows (relay list or Contacts) is not a name: the text goes as is
        r = self.send('Zed Quark: hi')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2], 'Zed Quark: hi')
        self.assertFalse([p for p, _ in self.posts if p['type'] in ('needs_target', 'needs_confirm')])

    def test_watcher_down(self):
        self.watcher_up = False
        r = self.send('Jordan Rivera: hi')
        self.assertTrue(r.get('needsTarget'), r)
        ev = [p for p, _ in self.posts if p['type'] == 'needs_target'][-1]
        self.assertIn('couldn\u2019t check', ev['detail'])

    def test_owner_never_offered(self):
        make_ab(self.tmp, [('Owen', 'Owner', [('mobile', '555-555-0199')])])
        r = self.send('Owen Owner: hi')
        self.assertNotIn('+15555550199', [c[3] for c in self.osa()])   # never to the owner's own number
        self.assertFalse(r.get('needsConfirm'))

    def test_dry_run_adds_nothing(self):
        self.raw['dryRun'] = True
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.send('Jordan: hey')
        r = self.send('Jordan: hey', confirmed=True, handle='+15555550188')
        self.assertTrue(r['ok'] and r['dryRun'], r)
        self.assertEqual([c['name'] for c in self.cfg()['contacts']], ['Test Contact'])

    def test_add_with_number(self):
        r = self.send('/add Jordan +1 (555) 555-0188')
        self.assertTrue(r['ok'], r)
        c = [c for c in self.cfg()['contacts'] if c['name'] == 'Jordan']
        self.assertEqual(c[0]['handle'], '+15555550188')
        self.assertEqual(self.osa(), [])
        r = self.send('Jordan: hi')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2], 'hi')

    def test_add_bad_number(self):
        r = self.send('/add Me +1 555 555 0199')
        self.assertFalse(r['ok'])
        self.assertNotIn('Me', [c['name'] for c in self.cfg()['contacts']])


class NotifyRetry(Base):
    def test_timeout_raised_and_failed_notify_is_retried_by_watcher(self):
        self.watcher_up = False
        self.post_ok = False
        self.send('Zed Quark: hi')
        ev, timeout = self.posts[-1]
        self.assertEqual(timeout, 12)
        q = os.listdir(self.m.host.NOTIFY_RETRY_DIR)
        self.assertEqual(len(q), 1)
        self.assertIn('queuedRetry', read_log())
        self.post_ok = True
        self.posts = []
        self.assertEqual(self.watcher.retry_notifications(self.cfg()), 1)
        self.assertEqual(self.posts[0][0]['id'], ev['id'])       # same id -> routine dedupes
        self.assertEqual(self.posts[0][0]['type'], 'needs_target')
        self.assertEqual(os.listdir(self.m.host.NOTIFY_RETRY_DIR), [])

    def test_stays_queued_while_failing_then_expires(self):
        self.post_ok = False
        self.m.host.notify(self.cfg(), self.poster, {'type': 'status', 'detail': 'x'})
        self.watcher.retry_notifications(self.cfg())
        self.assertEqual(len(os.listdir(self.m.host.NOTIFY_RETRY_DIR)), 1)
        d = self.m.host.NOTIFY_RETRY_DIR
        p = os.path.join(d, os.listdir(d)[0])
        with open(p) as f:
            item = json.load(f)
        item['queuedAt'] -= 700
        self.m.rc.write_json_atomic(p, item)
        self.watcher.retry_notifications(self.cfg())
        self.assertEqual(os.listdir(self.m.host.NOTIFY_RETRY_DIR), [])


class RoutineNewContact(unittest.TestCase):
    def test_detail_is_used(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
        import relay_post
        out = relay_post.format_event({'type': 'needs_confirm', 'reason': 'new_contact', 'label': 'Jordan Rivera',
                                       'detail': '\u2753 Jordan isn\u2019t on your relay list yet.', 'id': 'e1'})
        self.assertIn('Jordan isn', out['lines'][0]['text'])


if __name__ == '__main__':
    unittest.main()
