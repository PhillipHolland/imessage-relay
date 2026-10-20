"""v1.4.5: replies to a group (exact group name or a quote of its message, always confirmed, relay-all
only, delivery proven in the group chat), the wastebasket-to-block flow, and the spamMode tag."""
import json
import os
import sqlite3
import unittest

from helpers import ChatDb, FakeRunner, read_log
from test_v140 import CONV
from test_v143_handles import Base
import test_watch


def read_pending(d):
    out = []
    for f in sorted(os.listdir(d)):
        with open(os.path.join(d, f)) as fh:
            out.append(json.load(fh))
    return out

SAM = '+15555550150'
GROUP = 'group:chat0001'
GUID = 'iMessage;+;chat0001'


class Groups(Base):
    def setUp(self):
        super().setUp()
        self.raw['relayAll'] = True
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.db = ChatDb(os.path.join(self.tmp, 'chat.db'))
        self.db.add(SAM, text='x', group='fam', display_name='Family', members=[SAM, '+15555550151'])
        self.clock.t += 600
        self.m.rc.record_inbound(SAM, self.clock.t - 400, label='Sam')
        self.m.rc.record_inbound(GROUP, self.clock.t - 300, label='Family')

    def kinds(self, t):
        return [p for p, _ in self.posts if p['type'] == t]

    def confirm(self, text, **kw):
        r = self.send(text, **kw)
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual((r['handle'], r['reason']), (GROUP, 'group'))
        self.assertEqual(self.osa(), [])
        self.assertEqual(self.kinds('needs_confirm')[-1]['detail'], 'Reply to the Family group? Reply yes within 2 min.')
        return r

    def test_exact_name_any_case_asks_then_yes_sends_to_the_chat(self):
        self.confirm('family: cute pup')
        self.clock.t += 5
        r = self.send('family: cute pup', confirmed=True, handle=GROUP)
        self.assertTrue(r['ok'], r)
        call = self.osa()[0]
        self.assertEqual(call[2:4], ['cute pup', GUID])
        self.assertEqual(call[-1], 'chat')
        pend = read_pending(self.m.rc.PENDING_DIR)
        self.assertEqual([(p['handle'], p['chatGuid']) for p in pend], [(GROUP, GUID)])
        for s in ('cute pup',):
            self.assertNotIn(s, read_log())

    def test_native_quote_of_a_group_line_asks(self):
        self.confirm('aww', quotedText='iMessage Family \u00b7 Sam: look at this pup')

    def test_bare_reply_after_group_message_asks_whos_this_for(self):
        r = self.send('sounds good')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])
        self.assertIn('Who is this for?', self.kinds('needs_target')[-1]['detail'])

    def test_confirmed_without_a_prompt_is_refused(self):
        r = self.send('family: cute pup', confirmed=True, handle=GROUP)
        self.assertFalse(r.get('ok'), r)
        self.assertEqual(self.osa(), [])

    def test_prompt_is_used_once(self):
        self.confirm('family: one')
        self.clock.t += 5
        self.assertTrue(self.send('family: one', confirmed=True, handle=GROUP)['ok'])
        self.clock.t += 5
        r = self.send('family: two', confirmed=True, handle=GROUP)
        self.assertFalse(r.get('ok'), r)
        self.assertEqual(len(self.osa()), 0)   # osa() shows the last runner only: the second never ran

    def test_prompt_expires(self):
        self.confirm('family: cute pup')
        self.clock.t += 200
        r = self.send('family: cute pup', confirmed=True, handle=GROUP)
        self.assertFalse(r.get('ok'), r)
        self.assertEqual(self.osa(), [])

    def test_needs_relay_all(self):
        self.raw['relayAll'] = False
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        r = self.send('family: cute pup')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])

    def test_name_shared_by_a_person_and_a_group_asks(self):
        self.m.rc.record_inbound('+15555550152', self.clock.t - 200, label='Family Smith')   # first name "Family"
        r = self.send('family: cute pup')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])

    def test_watcher_down_sends_nothing(self):
        self.watcher_up = False
        r = self.send('family: cute pup')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(r['reason'], 'group-unavailable')
        self.assertEqual(self.osa(), [])

    def test_dry_run_never_calls_messages(self):
        self.raw['dryRun'] = True
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.confirm('family: cute pup')
        self.clock.t += 5
        r = self.send('family: cute pup', confirmed=True, handle=GROUP)
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa(), [])

    def test_group_delivery_is_proven_in_that_chat(self):
        self.confirm('family: cute pup')
        self.clock.t += 5
        self.send('family: cute pup', confirmed=True, handle=GROUP)
        sent_at = self.clock.t
        self.db.add(SAM, text='cute pup', from_me=1, delivered=1, group='fam', when=sent_at + 1)
        rs = self.m.rc
        e = read_pending(rs.PENDING_DIR)[0]
        st, _err, rowid = rs.check_delivery_once(e['handle'], e['at'], db_path=self.db.path,
                                                 text_length=e['textLength'], chat_guid=e['chatGuid'])
        self.assertEqual(st, 'delivered')
        # a 1:1 message to Sam at the same time is NOT proof for the group
        st2, _e2, _r2 = rs.check_delivery_once(e['handle'], e['at'] + 100, db_path=self.db.path,
                                               text_length=e['textLength'], chat_guid=e['chatGuid'])
        self.assertEqual(st2, 'pending')


