"""Inbound watcher against a temp sqlite db shaped like chat.db."""
import json
import os
import sqlite3
import time
import unittest
from datetime import datetime, timezone

from helpers import fresh, tmpdir, read_log, FakeRunner, ChatDb, attributed_body

ALLOWED = '+15555550123'
OTHER = '+15555550188'
OWNER = '+15555550199'


class Poster:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, url, payload, token=''):
        self.calls.append((url, json.loads(json.dumps(payload)), token))
        return self.ok

    def types(self):
        return [p['type'] for _, p, _ in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.db.add(ALLOWED, text='old history before install')       # must never be forwarded
        self.cfg = {'contacts': [{'name': 'Test Contact', 'handle': '15555550123'}], 'relayConversationId': '1111-2222',
                    'webhookUrl': 'https://example.invalid/hook', 'webhookToken': 'tok', 'ownerHandle': OWNER,
                    'quietHours': None, 'batchWebhooks': False}   # one post per text here; batching: test_v145
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()   # fixed base so tests are deterministic (no wall clock)
        self.poster = Poster()
        self.runner = FakeRunner()
        self.w = self.new_watcher()

    def new_watcher(self):
        return self.m.watch.Watcher(poster=self.poster, runner=self.runner, now=lambda: self.now,
                                    config_loader=lambda: self.m.rc.sanitize_config(self.cfg), sleeper=lambda s: None)

    def state(self):
        return self.m.rc.read_json(self.m.watch.STATE_PATH)

    def assert_no_bodies_on_disk(self, *bodies):
        for root, _, files in os.walk(self.tmp):
            for f in files:
                p = os.path.join(root, f)
                if p == self.db.path:
                    continue
                with open(p, 'rb') as fh:
                    data = fh.read()
                for b in bodies:
                    self.assertNotIn(b.encode(), data, '%s found in %s' % (b, p))


class Forwarding(Base):
    def test_first_run_baselines_then_forwards_new_allowlisted_only(self):
        c = self.w.tick()
        self.assertEqual(self.poster.calls, [], 'history is never replayed')
        self.assertEqual(self.state()['lastRowid'], 1)
        r2 = self.db.add(ALLOWED, text='hello from contact')
        self.db.add(OTHER, text='not opted in')
        self.db.add(ALLOWED, text='my own message', from_me=1)
        c = self.w.tick()
        self.assertEqual(len(self.poster.calls), 1)
        url, p, token = self.poster.calls[0]
        self.assertEqual((url, token), ('https://example.invalid/hook', 'tok'))
        self.assertEqual(p['type'], 'message')
        self.assertEqual({k: p[k] for k in ('contactName', 'label', 'handle', 'text')},
                         {'contactName': 'Test Contact', 'label': 'Test', 'handle': ALLOWED, 'text': 'hello from contact'})
        self.assertTrue(p['untrusted'])
        datetime.strptime(p['receivedAt'], '%Y-%m-%dT%H:%M:%SZ')
        self.assertEqual((c['forwarded'], c['notAllowlisted']), (1, 1))
        self.assertEqual(self.state()['lastRowid'], r2 + 2)
        self.assert_no_bodies_on_disk('hello from contact', 'not opted in', 'my own message')
        self.assertNotIn('hello', read_log())

    def test_non_allowlisted_text_is_never_selected(self):
        self.w.tick()
        self.db.add(OTHER, text='private stuff')
        self.db.add(ALLOWED, text='ok to read')
        seen = []
        real = sqlite3.connect

        def tracing(*a, **k):
            con = real(*a, **k)
            con.set_trace_callback(seen.append)
            return con
        sqlite3.connect = tracing
        try:
            self.w.tick()
        finally:
            sqlite3.connect = real
        body_queries = [q for q in seen if q.startswith('SELECT text')]
        self.assertEqual(len(body_queries), 1)
        self.assertIn('WHERE ROWID = 3', body_queries[0], 'only the allowlisted row (ROWID 3) has its text read')

    def test_attributed_body_reactions_groups_attachments(self):
        self.w.tick()
        self.db.add(ALLOWED, text=None, ab=attributed_body('from the blob 👋'))
        self.db.add(ALLOWED, text='Loved “hi”', assoc=2000)                 # tapback -> text line (item 5)
        self.db.add(ALLOWED, text='group hello', group=True)               # group chat (relayAll off -> skip)
        self.db.add(ALLOWED, text=None, attach={'mime': 'image/jpeg'})     # photo only -> placeholder (item 4)
        self.db.add(ALLOWED, text=None, ab=None)                           # nothing readable
        c = self.w.tick()
        self.assertEqual([p['text'] for _, p, _ in self.poster.calls],
                         ['from the blob 👋', '❤️ a message', '📷 Photo'])
        # a tapback is no longer a skipped "reaction"; the unreadable row is deferred (item 3), not dropped
        self.assertEqual((c['reactionsOrEvents'], c['group'], c['deferredEmpty'], c['empty']), (0, 1, 1, 0))
        # the tapback row never forwards the reacted message's own text
        self.assertNotIn('Loved', json.dumps([p for _, p, _ in self.poster.calls]))
        self.now += self.m.watch.EMPTY_GRACE_S + 5
        c2 = self.w.tick()
        self.assertEqual(c2['empty'], 1, 'after the grace window it is finally skipped as empty')
        self.assertEqual(len(self.poster.calls), 3, 'the unreadable row is never relayed')

    def test_handle_formats_in_chat_db_are_normalized(self):
        self.w.tick()
        self.db.add('15555550123', text='bare digits handle')               # chat.db rarely, but possibly
        self.w.tick()
        self.assertEqual(self.poster.calls[0][1]['handle'], ALLOWED)


class RelayAll(Base):
    """relayAll: X Chat as the whole iMessage inbox."""
    def setUp(self):
        super().setUp()
        self.cfg['relayAll'] = True
        ab = os.path.join(self.tmp, 'ab')
        os.makedirs(ab)
        con = sqlite3.connect(os.path.join(ab, 'AddressBook-v22.abcddb'))
        con.executescript("""CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZORGANIZATION TEXT);
          CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
          CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
          INSERT INTO ZABCDRECORD VALUES (1, 'Sam', 'Example', NULL);
          INSERT INTO ZABCDRECORD VALUES (2, 'Sam', 'Other', NULL);
          INSERT INTO ZABCDRECORD VALUES (3, 'Test', 'Person', NULL);
          INSERT INTO ZABCDPHONENUMBER VALUES (1, '(555) 555-0188');
          INSERT INTO ZABCDPHONENUMBER VALUES (2, '555-555-0178');
          INSERT INTO ZABCDPHONENUMBER VALUES (3, '555-555-0179');""")
        con.commit()
        con.close()
        self.w.tick()

    def sent(self):
        return [p for _, p, _ in self.poster.calls if p['type'] == 'message']

    def roster(self):
        return {r['handle']: r for r in self.m.rc.load_roster()}

    def test_every_1to1_sender_with_contacts_db_labels_made_unique(self):
        self.db.add(OTHER, text='hi from sam')
        self.db.add('+15555550178', text='hi from the other sam')
        self.db.add('+15555550179', text='label clashes with the contact "Test"')
        self.db.add('+15555550140', text='unknown number')
        self.db.add('someone@example.com', text='unknown email')
        self.db.add(ALLOWED, text='contact as before')
        self.w.tick()
        got = [(p['label'], p['handle'], p['contactName']) for p in self.sent()]
        self.assertEqual(got, [('Sam', OTHER, 'Sam Example'), ('Sam O.', '+15555550178', 'Sam Other'),
                               ('Test P.', '+15555550179', 'Test Person'),
                               ('(555) 555-0140', '+15555550140', '(555) 555-0140'),
                               ('someone@example.com', 'someone@example.com', 'someone@example.com'),
                               ('Test', ALLOWED, 'Test Contact')])
        r = self.roster()
        self.assertEqual(r[OTHER]['label'], 'Sam')
        self.assertEqual(r[OTHER]['lastInboundAt'], self.now)
        self.assertEqual(set(r[OTHER]), {'handle', 'label', 'lastInboundAt'}, 'handles and labels only')
        self.assert_no_bodies_on_disk('hi from sam', 'unknown number', 'unknown email', 'contact as before')
        st = self.m.rc.read_json(self.m.watch.STATUS_PATH)
        self.assertTrue(st['relayAll'])

    def test_labels_are_stable_across_messages_and_restarts(self):
        self.db.add(OTHER, text='one')
        self.w.tick()
        self.w = self.new_watcher()
        self.db.add(OTHER, text='two')
        self.w.tick()
        self.assertEqual([p['label'] for p in self.sent()], ['Sam', 'Sam'])

    def test_group_chats_are_forwarded_with_group_and_sender_labels(self):
        self.db.add(OTHER, text='group hi', group='fam', display_name='Family', members=(ALLOWED,))
        self.db.add(ALLOWED, text='group reply', group='fam', display_name='Family', members=(OTHER,))
        self.db.add('+15555550140', text='unnamed group', group='g2', members=(OTHER,))
        self.db.add('+15555550141', text='by member count', group='g3', members=(OTHER, ALLOWED), style=45)
        c = self.w.tick()
        got = [(p['group']['label'], p['label'], p['text']) for p in self.sent()]
        self.assertEqual(got, [('Family', 'Sam', 'group hi'), ('Family', 'Test', 'group reply'),
                               ('Group', '(555) 555-0140', 'unnamed group'), ('Group 2', '(555) 555-0141', 'by member count')])
        self.assertEqual(c['groupMessages'], 4)
        r = self.roster()
        groups = sorted(v['label'] for k, v in r.items() if k.startswith('group:'))
        self.assertEqual(groups, ['Family', 'Group', 'Group 2'])
        self.assertIsNone(r[OTHER]['lastInboundAt'], 'group-only sender: not a 1:1 reply target')
        p0 = self.sent()[0]
        self.assertEqual((p0['group'], p0['handle']), ({'label': 'Family', 'name': 'Family'}, OTHER))
        self.assertNotIn('route', p0, 'internal routing key is not posted')
        self.assertNotIn('group', self.sent()[2]['group']['name'] or 'unnamed', 'no chat identifiers posted')

    def test_group_skipped_when_relay_all_off(self):
        self.cfg['relayAll'] = False
        self.db.add(ALLOWED, text='contact in a group', group='fam', display_name='Family', members=(OTHER,))
        self.db.add(OTHER, text='stranger 1:1')
        c = self.w.tick()
        self.assertEqual(self.sent(), [])
        self.assertEqual((c['group'], c['notAllowlisted']), (1, 1))
        self.assertEqual(self.roster(), {}, 'nothing rostered when nothing is relayed')

    def test_owner_from_me_mute_and_short_codes(self):
        self.cfg['mute'] = ['555-555-0188', 'noisy@example.com']
        self.db.add(OWNER, text='note to self')
        self.db.add(OTHER, text='muted text')
        self.db.add('noisy@example.com', text='muted email')
        self.db.add('+15555550140', text='mine', from_me=1)
        self.db.add('12345', text='Your code is 000000')
        self.db.add('888222', text='six digit short code')
        self.db.add('+15555550140', text='kept')
        c = self.w.tick()
        self.assertEqual([p['text'] for p in self.sent()], ['kept'])
        self.assertEqual((c['own'], c['muted'], c['shortCode']), (1, 2, 2))
        self.assertNotIn(OTHER, self.roster())
        self.cfg['skipShortCodes'] = False
        self.db.add('12345', text='code now relayed')
        self.w.tick()
        self.assertEqual(self.sent()[-1]['label'], '12345')

    def test_roster_time_only_after_a_successful_forward_and_digest_timing(self):
        self.poster.ok = False
        self.db.add('+15555550140', text='fails')
        self.w.tick()
        r = self.roster()
        self.assertIsNone(r['+15555550140']['lastInboundAt'], 'labelled but not a reply target until delivered to X')
        self.poster.ok = True
        self.now += 30                                      # past the webhook backoff
        self.w.tick()
        self.assertEqual(self.roster()['+15555550140']['lastInboundAt'], self.now)

    def test_pause_still_drops_everything(self):
        self.cfg['paused'] = True
        self.db.add('+15555550140', text='while paused')
        self.w.tick()
        self.cfg['paused'] = False
        self.w.tick()
        self.assertEqual(self.sent(), [])


class PauseQuiet(Base):
    def test_pause_drops_and_resume_continues(self):
        self.w.tick()
        self.cfg['paused'] = True
        self.db.add(ALLOWED, text='while paused')
        before = self.runner.count('/usr/bin/pgrep')
        c = self.w.tick()
        self.assertEqual(self.poster.calls, [])
        self.assertEqual(self.runner.count('/usr/bin/pgrep'), before, 'paused: no Messages checks either')
        self.cfg['paused'] = False
        self.db.add(ALLOWED, text='after resume')
        self.w.tick()
        self.assertEqual([p['text'] for _, p, _ in self.poster.calls], ['after resume'])

    def test_quiet_hours_hold_in_memory_then_digest(self):
        self.w.tick()
        self.cfg['quietHours'] = {'start': '00:00', 'end': '23:59', 'tz': 'UTC'}
        self.now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc).timestamp()
        a = self.db.add(ALLOWED, text='night one')
        self.db.add(ALLOWED, text='night two')
        c = self.w.tick()
        self.assertEqual((self.poster.calls, c['held']), ([], 2))
        self.assertEqual(self.state()['lastRowid'], a - 1, 'committed cursor stays behind held rows')
        self.assert_no_bodies_on_disk('night one', 'night two')
        self.assertEqual(self.m.rc.read_json(self.m.watch.STATUS_PATH)['held'], 2)
        self.now = datetime(2026, 10, 18, 23, 59, tzinfo=timezone.utc).timestamp()      # quiet hours over
        self.db.add(ALLOWED, text='morning')
        self.w.tick()
        self.assertEqual(self.poster.types(), ['digest', 'message'])
        d = self.poster.calls[0][1]
        self.assertEqual((d['count'], [x['text'] for x in d['messages']]), (2, ['night one', 'night two']))
        self.assertEqual(self.poster.calls[1][1]['text'], 'morning')
        self.assertEqual(self.state()['lastRowid'], a + 2)

    def test_restart_during_quiet_hours_rereads_from_chat_db(self):
        self.w.tick()
        self.cfg['quietHours'] = {'start': '00:00', 'end': '23:59', 'tz': 'UTC'}
        self.now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc).timestamp()
        self.db.add(ALLOWED, text='held then restarted')
        self.w.tick()
        self.w = self.new_watcher()                                   # process restart: memory gone
        self.now = datetime(2026, 10, 18, 23, 59, tzinfo=timezone.utc).timestamp()
        self.w.tick()
        self.assertEqual(self.poster.types(), ['message'])
        self.assertEqual(self.poster.calls[0][1]['text'], 'held then restarted')


