"""v1.4.6: fast feedback in the owner's X tab (✅ / ❓ / 🤖 prompts / 👀 / mirrored tapbacks), the relay
ignoring its own output, undo, quiet names (+ quote routing), follow-messages spam mode, STOP-footer
cleanup, the one-post overnight recap. Fictional names only."""
import json
import os
import sqlite3
import sys
import time
import unittest

from helpers import fresh, tmpdir, FakeClock, FakeRunner, ChatDb, MAC
import test_watch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

CONV = '1111-2222'
XU = 'df48633d-6588-4b18-bc77-000000000001'
ALLOWED = test_watch.ALLOWED
STRANGER = '+15555550166'
SAM = '+15555550150'
CHECK, EYES, QMARK, ROBOT = '\u2705', '\U0001f440', '\u2753', '\U0001f916'


class HostBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.h = self.m.host
        self.clock = FakeClock()
        self.raw = {'dryRun': False, 'relayConversationId': CONV, 'webhookUrl': 'https://example.invalid/hook',
                    'contacts': [{'name': 'Jordan Test', 'handle': '15555550123'}],
                    'ownerHandle': '+15555550199', 'confirmSends': True}
        self.posts = []
        self.m.rc.record_inbound('+15555550123', self.clock.t - 60)

    def cfg(self, **over):
        return self.m.rc.sanitize_config(dict(self.raw, **over))

    def poster(self, url, payload, token='', timeout=15):
        self.posts.append(json.loads(json.dumps(payload)))
        return True

    def req(self, msg, runner=None, **over):
        base = {'conversationId': CONV, 'ts': '2026-10-18T21:32:00Z'}
        base.update(msg)
        return self.h.handle_request(base, cfg=self.cfg(**over), runner=runner or FakeRunner([(0, '')]),
                                     sleeper=self.clock.sleep, now=self.clock.now, poster=self.poster)

    def retry_files(self):
        d = self.h.NOTIFY_RETRY_DIR
        return [self.m.rc.read_json(os.path.join(d, n)) for n in sorted(os.listdir(d))] if os.path.isdir(d) else []


class HostConfirm(HostBase):
    def test_sent_reply_gets_a_check_reaction_directive_and_a_ui_pending(self):
        r = self.req({'type': 'send', 'text': 'Jordan: on my way', 'ui': True, 'xUuid': XU})
        self.assertTrue(r['ok'] and r['sent'])
        self.assertEqual(r['ui']['react'], [{'emoji': CHECK, 'uuid': XU, 'pendingId': r['pendingId']}])
        pend = self.m.rc.load_pending()
        self.assertEqual(len(pend), 1)
        self.assertEqual((pend[0]['xUuid'], pend[0]['ui']), (XU, True))
        self.assertNotIn('on my way', json.dumps(pend))

    def test_without_ui_nothing_changes(self):
        r = self.req({'type': 'send', 'text': 'Jordan: on my way'})
        self.assertNotIn('ui', r)
        self.assertNotIn('pendingId', r)
        self.assertNotIn('ui', self.m.rc.load_pending()[0])

    def test_ui_reactions_off_falls_back(self):
        r = self.req({'type': 'send', 'text': 'Jordan: on my way', 'ui': True, 'xUuid': XU}, uiReactions=False)
        self.assertNotIn('ui', r)

    def test_bad_uuid_is_ignored(self):
        r = self.req({'type': 'send', 'text': 'Jordan: on my way', 'ui': True, 'xUuid': '"><script>'})
        self.assertNotIn('ui', r)

    def test_ui_ack_marks_the_pending_send(self):
        r = self.req({'type': 'send', 'text': 'Jordan: on my way', 'ui': True, 'xUuid': XU})
        self.req({'type': 'ui_ack', 'pendingId': r['pendingId'], 'ok': True, 'step': 'placed', 'latencyMs': 2100})
        self.assertTrue(self.m.rc.load_pending()[0]['uiConfirmed'])
        r2 = self.req({'type': 'send', 'text': 'Jordan: second one', 'ui': True, 'xUuid': XU.replace('01', '02')})
        self.req({'type': 'ui_ack', 'pendingId': r2['pendingId'], 'ok': False, 'step': 'no-react-button'})
        p2 = [p for p in self.m.rc.load_pending() if p['id'] == r2['pendingId']][0]
        self.assertTrue(p2['uiFailed'])
        self.assertFalse(p2['uiConfirmed'])


