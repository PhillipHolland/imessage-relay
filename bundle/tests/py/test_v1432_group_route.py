"""v1.4.32: a reply aimed at a group never goes to a member's 1:1.

Hollands is the group. Brian Holland (+17703770130) is a member and also has a 1:1.
"Hollands: …" and a quote of a Hollands line (even one X shortened to "Brian: …") name the group.
"Brian: …" with no group signal still names the 1:1. The same words in both chats ask.
A group chat id lookup never returns Brian's 1:1 guid (iMessage;-;…).
"""
import sqlite3
import unittest

from helpers import fresh, tmpdir
import test_watch

T0 = 1_800_000_000.0
BRIAN = '+17703770130'
GROUP = 'group:chat853'
BODY = 'dinner at the house tonight'


class GroupRoute(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc
        self.cfg = self.rc.sanitize_config({
            'relayConversationId': '1111-2222',
            'relayAll': True,
            'contacts': [{'name': 'Brian Holland', 'handle': BRIAN}],
        })
        self.roster = [
            {'handle': GROUP, 'label': 'Hollands', 'lastInboundAt': T0 - 30},
            {'handle': BRIAN, 'label': 'Brian', 'lastInboundAt': T0 - 400, 'inboundCount': 30},
        ]

    def route(self, text, quoted='', quote_label=''):
        return self.rc.route_reply(text, quoted, self.cfg, self.roster, T0, quote_label=quote_label)

    def test_group_prefix_is_the_group_and_brian_prefix_is_the_one_to_one(self):
        g = self.route('Hollands: test')
        self.assertEqual((g['contact']['handle'], g['by'], g.get('group')), (GROUP, 'prefix', True))
        b = self.route('Brian: test')
        self.assertEqual((b['contact']['handle'], b['by'], b.get('group')), (BRIAN, 'prefix', None))
        self.assertNotIn(';-;', str(g['contact']['handle']))

    def test_full_group_line_and_a_different_separator_name_the_group(self):
        for quoted in ('Hollands \u00b7 Brian: ' + BODY,
                       'iMessage Hollands \u2022 Brian Holland: ' + BODY,
                       'Hollands - Brian: ' + BODY):
            r = self.route('sounds good', quoted=quoted)
            self.assertEqual(r.get('contact', {}).get('handle'), GROUP, quoted)
            self.assertTrue(r.get('group'), quoted)

    def test_speaker_only_preview_follows_the_recorded_group_body(self):
        self.rc.record_quote_keys(BODY, GROUP, at=T0 - 20)
        for quoted in ('Brian: ' + BODY, 'Brian Holland: ' + BODY):
            r = self.route('sounds good', quoted=quoted)
            self.assertEqual((r['contact']['handle'], r.get('group')), (GROUP, True), quoted)

    def test_header_above_a_speaker_preview_names_the_group(self):
        r = self.route('sounds good', quoted='Brian: ' + BODY, quote_label='Hollands')
        self.assertEqual((r['contact']['handle'], r.get('group')), (GROUP, True))

    def test_same_words_in_the_group_and_the_one_to_one_ask(self):
        self.rc.record_quote_keys(BODY, GROUP, at=T0 - 20)
        self.rc.record_quote_keys(BODY, BRIAN, at=T0 - 10)
        r = self.route('sounds good', quoted='Brian: ' + BODY, quote_label='Hollands')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertNotIn('contact', r)
        self.assertNotEqual(r.get('contact', {}).get('handle'), BRIAN)

    def test_a_real_brian_quote_stays_on_the_one_to_one(self):
        self.rc.record_quote_keys(BODY, BRIAN, at=T0 - 10)
        r = self.route('sounds good', quoted='Brian: ' + BODY, quote_label='Brian')
        self.assertEqual((r['contact']['handle'], r.get('group')), (BRIAN, None))
        # A Hollands header on a body that was relayed only in the 1:1 asks. It does not text Brian.
        r = self.route('sounds good', quoted='Brian: ' + BODY, quote_label='Hollands')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertNotIn('contact', r)

    def test_mentioning_the_group_in_a_sentence_is_not_a_group_line(self):
        r = self.route('ok', quoted='see you at Hollands tonight, bring dessert')
        self.assertNotEqual(r.get('contact', {}).get('handle'), GROUP)

    def test_a_shared_name_still_asks(self):
        self.roster.append({'handle': '+15555550199', 'label': 'Hollands Smith', 'lastInboundAt': T0 - 50})
        r = self.route('Hollands: test')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertNotIn('contact', r)


class GroupChatId(test_watch.Base):
    def test_lookup_skips_a_newer_one_to_one_that_shares_the_identifier(self):
        con = sqlite3.connect(self.db.path)
        con.execute('INSERT INTO chat (guid, style, chat_identifier, display_name) VALUES (?,?,?,?)',
                    ('iMessage;+;chat853', 43, 'chat853', 'Hollands'))
        gid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        con.execute('INSERT INTO chat (guid, style, chat_identifier, display_name) VALUES (?,?,?,?)',
                    ('iMessage;-;+17703770130', 45, 'chat853', None))
        one = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        brian = con.execute("SELECT ROWID FROM handle WHERE id = '+17703770130'").fetchone()
        if not brian:
            con.execute('INSERT INTO handle (id, service) VALUES (?, ?)', ('+17703770130', 'iMessage'))
            hid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        else:
            hid = brian[0]
        con.execute('INSERT INTO chat_handle_join VALUES (?, ?)', (gid, hid))
        con.execute('INSERT INTO chat_handle_join VALUES (?, ?)', (gid, hid))
        # The 1:1 row is newer. It must not win.
        con.execute('INSERT INTO message (guid, text, handle_id, date, is_from_me) VALUES (?,?,?,?,?)',
                    ('m-old', 'x', hid, 1, 0))
        old = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        con.execute('INSERT INTO message (guid, text, handle_id, date, is_from_me) VALUES (?,?,?,?,?)',
                    ('m-new', 'y', hid, 9, 0))
        new = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        con.execute('INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)', (gid, old))
        con.execute('INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)', (one, new))
        con.commit()
        con.close()
        got = self.w.group_guid('group:chat853')
        self.assertTrue(got.get('ok'), got)
        self.assertEqual(got['guid'], 'iMessage;+;chat853')
        self.assertNotIn(';-;', got['guid'])

    def test_only_a_one_to_one_is_not_a_group_destination(self):
        con = sqlite3.connect(self.db.path)
        con.execute('INSERT INTO chat (guid, style, chat_identifier, display_name) VALUES (?,?,?,?)',
                    ('iMessage;-;+17703770130', 45, '+17703770130', None))
        con.commit()
        con.close()
        got = self.w.group_guid('group:+17703770130')
        self.assertFalse(got.get('ok'), got)
        self.assertNotIn('guid', got)

    def test_a_message_joined_to_both_chats_is_relayed_as_the_group(self):
        self.cfg['relayAll'] = True
        self.w = self.new_watcher()
        self.w.tick()
        brian = '+17703770130'
        mid = self.db.add(brian, text=BODY, group='hollands', display_name='Hollands',
                          members=[brian, '+15555550199'], when=self.now)
        con = sqlite3.connect(self.db.path)
        con.execute('INSERT INTO chat (guid, style, chat_identifier, display_name) VALUES (?,?,?,?)',
                    ('iMessage;-;' + brian, 45, brian, None))
        one = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        gid = con.execute('SELECT chat_id FROM chat_message_join WHERE message_id = ?', (mid,)).fetchone()[0]
        con.execute('DELETE FROM chat_message_join WHERE message_id = ?', (mid,))
        con.execute('INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)', (one, mid))
        con.execute('INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)', (gid, mid))
        con.commit()
        con.close()
        self.w.tick()
        msgs = [p for _, p, _ in self.poster.calls if p.get('type') == 'message']
        self.assertTrue(msgs, self.poster.types())
        self.assertEqual(msgs[-1].get('group', {}).get('label'), 'Hollands')


if __name__ == '__main__':
    unittest.main()