class ReplyRouting(Base):
    """The watcher tells the host who was relayed last (handle + time, never text), and only once
    the owner can actually see the message (successful webhook post, not when held)."""
    def recent(self):
        return sorted([{'handle': r['handle'], 'at': r['lastInboundAt']} for r in self.m.rc.load_roster()
                       if r['lastInboundAt'] is not None], key=lambda r: -r['at'])

    def test_successful_forward_records_handle_and_time_only(self):
        self.cfg['contacts'].append({'name': 'Alex Rivera', 'handle': '+15555550124', 'label': 'KS'})
        self.w.tick()
        self.db.add('+15555550124', text='alex says hi')
        self.w.tick()
        self.assertEqual(self.poster.calls[-1][1]['label'], 'KS')
        self.assertEqual(self.recent(), [{'handle': '+15555550124', 'at': self.now}])
        self.assert_no_bodies_on_disk('alex says hi')
        self.now += 100
        self.db.add(ALLOWED, text='then test')
        self.w.tick()
        self.assertEqual([r['handle'] for r in self.recent()], [ALLOWED, '+15555550124'])

    def test_failed_or_held_messages_are_not_recorded(self):
        self.w.tick()
        self.poster.ok = False
        self.db.add(ALLOWED, text='lost for now')
        self.w.tick()
        self.assertEqual(self.recent(), [])
        self.cfg['quietHours'] = {'start': '00:00', 'end': '23:59', 'tz': 'UTC'}
        self.now = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc).timestamp()
        self.poster.ok = True
        self.w.tick()
        self.assertEqual(self.recent(), [], 'held during quiet hours: owner has not seen it')
        self.now = datetime(2026, 10, 18, 23, 59, tzinfo=timezone.utc).timestamp()
        self.w.tick()
        self.assertEqual(self.poster.types()[-1], 'digest')
        self.assertEqual(self.poster.calls[-1][1]['messages'][0]['label'], 'Test')
        self.assertEqual(self.recent(), [{'handle': ALLOWED, 'at': self.now}], 'recorded when the digest is posted')