class HostPrompts(HostBase):
    def guessed(self, **over):
        return self.req({'type': 'send', 'text': 'see you soon', 'ui': True, 'xUuid': XU}, confirmGuessed='always', **over)

    def test_confirm_prompt_is_typed_at_once_and_the_webhook_waits(self):
        # v1.4.12: typed 🤖 text (no ❓ reaction: X's picker could take ~11 s to fail)
        r = self.guessed()
        self.assertTrue(r.get('needsConfirm'))
        self.assertEqual(self.posts, [], 'the routine copy is deferred')
        self.assertNotIn('react', r['ui'])
        react = r['ui']['type'][0]
        self.assertTrue(react['text'].startswith(ROBOT + ' '))
        files = self.retry_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['payload']['type'], 'needs_confirm')
        self.assertGreater(files[0]['notBefore'], files[0]['queuedAt'])
        self.assertNotIn('see you soon', json.dumps(files))
        # the extension placed it: the routine copy is cancelled
        self.req({'type': 'ui_ack', 'noteId': react['noteId'], 'ok': True, 'step': 'placed'})
        self.assertEqual(self.retry_files(), [])
        self.assertEqual(self.posts, [])

    def test_a_missed_reaction_posts_the_text_prompt_at_once(self):
        r = self.guessed()
        self.req({'type': 'ui_ack', 'noteId': r['ui']['type'][0]['noteId'], 'ok': False, 'step': 'no-composer'})
        self.assertEqual([p['type'] for p in self.posts], ['needs_confirm'])
        self.assertEqual(self.retry_files(), [])

    def test_who_is_this_for_is_typed_with_the_robot_tag(self):
        self.m.rc.record_inbound('+15555550123', self.clock.t - 99999)    # nobody recent -> "Who's this for?"
        r = self.req({'type': 'send', 'text': 'hello there', 'ui': True, 'xUuid': XU})
        self.assertTrue(r.get('needsTarget'))
        t = r['ui']['type'][0]
        self.assertTrue(t['text'].startswith(ROBOT + ' '))
        self.assertEqual(self.posts, [])
        self.assertEqual(len(self.retry_files()), 1)

    def test_ui_prompts_off_keeps_the_routine(self):
        r = self.guessed(uiPrompts=False)
        self.assertNotIn('react', r.get('ui', {}))
        self.assertEqual([p['type'] for p in self.posts], ['needs_confirm'])


class HostIgnoresItsOwnOutput(HostBase):
    def test_robot_tagged_text_is_never_sent_or_routed(self):
        runner = FakeRunner([])
        for text in (ROBOT + ' Who\u2019s this for? Start with a name', ROBOT + ' Jordan: hi', '  ' + ROBOT + 'x',
                     'Who\u2019s this for? Start with a name, like "Jordan: \u2026"', 'Reply to Jordan? Reply yes within 2 min.'):
            r = self.req({'type': 'send', 'text': text, 'ui': True, 'xUuid': XU}, runner=runner)
            self.assertEqual(r.get('skipped'), 'relay prompt', text)
        self.assertEqual(runner.count('/usr/bin/osascript'), 0)
        self.assertEqual(self.m.rc.load_pending(), [])
        self.assertEqual(self.posts, [])

    def test_ordinary_text_with_a_robot_later_is_sent(self):
        r = self.req({'type': 'send', 'text': 'Jordan: the ' + ROBOT + ' vacuum is here'})
        self.assertTrue(r.get('sent'))

    def test_poll_returns_ids_and_emoji_only(self):
        self.m.ui.enqueue_react(XU, EYES, 'read')
        r = self.req({'type': 'ui_poll'})
        self.assertEqual([(a['uuid'], a['emoji']) for a in r['actions']], [(XU, EYES)])
        self.assertEqual(set(r['actions'][0]), {'id', 'action', 'uuid', 'emoji'})
        self.assertEqual(self.req({'type': 'ui_poll'})['actions'], [], 'leased, not offered twice at once')
        self.req({'type': 'ui_ack', 'actionId': r['actions'][0]['id'], 'ok': True})
        self.assertEqual(self.m.ui.state_of(r['actions'][0]['id']), 'done')

    def test_get_config_tells_the_extension(self):
        r = self.req({'type': 'get_config'})
        self.assertEqual((r['uiReactions'], r['undoSeconds']), (True, 5))
        r = self.req({'type': 'get_config'}, undoSeconds=99)
        self.assertEqual(r['undoSeconds'], 10, 'clamped')