class Trash(Base):
    def setUp(self):
        super().setUp()
        self.raw['relayAll'] = True
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, self.raw)
        self.clock.t += 600
        self.m.rc.record_inbound(SAM, self.clock.t - 100, label='Sam')
        self.m.rc.record_inbound(GROUP, self.clock.t - 50, label='Family')
        self.m.rc.record_inbound('+15555550166', self.clock.t - 40, label='(555) 555-0166')

    def trash(self, quoted, conv=CONV):
        return self.m.host.handle_request({'type': 'reaction', 'word': 'block', 'quotedText': quoted, 'conversationId': conv},
                                          cfg=self.cfg(), runner=FakeRunner(), sleeper=self.sleeper,
                                          now=self.clock.now, poster=self.poster)

    def prompt(self):
        return [p for p, _ in self.posts if p.get('reason') == 'confirm-block'][-1]['detail']

    def muted(self):
        return self.cfg()['mute']

    def test_trash_asks_then_yes_mutes(self):
        r = self.trash('iMessage Sam: buy now')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertIn('Stop relaying Sam', self.prompt())
        self.assertIn('Reply yes within 2 min', self.prompt())
        self.assertNotIn(SAM, self.muted())
        self.clock.t += 5
        self.send('yes')
        self.assertIn(SAM, self.muted())
        self.assertEqual(self.osa(), [])
        self.assertNotIn('buy now', read_log())

    def test_slash_block_yes_and_thumbs_up_confirm(self):
        self.trash('Sam: buy now')
        self.clock.t += 5
        self.send('/block yes')
        self.assertIn(SAM, self.muted())
        self.m.rc.write_json_atomic(self.m.rc.CONFIG_PATH, dict(self.raw, mute=[]))
        self.trash('Sam: buy now')
        self.clock.t += 5
        self.m.host.handle_request({'type': 'reaction', 'word': 'yes', 'conversationId': CONV}, cfg=self.cfg(),
                                   runner=FakeRunner(), sleeper=self.sleeper, now=self.clock.now, poster=self.poster)
        self.assertIn(SAM, self.muted())

    def test_no_or_thumbs_down_cancels_and_window_expires(self):
        self.trash('Sam: buy now')
        self.clock.t += 5
        self.send('no')
        self.assertNotIn(SAM, self.muted())
        self.trash('Sam: buy now')
        self.clock.t += 200
        self.send('yes')
        self.assertNotIn(SAM, self.muted())
        self.assertEqual(self.osa(), [])

    def test_group_line_blocks_the_sender_not_the_group(self):
        self.trash('Family \u00b7 Sam: buy now')
        self.assertIn('Stop relaying Sam', self.prompt())
        self.clock.t += 5
        self.send('yes')
        self.assertIn(SAM, self.muted())
        self.assertNotIn(GROUP, self.muted())

    def test_unknown_number_prompt_is_masked(self):
        self.trash('(555) 555-0166: vote now')
        self.assertIn('+1 \u2022\u2022\u20220166 (\u26a0\ufe0f not in Contacts)', self.prompt())
        self.assertNotIn('555-0166', self.prompt())

    def test_unplaceable_bubble_mutes_nobody(self):
        r = self.trash('just some text')
        self.assertTrue(r.get('needsTarget'), r)
        self.clock.t += 5
        self.send('yes')
        self.assertEqual(self.muted(), [])

    def test_other_conversation_ignored(self):
        r = self.trash('Sam: buy now', conv='9999-0000')
        self.assertFalse(r.get('ok'))
        self.assertFalse([p for p, _ in self.posts if p.get('reason') == 'confirm-block'])