class Failures(Base):
    def test_webhook_failure_retries_with_backoff_without_loss_or_dupes(self):
        self.w.tick()
        self.poster.ok = False
        self.db.add(ALLOWED, text='first')
        self.db.add(ALLOWED, text='second')
        c = self.w.tick()
        self.assertEqual((c['webhookFailures'], len(self.poster.calls)), (1, 1), 'stops at first failure (keeps order)')
        self.now += 5
        self.w.tick()
        self.assertEqual(len(self.poster.calls), 1, 'backoff: no post within 10 s')
        self.poster.ok = True
        self.now += 30
        self.w.tick()
        self.assertEqual([p['text'] for _, p, _ in self.poster.calls[1:]], ['first', 'second'])
        self.now += 30
        self.w.tick()
        self.assertEqual(len(self.poster.calls), 3, 'no duplicates')

    def test_chat_db_unavailable_reports_fda_once(self):
        os.unlink(self.db.path)
        self.w.tick()
        self.w.tick()
        self.assertEqual(self.poster.types(), ['status'])
        self.assertEqual(self.poster.calls[0][1]['status'], 'fda_missing')
        self.assertNotIn('text', self.poster.calls[0][1])
        self.assertEqual(self.m.rc.read_json(self.m.watch.STATUS_PATH)['chatDb'], 'unavailable')
        self.assertEqual(read_log().count('chat_db_unavailable'), 1, 'logged at most hourly')

    def test_chat_db_access_events_drive_relayctl_status(self):
        st = self.m.rc.watcher_chat_db_state
        self.assertEqual(st()['state'], 'unknown')
        self.w.tick()
        self.w.tick()
        self.assertEqual(read_log().count('"chat_db_ok"'), 1, 'once per start')
        self.assertEqual(st()['state'], 'ok')
        os.rename(self.db.path, self.db.path + '.away')       # access lost (FDA removed)
        self.w.tick()
        self.assertEqual((st()['state'], st()['event']), ('denied', 'chat_db_unavailable'))
        os.rename(self.db.path + '.away', self.db.path)       # granted again
        self.w.tick()
        self.assertEqual(st()['state'], 'ok')
        os.rename(self.db.path, self.db.path + '.away')       # lost again within the hour: still logged
        self.w.tick()
        self.assertEqual(st()['state'], 'denied')
        with open(os.environ['IMESSAGE_RELAY_LOG'], 'a') as f:
            f.write('not json "event"\n{"event": "tick"}\n')
        self.assertEqual(st()['state'], 'denied', 'other lines are skipped')