class UnackedWarning(HostBase):
    def test_unacked_posts_the_reload_line_verbatim(self):
        line = '\u26a0\ufe0f Didn\u2019t send. Reload X Chat and retype.'
        r = self.req({'type': 'signal', 'status': 'unacked'})
        self.assertTrue(r.get('ok'))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['type'], 'status')
        self.assertEqual(self.posts[0]['detail'], line)
        ev = rp.format_event(self.posts[0])
        self.assertEqual(ev['lines'][0]['text'], line)
        self.assertNotIn('Not sent.', ev['lines'][0]['text'])
        with open(self.m.rc.LOG_PATH, encoding='utf-8') as f:
            log = f.read()
        self.assertIn('"status": "unacked"', log)
        self.assertNotIn('Reload', log)
        self.assertNotIn('retype', log)


class UiQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)

    def test_only_valid_ids_and_emoji(self):
        ui = self.m.ui
        self.assertEqual(ui.enqueue_react('bad id', CHECK), '')
        self.assertEqual(ui.enqueue_react(XU, 'hello'), '')
        self.assertEqual(ui.enqueue_react(XU, '<img>'), '')
        self.assertTrue(ui.enqueue_react(XU, '\u2764\ufe0f'))
        self.assertTrue(ui.enqueue_react(XU, '\U0001f525'), 'a custom emoji tapback')

    def test_expiry(self):
        ui = self.m.ui
        aid = ui.enqueue_react(XU, CHECK, at=1000.0)
        self.assertEqual(ui.take(now=1000.0 + ui.ACTION_TTL_S + 1), [])
        self.assertEqual(ui.state_of(aid), 'expired')

    def test_map_strips_part_prefixes(self):
        ui = self.m.ui
        ui.map_put('ABC-123', XU, rowid=5)
        self.assertEqual(ui.map_get('p:0/ABC-123')['x'], XU)
        self.assertEqual(ui.map_get('bp:ABC-123')['x'], XU)
        self.assertIsNone(ui.map_get('p:0/OTHER'))


class WatchBase(test_watch.Base):
    def setUp(self):
        super().setUp()
        con = sqlite3.connect(self.db.path)
        con.execute('ALTER TABLE message ADD COLUMN is_read INTEGER DEFAULT 0')
        con.execute('ALTER TABLE message ADD COLUMN date_read INTEGER DEFAULT 0')
        con.commit()
        con.close()
        self.w = self.new_watcher()
        self.w.tick()

    def sent(self, delivered=1, xu=XU, ui=True, guid='OUT-1', ago=30, **fields):
        rc = self.m.rc
        self.db.add(ALLOWED, text='hello back', from_me=1, delivered=delivered, guid=guid)
        pid = rc.add_pending_send(ALLOWED, self.now - ago, len('hello back'), label='Jordan', x_uuid=xu, ui=ui)
        if fields:
            rc.update_pending(pid, **fields)
        return pid

    def types(self):
        return self.poster.types()