class Spam(test_watch.Base):
    def setUp(self):
        super().setUp()
        self.cfg['relayAll'] = True
        self.cfg['spamMode'] = 'tag'      # v1.4.6: these are the v1.4.5 'tag' rules (follow-messages: test_v146)
        con = sqlite3.connect(self.db.path)
        con.execute('ALTER TABLE chat ADD COLUMN is_filtered INTEGER DEFAULT 0')
        con.execute('ALTER TABLE message ADD COLUMN is_spam INTEGER DEFAULT 0')
        con.commit()
        con.close()
        self.w = self.new_watcher()
        self.w.tick()

    def msgs(self):
        return [p for _, p, _ in self.poster.calls if p['type'] == 'message']

    def test_not_in_contacts_is_tagged_contacts_are_not(self):
        self.db.add('+15555550166', text='vote for me')
        self.db.add(test_watch.ALLOWED, text='hi from a contact')
        self.w.tick()
        tags = {p['handle']: bool(p.get('unknown')) for p in self.msgs()}
        self.assertEqual(tags, {'+15555550166': True, test_watch.ALLOWED: False})
        self.assertTrue(all(p['text'] for p in self.msgs()), 'never hidden')

    def test_flagged_by_messages_is_tagged(self):
        self.db.add(test_watch.ALLOWED, text='hi')
        con = sqlite3.connect(self.db.path)
        con.execute('UPDATE chat SET is_filtered = 2')
        con.commit()
        con.close()
        self.w.tick()
        self.assertTrue(self.msgs()[-1].get('unknown'))

    def test_off(self):
        self.cfg['spamMode'] = 'off'
        self.db.add('+15555550166', text='vote for me')
        self.w.tick()
        self.assertFalse(self.msgs()[-1].get('unknown'))

    def test_group_messages_unchanged(self):
        self.db.add('+15555550166', text='hi all', group='fam', display_name='Family', members=['+15555550166', SAM])
        self.w.tick()
        p = self.msgs()[-1]
        self.assertTrue(p.get('group'))
        self.assertNotIn('unknown', p)

    def test_routine_renders_the_tag(self):
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('relay_post', os.path.join(here, '..', '..', 'routine', 'relay_post.py'))
        rp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rp)
        out = rp.format_event({'type': 'message', 'id': 'a', 'label': '(555) 555-0166', 'text': 'vote', 'unknown': True})
        self.assertEqual(out['lines'][0]['text'], '\u26a0\ufe0f (555) 555-0166 \u00b7 not a contact: vote')
        out = rp.format_event({'type': 'message', 'id': 'b', 'label': 'Sam', 'text': 'hi'})
        self.assertEqual(out['lines'][0]['text'], 'Sam: hi')


class Batching(test_watch.Base):
    """v1.4.5 speed: texts waiting together go out as ONE webhook (one routine run on the box)."""
    def setUp(self):
        super().setUp()
        self.cfg.pop('batchWebhooks', None)          # the default: on
        self.cfg['relayAll'] = True
        self.w = self.new_watcher()
        self.w.tick()

    def test_two_texts_in_one_tick_are_one_batch_webhook(self):
        self.db.add(test_watch.ALLOWED, text='one')
        self.db.add(SAM, text='two', group='fam', display_name='Family', members=[SAM, test_watch.OTHER])
        c = self.w.tick()
        self.assertEqual(self.poster.types(), ['batch'])
        p = self.poster.calls[0][1]
        self.assertEqual([m['text'] for m in p['messages']], ['one', 'two'])
        self.assertEqual(p['count'], 2)
        self.assertTrue(p['untrusted'] and p['id'])
        self.assertEqual(p['messages'][1]['group']['label'], 'Family')      # inbound groups exactly as before
        self.assertEqual(len({m['id'] for m in p['messages']}), 2)

    def test_a_single_text_is_still_a_plain_message(self):
        self.db.add(test_watch.ALLOWED, text='solo')
        self.w.tick()
        self.assertEqual(self.poster.types(), ['message'])

    def test_failed_batch_is_retried_whole_and_never_duplicated(self):
        self.poster.ok = False
        self.db.add(test_watch.ALLOWED, text='one')
        self.db.add(test_watch.ALLOWED, text='two')
        self.w.tick()
        self.poster.ok = True
        self.poster.calls.clear()
        self.now += 400
        self.w.tick()
        self.w.tick()
        texts = [m['text'] for _, p, _ in self.poster.calls for m in (p.get('messages') or [p]) if p['type'] in ('batch', 'message')]
        self.assertEqual(texts, ['one', 'two'])

    def test_off_switch(self):
        self.cfg['batchWebhooks'] = False
        self.db.add(test_watch.ALLOWED, text='one')
        self.db.add(test_watch.ALLOWED, text='two')
        self.w.tick()
        self.assertEqual(self.poster.types(), ['message', 'message'])

    def test_batch_formats_in_the_routine_helper(self):
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location('relay_post', os.path.join(here, '..', '..', 'routine', 'relay_post.py'))
        rp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rp)
        self.db.add(test_watch.ALLOWED, text='one')
        self.db.add('+15555550166', text='two')
        self.w.tick()
        out = rp.format_event(self.poster.calls[0][1])
        self.assertEqual([ln['text'] for ln in out['lines']],
                         ['Test: one', '\u26a0\ufe0f (555) 555-0166 \u00b7 not a contact: two'])




if __name__ == '__main__':
    unittest.main()