class MessagesApp(Base):
    def test_not_running_opt_out_logs_and_posts_one_heads_up(self):
        self.runner.messages_running = False
        self.w.tick()
        self.now += 60
        self.w.tick()
        self.assertEqual(self.runner.count('/usr/bin/open'), 0, 'never reopens unless opted in')
        self.assertEqual(self.poster.types(), ['status'])
        st = self.poster.calls[0][1]
        self.assertEqual(st['status'], 'messages_not_running')
        self.assertEqual(set(st) - {'id'}, {'type', 'status', 'detail', 'at'}, 'status heads-up carries no message body')
        self.assertTrue(st.get('id'), 'every webhook payload carries a unique id (item B)')
        self.assertEqual(read_log().count('"event": "messages_not_running"'), 1)
        self.now += 301
        self.w.tick()
        self.assertEqual(read_log().count('"event": "messages_not_running"'), 2, 'logged at most every 5 minutes')
        self.assertEqual(self.poster.types(), ['status'], 'heads-up only once per outage')
        self.runner.messages_running = True
        self.w.tick()
        self.runner.messages_running = False
        self.w.tick()
        self.assertEqual(self.poster.types(), ['status', 'status'], 'a new outage gets a new heads-up')

    def test_auto_reopen_opt_in_at_most_every_5_minutes(self):
        self.cfg['autoReopenMessages'] = True
        self.runner.messages_running = False
        self.w.tick()
        self.now += 60
        self.w.tick()
        opens = [c for c in self.runner.calls if c[0] == '/usr/bin/open']
        self.assertEqual(opens, [['/usr/bin/open', '-g', '-j', '-a', 'Messages']])
        self.now += 300
        self.w.tick()
        self.assertEqual(self.runner.count('/usr/bin/open'), 2)
        self.assertEqual(read_log().count('messages_reopened'), 2)
        self.assertEqual(self.poster.calls, [], 'no heads-up when auto-reopen is on')