class WatchConfirm(WatchBase):
    def test_ui_confirmed_send_posts_no_text_line(self):
        self.sent(uiConfirmed=True)
        self.w.tick()
        self.assertNotIn('sent', self.types())
        self.assertEqual(self.m.rc.load_pending(), [])

    def test_waits_for_the_ack_then_falls_back(self):
        self.sent(ago=2)
        self.w.tick()
        self.assertNotIn('sent', self.types(), 'within the grace window: wait for the ✅')
        self.assertEqual(len(self.m.rc.load_pending()), 1)
        self.now += 15
        self.w.tick()
        self.assertEqual(self.types().count('sent'), 1, 'no ack -> the old text confirmation')

    def test_an_ack_at_twelve_seconds_still_suppresses_the_line(self):
        # The ✅ attempt can take ~12s, past the old 10s grace. Wait, then a placed reaction posts nothing.
        pid = self.sent(ago=12)
        self.w.tick()
        self.assertNotIn('sent', self.types())
        self.assertEqual(len(self.m.rc.load_pending()), 1)
        self.m.rc.update_pending(pid, uiConfirmed=True)
        self.now += 10
        self.w.tick()
        self.assertNotIn('sent', self.types())
        self.assertEqual(self.m.rc.load_pending(), [])

    def test_failed_reaction_posts_at_once(self):
        self.sent(ago=2, uiFailed=True)
        self.w.tick()
        self.assertEqual(self.types().count('sent'), 1)

    def test_failures_are_always_posted(self):
        self.db.add(ALLOWED, text='x', from_me=1, error=22, guid='OUT-F')
        pid = self.m.rc.add_pending_send(ALLOWED, self.now - 30, 1, label='Jordan', x_uuid=XU, ui=True)
        self.m.rc.update_pending(pid, uiConfirmed=True)
        self.w.tick()
        self.assertIn('send_failed', self.types())

    def test_read_receipt_upgrades_to_eyes(self):
        self.sent(uiConfirmed=True)
        self.w.tick()
        self.assertEqual(self.m.ui.take(now=time.time()), [], 'not read yet')
        con = sqlite3.connect(self.db.path)
        con.execute("UPDATE message SET is_read = 1, date_read = 12345 WHERE guid = 'OUT-1'")
        con.commit()
        con.close()
        self.w.tick()
        acts = self.m.ui.take(now=time.time())
        self.assertEqual([(a['uuid'], a['emoji']) for a in acts], [(XU, EYES)])
        self.w.tick()
        self.assertEqual(len(os.listdir(self.m.ui.QUEUE_DIR)), 1, 'once only')

    def test_no_read_receipts_for_groups_or_when_off(self):
        self.cfg['readReceipts'] = False
        self.sent(uiConfirmed=True)
        self.w.tick()
        con = sqlite3.connect(self.db.path)
        con.execute("UPDATE message SET is_read = 1 WHERE guid = 'OUT-1'")
        con.commit()
        con.close()
        self.w.tick()
        self.assertEqual(self.m.ui.take(now=time.time()), [])

    def test_map_and_queue_hold_ids_only(self):
        self.sent(uiConfirmed=True)
        self.w.tick()
        self.assert_no_bodies_on_disk('hello back')