class Jobs(Base):
    def job(self, **job):
        os.makedirs(self.m.watch.JOBS_DIR, exist_ok=True)
        job.setdefault('createdAt', self.now)
        self.m.rc.write_json_atomic(os.path.join(self.m.watch.JOBS_DIR, 'j1.job.json'), dict(job, id='j1'))
        self.w.process_jobs(self.m.rc.sanitize_config(self.cfg))
        return self.m.rc.read_json(os.path.join(self.m.watch.JOBS_DIR, 'j1.result.json'))

    def test_recent_senders_with_contact_names_no_text(self):
        self.db.add(OTHER, text='a secret')
        self.db.add(OTHER, text='another secret')
        self.db.add(OWNER, text='note to self')
        self.db.add('+15555550188', text='group only', group=True)
        ab = os.path.join(self.tmp, 'ab')
        os.makedirs(ab)
        con = sqlite3.connect(os.path.join(ab, 'AddressBook-v22.abcddb'))
        con.executescript("""CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZORGANIZATION TEXT);
          CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
          CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
          INSERT INTO ZABCDRECORD VALUES (1, 'Sam', 'Example', NULL);
          INSERT INTO ZABCDPHONENUMBER VALUES (1, '(555) 555-0188');""")
        con.commit(); con.close()
        res = self.job(type='recent_senders', days=30, limit=10)
        self.assertTrue(res['ok'])
        got = {s['handle']: s for s in res['senders']}
        self.assertEqual(set(got), {ALLOWED, OTHER}, 'owner and group-only senders excluded')
        self.assertEqual((got[OTHER]['name'], got[OTHER]['count'], got[OTHER]['alreadyContact']), ('Sam Example', 2, False))
        self.assertTrue(got[ALLOWED]['alreadyContact'])
        self.assertNotIn('secret', json.dumps(res))

    def test_confirm_delivery_job(self):
        self.db.add(OWNER, text='Relay is set up', from_me=1, delivered=1)
        res = self.job(type='confirm_delivery', handle=OWNER, since=self.now - 60, attempts=1)
        self.assertEqual((res['delivered'], res['note']), (True, 'delivered'))

    def test_stale_jobs_are_dropped(self):
        self.assertIsNone(self.job(type='recent_senders', createdAt=self.now - 3600))