class WatchTapbacks(WatchBase):
    def mapped(self):
        self.sent(uiConfirmed=True)
        self.w.tick()
        self.assertIsNotNone(self.m.ui.map_get('OUT-1'))

    def tapback(self, assoc=2000, guid='p:0/OUT-1', emoji=None):
        self.db.add(ALLOWED, text='Loved \u201chello back\u201d', assoc=assoc, assoc_guid=guid, assoc_emoji=emoji)

    def test_mirrored_tapback_replaces_the_text_line(self):
        self.mapped()
        self.tapback()
        self.w.tick()
        self.assertNotIn('message', self.types(), 'held while the extension places it')
        acts = self.m.ui.take(now=time.time())
        self.assertEqual([(a['uuid'], a['emoji']) for a in acts], [(XU, '\u2764\ufe0f')])
        self.m.ui.ack(acts[0]['id'], True, 'placed')
        self.w.tick()
        self.assertNotIn('message', self.types(), 'shown as a real reaction: no text line')

    def test_failed_mirror_falls_back_to_the_text_line(self):
        self.mapped()
        self.tapback(assoc=2001)
        self.w.tick()
        acts = self.m.ui.take(now=time.time())
        self.m.ui.ack(acts[0]['id'], False, 'no-react-button')
        self.w.tick()
        msgs = [p for _, p, _ in self.poster.calls if p['type'] == 'message']
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0]['reaction'])
        self.assertIn('\U0001f44d', msgs[0]['text'])

    def test_unanswered_mirror_times_out_to_text(self):
        self.mapped()
        self.tapback()
        self.w.tick()
        self.now += 25
        self.w.tick()
        self.assertEqual(self.types().count('message'), 1)

    def test_tapback_on_an_unmapped_message_is_a_text_line_as_before(self):
        self.tapback(guid='p:0/NOT-MINE')
        self.w.tick()
        self.assertEqual(self.types().count('message'), 1)
        self.assertEqual(self.m.ui.take(now=time.time()), [])

    def test_off_switch(self):
        self.cfg['mirrorTapbacks'] = False
        self.mapped()
        self.tapback()
        self.w.tick()
        self.assertEqual(self.types().count('message'), 1)


class FollowMessages(test_watch.Base):
    def setUp(self):
        super().setUp()
        self.cfg['relayAll'] = True
        con = sqlite3.connect(self.db.path)
        con.execute('ALTER TABLE chat ADD COLUMN is_filtered INTEGER DEFAULT 0')
        con.execute('ALTER TABLE message ADD COLUMN filter_action INTEGER DEFAULT 0')
        con.commit()
        con.close()
        self.prefs = {'FilterMessageRequests': '1', 'EnableJunkFiltering': '1'}
        test = self

        class PrefRunner(FakeRunner):
            def __call__(self, cmd, **kw):
                if cmd[:3] == ['/usr/bin/defaults', 'read', 'com.apple.MobileSMS']:
                    self.calls.append(list(cmd))
                    v = test.prefs.get(cmd[3])
                    import subprocess
                    return subprocess.CompletedProcess(cmd, 0 if v is not None else 1, (v or '') + '\n', '')
                return super().__call__(cmd, **kw)
        self.runner = PrefRunner()
        self.w = self.new_watcher()
        self.w.tick()

    def set_filtered(self, handle_rowid_sql, value):
        con = sqlite3.connect(self.db.path)
        con.execute(handle_rowid_sql, (value,))
        con.commit()
        con.close()

    def msgs(self):
        return [p for _, p, _ in self.poster.calls if p['type'] == 'message']

    def add(self, handle, text, is_filtered=0, fa=0):
        mid = self.db.add(handle, text=text)
        con = sqlite3.connect(self.db.path)
        con.execute('UPDATE chat SET is_filtered = ? WHERE ROWID = (SELECT chat_id FROM chat_message_join WHERE message_id = ?)', (is_filtered, mid))
        con.execute('UPDATE message SET filter_action = ? WHERE ROWID = ?', (fa, mid))
        con.commit()
        con.close()
        return mid

    def test_default_is_follow_messages(self):
        self.assertEqual(self.m.rc.sanitize_config({})['spamMode'], 'follow-messages')

    def test_unknown_senders_bucket_is_tagged(self):
        self.add(STRANGER, 'are you free for a quick call', is_filtered=1)
        self.w.tick()
        self.assertTrue(self.msgs()[-1].get('unknown'))

    def test_main_list_is_not_tagged_even_if_not_in_contacts(self):
        self.add(STRANGER, 'hi it is Alex from the gym', is_filtered=0)
        self.w.tick()
        self.assertFalse(self.msgs()[-1].get('unknown'))

    def test_spam_is_held_and_summarized_as_counts_only(self):
        self.add(STRANGER, 'You won a prize click here', is_filtered=2)
        self.add(STRANGER, 'Final notice toll unpaid', is_filtered=2)
        self.w.tick()
        self.assertEqual(self.msgs(), [], 'Messages hides spam: so does the relay')
        self.now += 3700
        self.w.tick()
        st = [p for _, p, _ in self.poster.calls if p.get('status') == 'spam_summary']
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]['count'], 2)
        self.assertNotIn('prize', json.dumps(st))
        self.assert_no_bodies_on_disk('You won a prize', 'toll unpaid')

    def test_spam_from_a_relay_contact_is_tagged_not_held(self):
        self.add(ALLOWED, 'hey', is_filtered=2)
        self.w.tick()
        self.assertEqual(self.msgs()[-1].get('tag'), 'spam')

    def test_promotions_and_transactions(self):
        self.add(STRANGER, '20% off today only', fa=3)
        self.add('+15555550177', 'Your code is ready', fa=4)
        self.w.tick()
        self.assertEqual([m.get('tag') for m in self.msgs()], ['promotion', 'transaction'])

    def test_screening_off_is_mirrored(self):
        self.prefs['FilterMessageRequests'] = '0'
        self.w._prefs = None
        self.add(STRANGER, 'are you free for a quick call', is_filtered=1)
        self.w.tick()
        self.assertFalse(self.msgs()[-1].get('unknown'))

    def test_spam_filter_off_is_mirrored(self):
        self.prefs['EnableJunkFiltering'] = '0'
        self.w._prefs = None
        self.add(STRANGER, 'You won a prize', is_filtered=2)
        self.w.tick()
        self.assertEqual(len(self.msgs()), 1)

    def test_prefs_are_only_read(self):
        self.add(STRANGER, 'x y z', is_filtered=1)
        self.w.tick()
        cmds = [c for c in self.runner.calls if c[0] == '/usr/bin/defaults']
        self.assertTrue(cmds)
        self.assertTrue(all(c[1] == 'read' for c in cmds))

    def test_stop_footer_stripped_on_tagged_texts_only(self):
        self.add(STRANGER, 'Sale ends Friday at the shop. Reply STOP to opt out', fa=3)
        self.add(ALLOWED, 'Dad said reply STOP to opt out is annoying', is_filtered=0)
        self.w.tick()
        texts = [m['text'] for m in self.msgs()]
        self.assertEqual(texts[0], 'Sale ends Friday at the shop.')
        self.assertEqual(texts[1], 'Dad said reply STOP to opt out is annoying')

    def test_fallback_when_chat_db_cannot_tell(self):
        tmp = test_watch.Base.setUp  # noqa: F841 (documentation: a db without is_filtered)
        w = self.m.watch.Watcher
        self.assertTrue(w)
        # a chat.db with no is_filtered column -> the v1.4.5 'tag' behavior
        self.m.watch.Watcher.messages_filter  # exists
        ctx = {'chat_cols': set(), 'spam_col': False, 'fa_col': False}
        self.assertIsNone(self.w.messages_filter(None, 1, 1, ctx))

    def test_groups_unchanged(self):
        self.db.add(STRANGER, text='hi all', group='fam', display_name='Family', members=[STRANGER, SAM])
        con = sqlite3.connect(self.db.path)
        con.execute('UPDATE chat SET is_filtered = 2')
        con.commit()
        con.close()
        self.w.tick()
        m = self.msgs()[-1]
        self.assertFalse(m.get('unknown') or m.get('tag'))


class StripOptOut(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())

    def test_variants(self):
        f = self.m.watch.strip_opt_out
        self.assertEqual(f('Your table is ready. Reply STOP to unsubscribe.'), 'Your table is ready.')
        self.assertEqual(f('Deal inside! Msg&data rates may apply. Txt STOP to end'), 'Deal inside!')
        self.assertEqual(f('Big sale\n\u2002Text STOP to opt out'), 'Big sale')
        self.assertEqual(f('Reply STOP to opt out'), 'Reply STOP to opt out', 'never empties a text')
        self.assertEqual(f('stop by later?'), 'stop by later?')