if __name__ == '__main__':
    unittest.main()


class PendingDelivery(Base):
    """Delivery proof moved to the watcher (it holds Full Disk Access; the host under Chrome does not).
    The host leaves body-free pending records; the watcher checks chat.db and notifies only on failure."""
    def add_pending(self, handle=ALLOWED, text='hello back', label='Test', at=None):
        at = self.now if at is None else at
        self.m.rc.add_pending_send(handle, at, len(text), label=label)

    def test_delivered_posts_the_single_confirmation(self):
        # item 6: the watcher posts the ONE confirmation once chat.db shows delivered (host no longer does).
        self.w.tick()                                           # baseline
        self.db.add(ALLOWED, text='hello back', from_me=1, delivered=1, when=self.now)
        self.add_pending(label='Test')
        self.w.tick()
        self.assertEqual(self.m.rc.load_pending(), [], 'resolved entry is removed')
        self.assertNotIn('send_failed', self.poster.types())
        sent = [p for _, p, _ in self.poster.calls if p.get('type') == 'sent']
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]['label'], sent[0]['delivered'], sent[0]['dryRun']), ('Test', True, False))
        self.assertNotIn('hello back', json.dumps(self.poster.calls))   # confirmation carries no body
        log = read_log()
        self.assertIn('"status": "delivered"', log)
        self.assertNotIn('hello back', log)                    # counts only, never the body

    def test_confirmSends_off_posts_no_confirmation(self):
        self.cfg['confirmSends'] = False
        self.w.tick()
        self.db.add(ALLOWED, text='hello back', from_me=1, delivered=1, when=self.now)
        self.add_pending(label='Test')
        self.w.tick()
        self.assertEqual(self.m.rc.load_pending(), [])
        self.assertNotIn('sent', self.poster.types())

    def test_no_row_waits_then_fails_unconfirmed_at_timeout(self):
        # W3: with NO matching chat.db row, the watcher never claims "✓ sent"; it waits silently and,
        # at the 10-minute timeout, posts a "couldn’t confirm delivery" failure.
        self.w.tick()
        self.add_pending(label='Test')                         # no matching outgoing row ever appears
        self.now += self.m.watch.SEND_CONFIRM_FALLBACK_S + 1
        self.w.tick()
        self.assertEqual(len(self.m.rc.load_pending()), 1, 'kept; no soft confirmation without a row')
        self.assertNotIn('sent', self.poster.types())
        self.assertNotIn('send_failed', self.poster.types())
        self.now += self.m.rc.PENDING_TTL_S + 1
        self.w.tick()
        self.assertEqual(self.m.rc.load_pending(), [], 'dropped at the timeout')
        failed = [p for _, p, _ in self.poster.calls if p.get('type') == 'send_failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]['label'], failed[0].get('reason')), ('Test', 'unconfirmed'))

    def test_soft_sent_at_20s_with_row_then_late_failure(self):
        # W3: a matching row exists but delivery is unconfirmed -> soft "✓ sent" at 20s, record KEPT,
        # and a failure that arrives later still posts a send_failed.
        self.w.tick()
        r = self.db.add(ALLOWED, text='hello back', from_me=1, delivered=0, when=self.now)
        self.add_pending(label='Test')
        self.now += self.m.watch.SEND_CONFIRM_FALLBACK_S + 1
        self.w.tick()
        sent = [p for _, p, _ in self.poster.calls if p.get('type') == 'sent']
        self.assertEqual((len(sent), sent[0]['delivered']), (1, False), 'one soft ✓ sent')
        self.assertEqual(len(self.m.rc.load_pending()), 1, 'record kept after the soft confirmation')
        import sqlite3
        c = sqlite3.connect(self.db.path); c.execute('UPDATE message SET error=22 WHERE ROWID=?', (r,)); c.commit(); c.close()
        self.now += 30
        self.w.tick()
        self.assertEqual(self.m.rc.load_pending(), [])
        failed = [p for _, p, _ in self.poster.calls if p.get('type') == 'send_failed']
        self.assertEqual(self.poster.types()[-1], 'send_failed')
        self.assertTrue(failed[-1].get('superseded'), 'late failure after a soft ✓ sent is flagged superseded')

    def test_soft_sent_then_delivered_does_not_double_post(self):
        # W3: after a soft "✓ sent", a later delivered just clears the record (no second confirmation).
        self.w.tick()
        r = self.db.add(ALLOWED, text='hi', from_me=1, delivered=0, when=self.now)
        self.add_pending(label='Test')
        self.now += self.m.watch.SEND_CONFIRM_FALLBACK_S + 1
        self.w.tick()
        import sqlite3
        c = sqlite3.connect(self.db.path); c.execute('UPDATE message SET is_delivered=1 WHERE ROWID=?', (r,)); c.commit(); c.close()
        self.now += 5
        self.w.tick()
        sent = [p for _, p, _ in self.poster.calls if p.get('type') == 'sent']
        self.assertEqual(len(sent), 1, 'still only the one soft confirmation')
        self.assertEqual(self.m.rc.load_pending(), [])

    def test_failed_send_notifies_webhook_with_label_only(self):
        self.w.tick()
        self.db.add(ALLOWED, text='hello back', from_me=1, delivered=0, error=1, when=self.now)
        self.add_pending(label='Test')
        self.w.tick()
        self.assertEqual(self.m.rc.load_pending(), [])
        self.assertEqual(self.poster.types()[-1], 'send_failed')
        payload = self.poster.calls[-1][1]
        self.assertEqual(payload['label'], 'Test')
        self.assertEqual(payload['error'], 1)
        self.assertNotIn('hello back', json.dumps(self.poster.calls))

    def test_unconfirmed_entry_is_kept_inside_window(self):
        self.w.tick()
        self.add_pending()                                     # no matching outgoing row yet
        self.w.tick()
        self.assertEqual(len(self.m.rc.load_pending()), 1, 'kept while still inside the 20s window')
        self.assertNotIn('send_failed', self.poster.types())
        self.assertNotIn('sent', self.poster.types())

    def test_pending_file_holds_no_bodies(self):
        self.w.tick()
        self.add_pending(text='secret body text')
        self.assert_no_bodies_on_disk('secret body text')