class QuietNames(test_watch.Base):
    def setUp(self):
        super().setUp()
        self.cfg['contacts'] = [{'name': 'Jordan Test', 'handle': '15555550123'}, {'name': 'Sam Test', 'handle': '15555550150'}]
        self.cfg['senderXHandle'] = 'SenderBot'
        self.w = self.new_watcher()
        self.w.tick()

    def msgs(self):
        return [p for _, p, _ in self.poster.calls if p['type'] == 'message']

    def test_prefix_only_when_the_sender_changes(self):
        for h, t in ((ALLOWED, 'one'), (ALLOWED, 'two'), (SAM, 'three'), (SAM, 'four'), (ALLOWED, 'five')):
            self.db.add(h, text=t)
            self.w.tick()
        self.assertEqual([bool(m.get('quiet')) for m in self.msgs()], [False, True, False, True, False])
        lines = [rp.format_event(m)['lines'][0]['text'] for m in self.msgs()]
        self.assertEqual(lines, ['Jordan: one', 'two', 'Sam: three', 'four', 'Jordan: five'])

    def test_window(self):
        self.db.add(ALLOWED, text='one')
        self.w.tick()
        self.now += 601
        self.db.add(ALLOWED, text='two')
        self.w.tick()
        self.assertFalse(self.msgs()[-1].get('quiet'))

    def test_off(self):
        self.cfg['quietNamesSeconds'] = 0
        for t in ('one', 'two'):
            self.db.add(ALLOWED, text=t)
            self.w.tick()
        self.assertFalse(any(m.get('quiet') for m in self.msgs()))

    def test_batch_is_quiet_too(self):
        self.cfg['batchWebhooks'] = True
        self.db.add(ALLOWED, text='one')
        self.db.add(ALLOWED, text='two')
        self.db.add(SAM, text='three')
        self.w.tick()
        b = [p for _, p, _ in self.poster.calls if p['type'] == 'batch'][-1]
        self.assertEqual([bool(m.get('quiet')) for m in b['messages']], [False, True, False])

    def test_tapback_lines_keep_the_name(self):
        self.db.add(ALLOWED, text='one')
        self.w.tick()
        self.db.add(ALLOWED, text='Liked', assoc=2001, assoc_guid='p:0/whatever')
        self.w.tick()
        self.assertFalse(self.msgs()[-1].get('quiet'))

    def route(self, quoted, text='sounds good', quote_label=''):
        rc = self.m.rc
        cfg = rc.sanitize_config(self.cfg)
        return rc.route_reply(text, quoted, cfg, rc.load_roster(), self.now + 5, quote_label=quote_label)

    def test_native_quote_of_an_unprefixed_line_routes_to_its_sender(self):
        for h, t in ((SAM, 'first from sam'), (SAM, 'are we still on for the picnic saturday'), (ALLOWED, 'unrelated')):
            self.db.add(h, text=t)
            self.w.tick()
        r = self.route('are we still on for the picnic saturday')
        self.assertEqual((r['contact']['handle'], r['by']), (SAM, 'quote'))
        r = self.route('are we still on for the picnic s\u2026')      # a truncated preview
        self.assertEqual((r['contact']['handle'], r['by']), (SAM, 'quote'))
        r = self.route('SenderBot are we still on for the picnic saturday')
        self.assertEqual(r['contact']['handle'], SAM)

    def test_same_quiet_text_from_two_people_is_ambiguous(self):
        for h, t in ((SAM, 'hey'), (SAM, 'call me when you can please'), (ALLOWED, 'hi'), (ALLOWED, 'call me when you can please')):
            self.db.add(h, text=t)
            self.w.tick()
        r = self.route('call me when you can please')
        self.assertTrue(r.get('needsTarget'))

    def test_quote_keys_hold_no_text(self):
        for t in ('first', 'are we still on for the picnic saturday'):
            self.db.add(SAM, text=t)
            self.w.tick()
        raw = open(self.m.rc.QUOTE_KEYS_PATH).read()
        self.assertNotIn('picnic', raw)

    def test_the_first_line_body_is_keyed_and_a_chrome_word_matches_only_in_full(self):
        body = 'picnic at the lake saturday'
        self.db.add(ALLOWED, text=body)
        self.w.tick()
        self.assertFalse(self.msgs()[-1].get('quiet'))
        r = self.route(body)
        self.assertEqual((r['contact']['handle'], r['by']), (ALLOWED, 'quote'))
        r = self.route('iMessage ' + body)
        self.assertEqual((r['contact']['handle'], r['by']), (ALLOWED, 'quote'))
        r = self.route('iMessage picnic at the lake satur')   # chrome word plus a truncated body: not a prefix guess
        self.assertNotEqual(r.get('by'), 'quote')

    def test_two_senders_stay_ambiguous_with_a_chrome_word_and_a_label(self):
        body = 'call me when you can please'
        self.db.add(SAM, text=body)
        self.w.tick()
        self.db.add(ALLOWED, text=body)
        self.w.tick()
        for q in (body, 'iMessage ' + body):
            r = self.route(q, quote_label='Sam')
            self.assertTrue(r.get('needsTarget'), q)
            self.assertNotIn('contact', r)

    def test_a_header_label_routes_when_the_quote_is_not_keyed(self):
        r = self.route('zzzz never relayed body', quote_label='Sam')
        self.assertEqual((r['contact']['handle'], r['by']), (SAM, 'quote'))

    def test_a_shared_first_name_is_not_a_guess(self):
        self.cfg['contacts'] = [
            {'name': 'Sam Ellis', 'handle': '15555550150'},
            {'name': 'Sam Ortiz', 'handle': '15555550123'},
        ]
        r = self.route('zzzz never relayed body', quote_label='Sam')
        self.assertTrue(r.get('needsTarget'))
        self.assertNotIn('contact', r)

    def test_a_resolved_body_beats_a_different_header_label(self):
        self.db.add(SAM, text='picnic at the lake saturday')
        self.w.tick()
        r = self.route('picnic at the lake saturday', quote_label='Jordan')
        self.assertEqual((r['contact']['handle'], r['by']), (SAM, 'quote'))

    def test_a_reaction_line_is_not_quote_keyed(self):
        self.db.add(SAM, text='bring the salad please')
        self.w.tick()
        with open(self.m.rc.QUOTE_KEYS_PATH, encoding='utf-8') as fh:
            n = len(json.load(fh)['entries'])
        self.db.add(SAM, text='Liked', assoc=2001, assoc_guid='p:0/whatever')
        self.w.tick()
        with open(self.m.rc.QUOTE_KEYS_PATH, encoding='utf-8') as fh:
            self.assertEqual(len(json.load(fh)['entries']), n)
        self.assertNotEqual(self.route('Liked').get('by'), 'quote')


class RecapAndFormatting(unittest.TestCase):
    def test_tags(self):
        line = lambda **m: rp.format_event(dict({'type': 'message', 'label': 'Alex', 'text': 'hi'}, **m))['lines'][0]['text']
        self.assertEqual(line(tag='promotion'), 'Alex \u00b7 promo: hi')
        self.assertEqual(line(tag='transaction'), 'Alex \u00b7 receipt: hi')
        self.assertEqual(line(tag='spam'), '\U0001f6ab Alex \u00b7 spam: hi')
        self.assertEqual(line(quiet=True), 'hi')
        self.assertEqual(line(quiet=True, unknown=True), '\u26a0\ufe0f Alex \u00b7 not a contact: hi', 'tagged lines always name')
        self.assertEqual(line(quiet=True, group={'label': 'Family'}), 'Family \u00b7 Alex: hi', 'groups unchanged')

    def test_spam_summary_status(self):
        ev = rp.format_event({'type': 'status', 'status': 'spam_summary', 'detail': '\U0001f6ab 2 texts went to Messages \u203a Spam'})
        self.assertTrue(ev['lines'][0]['text'].startswith('\U0001f6ab 2 texts'))


if __name__ == '__main__':
    unittest.main()
