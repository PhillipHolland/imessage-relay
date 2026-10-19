"""Regression tests: one+ per shipped change. The v1.3.4 items (A, B) pass on 1.3.4 and fail on the
frozen 1.3.3 checkout; the v1.3.5 items (C1/W1/W4 + nits, classes suffixed W1/W4/Nits/W) pass on 1.3.5
and fail on a 1.3.4 checkout (run this same file against that tree to confirm). Items:
  A   conversational addressing in route_reply (text/tell/send/@/comma/dash forms)
  B   unique id on every webhook payload + relay_post --payload-file
  C1  confirmed /yes carries + requires the handle; never re-guesses a recipient
  W1  directive route type + /yes confirmation when the target differs / is a dict word
  W4  verb + phone/email refuses when never-texted; never falls back to the last sender
  nits  /add dry-run doesn't mutate; /add <Name> mismatch refuses; tapbacks don't move last-sender
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

from helpers import fresh, tmpdir, FakeRunner, ChatDb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

A = '+15555550123'       # Robin
S = '+15555550124'       # Sam
R1 = '+15555550125'      # Alex Rivera
R2 = '+15555550126'      # Alex Jordan
OWNER = '+15550000001'
CONV = '1111-2222'
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


# ---------- item A: conversational addressing ----------
class Directive(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc
        self.c = self.rc.sanitize_config({'relayConversationId': CONV, 'contacts': [
            {'name': 'Robin Smith', 'handle': A},
            {'name': 'Sam Alpha', 'handle': S},
            {'name': 'Alex Rivera', 'handle': R1},
            {'name': 'Alex Jordan', 'handle': R2}]})

    def route(self, text, quoted=''):
        return self.rc.route_reply(text, quoted, self.c, self.rc.load_roster(), NOW)

    def ok(self, text, label, body):
        r = self.route(text)
        self.assertNotIn('needsTarget', r, 'expected a send for %r, got %r' % (text, r))
        self.assertEqual((r['contact']['label'], r['body']), (label, body), text)

    # each supported form
    def test_text_name_space(self):
        self.ok('text Robin running late', 'Robin', 'running late')

    def test_text_name_colon(self):
        self.ok('text Robin: running late', 'Robin', 'running late')

    def test_tell_name(self):
        self.ok('tell Sam dinner at 7', 'Sam', 'dinner at 7')

    def test_send_name(self):
        self.ok('send Robin hi there', 'Robin', 'hi there')

    def test_message_name(self):
        self.ok('message Sam yo', 'Sam', 'yo')

    def test_send_a_message_to_period_newline(self):
        # the conversational-addressing case: "Send a message to Robin.\n\nTest" -> body "Test"
        self.ok('Send a message to Robin.\n\nTest', 'Robin', 'Test')

    def test_send_a_text_to(self):
        self.ok('send a text to Sam on my way', 'Sam', 'on my way')

    def test_send_to(self):
        self.ok('send to Robin yes', 'Robin', 'yes')

    def test_reply_to(self):
        self.ok('reply to Sam sounds good', 'Sam', 'sounds good')

    def test_to_name_colon(self):
        self.ok('to Robin: see you', 'Robin', 'see you')

    def test_at_name(self):
        self.ok('@Sam where are you', 'Sam', 'where are you')

    def test_name_comma(self):
        self.ok('Robin, are you free?', 'Robin', 'are you free?')

    def test_name_dash(self):
        self.ok('Sam - call me', 'Sam', 'call me')

    def test_inner_newline_preserved(self):
        self.ok('text Robin line one\nline two', 'Robin', 'line one\nline two')

    def test_first_name_ok_when_unique(self):
        self.ok('text Robin hey', 'Robin', 'hey')

    # negatives: ordinary prose must NOT be captured as a directive
    def test_tell_me_when_you_land_goes_to_recent(self):
        self.rc.record_inbound(S, NOW - 60, label='Sam')
        r = self.route('tell me when you land')
        self.assertEqual((r['contact']['label'], r['body'], r['by']), ('Sam', 'tell me when you land', 'recent'))

    def test_send_it_tomorrow_goes_to_recent(self):
        self.rc.record_inbound(A, NOW - 60, label='Robin')
        r = self.route('send it tomorrow')
        self.assertEqual((r['contact']['label'], r['by']), ('Robin', 'recent'))
        self.assertEqual(r['body'], 'send it tomorrow')

    def test_text_me_later_goes_to_recent(self):
        self.rc.record_inbound(S, NOW - 60, label='Sam')
        r = self.route('text me later')
        self.assertEqual((r['contact']['label'], r['body'], r['by']), ('Sam', 'text me later', 'recent'))

    def test_unknown_name_is_not_a_directive(self):
        # "text Dana ..." (Dana not a contact): no recent sender -> needs_target, never a wrong contact
        r = self.route('text Dana hello')
        self.assertTrue(r.get('needsTarget'), r)

    def test_ambiguous_first_name_needs_target(self):
        r = self.route('text Alex hi')          # two Alexes
        self.assertTrue(r.get('needsTarget'), r)
        self.assertNotIn('contact', r)

    def test_ambiguous_full_name_resolves(self):
        self.ok('text Alex Rivera hi', 'Alex R.', 'hi')

    def test_empty_body_asks_what_to_send(self):
        r = self.route('text Robin')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertIn('What should I send to Robin', r['error'])

    def test_bare_name_no_separator_is_not_a_directive(self):
        # "Robin hey" (no verb, no separator) is NOT a directive; falls through to recent
        self.rc.record_inbound(S, NOW - 60, label='Sam')
        r = self.route('Robin hey')
        self.assertEqual(r['by'], 'recent')

    def test_hint_mentions_both_forms(self):
        r = self.route('no name here and nobody recent')
        self.assertTrue(r.get('needsTarget'))
        self.assertIn('text ', r['error'])        # the new hint shows the "text <Name>" form


# ---------- item B: unique webhook ids + --payload-file ----------
class EventId(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc

    def test_status_payload_gets_unique_id(self):
        a = {'type': 'needs_target', 'detail': 'x'}
        b = {'type': 'needs_target', 'detail': 'x'}
        self.rc.ensure_event_id(a)
        self.rc.ensure_event_id(b)
        self.assertTrue(a['id'] and b['id'])
        self.assertNotEqual(a['id'], b['id'], 'distinct episodes get distinct ids')
        self.assertTrue(a['id'].startswith('needs_target:'))

    def test_existing_id_preserved(self):
        p = {'type': 'message', 'id': 'rowid-42'}
        self.rc.ensure_event_id(p)
        self.assertEqual(p['id'], 'rowid-42')

    def test_status_kind_included(self):
        p = {'type': 'status', 'status': 'fda_missing'}
        self.rc.ensure_event_id(p)
        self.assertTrue(p['id'].startswith('status:fda_missing:'))


class PayloadFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()

    def test_payload_file_is_read_and_deleted(self):
        pf = os.path.join(self.tmp, 'event.json')
        with open(pf, 'w') as f:
            json.dump({'type': 'needs_target', 'detail': 'Who is this for?'}, f)
        req = os.path.join(self.tmp, 'req.json')
        with open(req, 'w') as f:
            json.dump({'conversation_id': CONV}, f)   # NO body/payload in the request itself
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rp.main(['--in', req, '--payload-file', pf])
        out = json.loads(buf.getvalue())
        # payload was spliced in: it formatted a needs_target line (then failed only for lack of xchat ctx)
        self.assertEqual(out.get('posted_kind'), 'needs_target')
        self.assertFalse(os.path.exists(pf), 'payload file deleted right after read')
        self.assertFalse(os.path.exists(req), '--in file deleted right after read')

    def _run_bad_payload(self, raw):
        pf = os.path.join(self.tmp, 'bad.json')
        with open(pf, 'w') as f:
            f.write(raw)
        req = os.path.join(self.tmp, 'req2.json')
        with open(req, 'w') as f:
            json.dump({'conversation_id': CONV}, f)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_code = rp.main(['--in', req, '--payload-file', pf])
        out = json.loads(buf.getvalue())
        self.assertEqual(out.get('action'), 'error')
        self.assertFalse(os.path.exists(pf), 'bad payload file is still deleted')
        # body-free: the refusal never echoes the file contents
        self.assertNotIn('secret', json.dumps(out))
        return out, rc_code

    def test_payload_file_must_be_one_json_object(self):
        self._run_bad_payload('{"text": "secret"} {"text": "second object"}')   # two objects
        self._run_bad_payload('["secret", "a list is not an object"]')          # a JSON list
        self._run_bad_payload('"secret just a string"')                         # a bare value
        self._run_bad_payload('{"text": "secret", truncat')                     # truncated / garbage


if __name__ == '__main__':
    unittest.main()


# ---------- item 2: owner commands ----------
class _HostBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.cfgd = {'contacts': [{'name': 'Sam Alpha', 'handle': S}, {'name': 'Robin Smith', 'handle': A}],
                     'relayConversationId': CONV, 'webhookUrl': 'https://x.invalid/h', 'webhookToken': 't',
                     'ownerHandle': OWNER, 'ownerHandles': [OWNER], 'quietHours': None, 'dryRun': False}
        self._write()
        self.posts = []
        self.runner = FakeRunner([(0, '')] * 40)
        self.t = NOW

    def _write(self):
        with open(self.m.rc.CONFIG_PATH, 'w') as f:
            json.dump(self.cfgd, f)

    def send(self, text, quoted='', t=None):
        if t is not None:
            self.t = t
        cfg = self.m.rc.load_config()
        return self.m.host.handle_request(
            {'type': 'send', 'text': text, 'quotedText': quoted, 'conversationId': CONV, 'ts': ''},
            cfg=cfg, runner=self.runner, sleeper=lambda x: None, now=lambda: self.t,
            poster=lambda u, p, token='', timeout=4: self.posts.append(p) or True)

    def osa(self):
        return [c for c in self.runner.calls if c[0] == '/usr/bin/osascript']


class OwnerCommands(_HostBase):
    def test_pause_and_resume_run_even_while_paused(self):
        r = self.send('/pause')
        self.assertTrue(r.get('paused'))
        self.assertTrue(self.m.rc.load_config()['paused'])
        # a normal reply is refused while paused
        self.assertEqual(self.send('ok')['error'], 'paused')
        # /resume still works while paused (parsed before the paused check)
        r = self.send('/resume')
        self.assertFalse(r.get('paused'))
        self.assertFalse(self.m.rc.load_config()['paused'])

    def test_help_posts_and_sends_nothing(self):
        n = len(self.osa())
        r = self.send('/help')
        self.assertEqual(r.get('command'), 'help')
        self.assertEqual(len(self.osa()), n)
        self.assertIn('/who', self.posts[-1]['detail'])

    def test_who_reports_target_and_no_text(self):
        self.m.rc.record_inbound(S, NOW - 120, label='Sam')
        r = self.send('/who', t=NOW)
        self.assertEqual(r.get('command'), 'who')
        d = self.posts[-1]['detail']
        self.assertIn('Sam', d)
        self.assertNotIn('text', json.dumps(self.posts).replace('texters', '').replace('text Sam', ''))

    def test_add_stages_last_unknown_and_confirms(self):
        # an unknown number texted in (roster, not a contact)
        self.m.rc.record_inbound('+15555559999', NOW - 30, label='+1 (555) 555-9999')
        r = self.send('/add Kent', t=NOW)
        self.assertTrue(r.get('needsConfirm'))
        self.assertIn('9999', self.posts[-1]['detail'])
        self.assertIn('Kent', self.posts[-1]['detail'])
        # confirm
        r = self.send('/add yes', t=NOW + 10)
        self.assertEqual(r.get('added'), 'Kent')
        cs = self.m.rc.load_config()['contacts']
        self.assertTrue(any(c['name'] == 'Kent' and c['handle'] == '+15555559999' for c in cs))

    def test_add_without_name_asks(self):
        r = self.send('/add')
        self.assertTrue(r.get('needsTarget'))
        self.assertEqual(self.osa(), [])


# ---------- item 1: should_notify + reply_to threading ----------
class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout.encode() if isinstance(stdout, str) else stdout
        self.returncode = 0
        self.stderr = b''


def _enc_runner(seen):
    def runner(argv, input=b''):
        seen.append(json.loads(input.decode()))
        return _FakeProc(json.dumps({'message_id': 'm', 'encoded_message_create_event': 'E',
                                     'encoded_message_event_signature': 'S'}))
    return runner


class NotifyAndReplyTo(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.dedupe = os.path.join(self.tmp, 'seen.json')

    def _xchat(self):
        return {'events': [], 'conversation_key_events': [], 'signing_keys': []}

    def test_relayed_message_notifies_and_does_not_thread(self):
        seen = []
        r = rp.process({'payload': {'type': 'message', 'id': 'g1', 'label': 'Sam', 'text': 'hi'},
                        'conversation_id': 'C', 'xchat': self._xchat(), 'dedupe_path': self.dedupe},
                       runner=_enc_runner(seen))
        self.assertEqual(r['action'], 'send')
        self.assertTrue(r['sends'][0]['should_notify'], 'a relayed text pings the owner')
        self.assertIsNone(r['sends'][0]['reply_to'])

    def test_confirmation_is_silent(self):
        seen = []
        r = rp.process({'payload': {'type': 'sent', 'id': 's1', 'label': 'Sam'},
                        'conversation_id': 'C', 'xchat': self._xchat(), 'dedupe_path': self.dedupe},
                       runner=_enc_runner(seen))
        self.assertFalse(r['sends'][0]['should_notify'], 'a confirmation never pings')

    def test_confirmation_threads_under_owner_reply_when_reply_to_given(self):
        seen = []
        r = rp.process({'payload': {'type': 'sent', 'id': 's2', 'label': 'Sam', 'reply_to': 'EVT-99'},
                        'conversation_id': 'C', 'xchat': self._xchat(), 'dedupe_path': self.dedupe},
                       runner=_enc_runner(seen))
        self.assertEqual(r['sends'][0]['reply_to'], 'EVT-99')
        self.assertEqual(seen[-1].get('reply_to'), 'EVT-99', 'reply_to reaches xchat_lite on stdin')


# ---------- item 3: confirm-when-guessed ----------
class ConfirmGuessed(_HostBase):
    def send_c(self, text, confirmed=False, t=None, handle=None, **over):
        if t is not None:
            self.t = t
        cfg = self.m.rc.load_config()
        cfg.update(over)
        msg = {'type': 'send', 'text': text, 'conversationId': CONV, 'ts': ''}
        if confirmed:
            msg['confirmed'] = True
        if handle is not None:
            msg['handle'] = handle        # C1: /yes carries the confirmed handle
        return self.m.host.handle_request(
            msg, cfg=cfg, runner=self.runner, sleeper=lambda x: None, now=lambda: self.t,
            poster=lambda u, p, token='', timeout=4: self.posts.append(p) or True)

    def test_stale_recent_needs_confirm(self):
        self.m.rc.record_inbound(S, NOW - 20 * 60, label='Sam')   # >10 min ago -> risky
        n = len(self.osa())
        r = self.send_c('ok', t=NOW)
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r.get('label'), 'Sam')
        self.assertEqual(len(self.osa()), n, 'nothing texted until confirmed')

    def test_competition_within_15min_needs_confirm(self):
        self.m.rc.record_inbound(S, NOW - 60, label='Sam')        # most recent
        self.m.rc.record_inbound(A, NOW - 120, label='Robin')     # another contact within 15 min
        r = self.send_c('see you', t=NOW)
        self.assertTrue(r.get('needsConfirm'), r)

    def test_confirmed_flag_sends(self):
        self.m.rc.record_inbound(S, NOW - 20 * 60, label='Sam')
        r = self.send_c('ok', confirmed=True, handle=S, t=NOW)
        self.assertTrue(r.get('sent'), r)
        self.assertEqual(self.osa()[-1][3], S, 'sent to the confirmed handle')
        self.assertEqual(len(self.osa()), 1)

    def test_confirmed_without_handle_refuses(self):
        # C1: a confirmed reply with no handle must never re-guess a recipient.
        self.m.rc.record_inbound(S, NOW - 20 * 60, label='Sam')
        r = self.send_c('ok', confirmed=True, t=NOW)
        self.assertFalse(r.get('ok'))
        self.assertEqual(len(self.osa()), 0)

    def test_confirmed_goes_to_confirmed_handle_not_drifted_recent(self):
        # C1 core: Sam guessed, then Robin texts inside the window; /yes must go to Sam (or refuse), never Robin.
        self.m.rc.record_inbound(S, NOW - 11 * 60, label='Sam')
        r = self.send_c('running late', t=NOW)
        self.assertTrue(r.get('needsConfirm'), r)
        self.m.rc.record_inbound(A, NOW + 60, label='Robin')          # Robin now most recent
        r2 = self.send_c('running late', confirmed=True, handle=S, t=NOW + 90)
        self.assertTrue(r2.get('sent'), r2)
        self.assertEqual(self.osa()[-1][3], S, 'went to the confirmed handle, not the drifted recent')

    def test_off_never_confirms(self):
        self.m.rc.record_inbound(S, NOW - 20 * 60, label='Sam')
        r = self.send_c('ok', t=NOW, confirmGuessed='off')
        self.assertTrue(r.get('sent'), r)

    def test_always_confirms_even_fresh(self):
        self.m.rc.record_inbound(S, NOW - 30, label='Sam')        # fresh, single contact -> not risky
        r = self.send_c('ok', t=NOW, confirmGuessed='always')
        self.assertTrue(r.get('needsConfirm'), r)

    def test_fresh_single_recent_sends_under_default_risky(self):
        self.m.rc.record_inbound(S, NOW - 30, label='Sam')        # fresh, only one recent contact
        r = self.send_c('ok', t=NOW)
        self.assertTrue(r.get('sent'), r)

    def test_guess_expired_signal_posts_body_free_line(self):
        cfg = self.m.rc.load_config()
        r = self.m.host.handle_request(
            {'type': 'signal', 'status': 'guess_expired', 'label': 'Sam'},
            cfg=cfg, now=lambda: NOW,
            poster=lambda u, p, token='', timeout=4: self.posts.append(p) or True)
        self.assertTrue(r.get('ok'))
        self.assertEqual(self.posts[-1]['type'], 'guess_expired')
        self.assertEqual(self.posts[-1]['label'], 'Sam')


# ---------- items 4 & 5: metadata placeholders + incoming tapbacks (watcher) ----------
class _WatchBase(unittest.TestCase):
    ALLOWED = '+15555550123'
    OWNER = '+15555550199'

    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.db.add(self.ALLOWED, text='old history')
        self.cfg = {'contacts': [{'name': 'Robin Smith', 'handle': self.ALLOWED}], 'relayConversationId': CONV,
                    'webhookUrl': 'https://x.invalid/h', 'webhookToken': 't', 'ownerHandle': self.OWNER,
                    'quietHours': None}
        self.now = NOW
        self.posts = []
        self.w = self.m.watch.Watcher(
            poster=lambda u, p, token='': self.posts.append(json.loads(json.dumps(p))) or True,
            runner=FakeRunner(), now=lambda: self.now,
            config_loader=lambda: self.m.rc.sanitize_config(self.cfg), sleeper=lambda s: None)

    def texts(self):
        return [p['text'] for p in self.posts if p.get('type') == 'message']


class Tapbacks(_WatchBase):
    def test_tapback_add_and_remove_and_emoji(self):
        self.w.tick()
        # a message from the owner for the tapback to point at (your message)
        g_owner = self.db.add(self.ALLOWED, text='dinner?', from_me=1)
        owner_guid = None
        import sqlite3 as _s
        con = _s.connect(self.db.path)
        owner_guid = con.execute('SELECT guid FROM message WHERE ROWID=?', (g_owner,)).fetchone()[0]
        con.close()
        self.db.add(self.ALLOWED, text='Loved "dinner?"', assoc=2000, assoc_guid='p:0/' + owner_guid)  # love add
        self.db.add(self.ALLOWED, text='x', assoc=2001)                      # like, unknown target
        self.db.add(self.ALLOWED, text='x', assoc=3000)                      # love removed
        self.db.add(self.ALLOWED, text='x', assoc=2006, assoc_emoji='🔥')     # custom emoji
        self.w.tick()
        t = self.texts()
        self.assertIn('❤️ your message', t)
        self.assertIn('👍 a message', t)
        self.assertIn('removed ❤️ a message', t)
        self.assertIn('🔥 a message', t)
        # never forward the reacted row's own text
        self.assertNotIn('Loved', json.dumps(self.posts))

    def test_tapback_posts_with_reaction_flag(self):
        self.w.tick()
        self.db.add(self.ALLOWED, text='x', assoc=2003)                      # laughed
        self.w.tick()
        p = [p for p in self.posts if p.get('type') == 'message'][-1]
        self.assertTrue(p.get('reaction'))
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'routine'))
        import relay_post as _rp
        self.assertEqual(_rp._message_line(p), 'Robin 😂 a message')          # space, not colon


class Placeholders(_WatchBase):
    def test_photo_video_file_sticker_voice(self):
        self.w.tick()
        self.db.add(self.ALLOWED, text=None, attach={'mime': 'image/jpeg'})
        self.db.add(self.ALLOWED, text=None, attach={'mime': 'video/mp4'})
        self.db.add(self.ALLOWED, text=None, attach={'transfer_name': 'budget.pdf', 'mime': 'application/pdf'})
        self.db.add(self.ALLOWED, text=None, attach={'is_sticker': True, 'mime': 'image/png'})
        self.db.add(self.ALLOWED, text=None, is_audio=1, attach={'mime': 'audio/x-m4a'})
        self.db.add(self.ALLOWED, text=None, attach=[{'mime': 'image/jpeg'}, {'mime': 'image/png'}])
        self.w.tick()
        t = self.texts()
        self.assertIn('📷 Photo', t)
        self.assertIn('🎥 Video', t)
        self.assertIn('📎 budget.pdf', t)
        self.assertIn('🧷 Sticker', t)
        self.assertIn('🎤 Voice message', t)
        self.assertIn('📷 2 photos', t)

    def test_unsent_notice_no_old_text(self):
        self.w.tick()
        self.db.add(self.ALLOWED, text=None, date_retracted=123)
        self.w.tick()
        self.assertIn('🚫 Unsent a message', self.texts())

    def test_edited_marks_without_old_text(self):
        self.w.tick()
        self.db.add(self.ALLOWED, text='new wording', date_edited=123)
        self.w.tick()
        self.assertIn('new wording  ✏️ (edited)', self.texts())


# ---------- item 7: first bare reply after a quiet-hours digest must name a person ----------
class PostDigest(_HostBase):
    def send_o(self, text, t=None, **over):
        if t is not None:
            self.t = t
        cfg = self.m.rc.load_config()
        cfg.update({'confirmGuessed': 'off'})   # isolate the digest gate from the item-3 guess prompt
        cfg.update(over)
        return self.m.host.handle_request(
            {'type': 'send', 'text': text, 'conversationId': CONV, 'ts': ''},
            cfg=cfg, runner=self.runner, sleeper=lambda x: None, now=lambda: self.t,
            poster=lambda u, p, token='', timeout=4: self.posts.append(p) or True)

    def test_no_digest_bare_reply_sends(self):
        self.m.rc.record_inbound(S, NOW - 30, label='Sam')
        r = self.send_o('ok', t=NOW)
        self.assertTrue(r.get('sent'), r)

    def test_first_bare_reply_after_digest_needs_target(self):
        self.m.rc.record_inbound(S, NOW - 30, label='Sam')
        self.m.rc.write_digest_marker(NOW - 5)
        n = len(self.osa())
        r = self.send_o('ok', t=NOW)
        self.assertTrue(r.get('needsTarget'), r)
        self.assertEqual(r.get('reason'), 'post-digest')
        self.assertEqual(len(self.osa()), n, 'nothing is sent')
        self.assertEqual(self.posts[-1]['type'], 'needs_target')
        self.assertNotIn('ok', json.dumps(self.posts[-1]))     # body never leaves the host

    def test_second_bare_reply_after_digest_sends(self):
        self.m.rc.record_inbound(S, NOW - 30, label='Sam')
        self.m.rc.write_digest_marker(NOW - 5)
        self.send_o('first', t=NOW)                            # consumes the gate
        r = self.send_o('ok', t=NOW + 1)
        self.assertTrue(r.get('sent'), r)

    def test_named_reply_after_digest_sends_and_clears_gate(self):
        self.m.rc.record_inbound(S, NOW - 30, label='Sam')
        self.m.rc.write_digest_marker(NOW - 5)
        r = self.send_o('Sam: hi there', t=NOW)               # names a person -> no needs_target
        self.assertTrue(r.get('sent'), r)
        r2 = self.send_o('and another', t=NOW + 1)            # gate already cleared
        self.assertTrue(r2.get('sent'), r2)


# ---------- v1.3.5 W1: directive routes get their own type + /yes confirmation ----------
class DirectiveConfirmW1(unittest.TestCase):
    """Reviewer's table: a directive aimed at someone other than the within-window bare-reply target
    holds for /yes; a verb-less dict-word form (Name, / Name -) holds too; explicit Name: does not."""
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc
        C = [{'name': 'Robin Lee', 'handle': '+15555550101'}, {'name': 'Will Smith', 'handle': '+15555550102'},
             {'name': 'May Park', 'handle': '+15555550103'}, {'name': 'Sam Alpha', 'handle': '+15555550107'}]
        self.c = self.rc.sanitize_config({'relayConversationId': CONV, 'contacts': C})
        self.rc.record_inbound('+15555550101', NOW - 60, label='Robin')   # Robin is the bare-reply target

    def route(self, t):
        return self.rc.route_reply(t, '', self.c, self.rc.load_roster(), NOW)

    def test_by_directive_type(self):
        r = self.route('tell Will to call me')
        self.assertEqual(r['by'], 'directive')
        self.assertEqual(r['contact']['label'], 'Will')
        self.assertEqual(r['body'], 'to call me')

    def test_directive_to_other_holds_with_alt(self):
        for t in ['tell Will to call me', 'Will, be there at 5', 'Will - be there', 'May, I think so', 'Sam, grab milk']:
            r = self.route(t)
            self.assertTrue(r.get('confirm'), (t, r))
            self.assertEqual(r['confirm']['reason'], 'directive')
            self.assertEqual(r['confirm']['altLabel'], 'Robin', t)

    def test_directive_to_the_recent_person_just_sends(self):
        # 'Robin, yes!' goes to Robin, who IS the within-window target -> no confirmation.
        r = self.route('Robin, yes!')
        self.assertEqual(r['contact']['label'], 'Robin')
        self.assertIsNone(r.get('confirm'))

    def test_dictword_bare_holds_even_without_a_recent_writer(self):
        m2 = fresh(tmpdir()); c2 = m2.rc.sanitize_config({'relayConversationId': CONV, 'contacts': [
            {'name': 'Will Smith', 'handle': '+15555550102'}, {'name': 'Sam Alpha', 'handle': '+15555550107'}]})
        # no recent inbound at all -> no bare-reply target (alt is None)
        for t in ['Will, be there at 5', 'Will - ok']:
            r = m2.rc.route_reply(t, '', c2, m2.rc.load_roster(), NOW)
            self.assertTrue(r.get('confirm'), (t, r))
            self.assertIsNone(r['confirm']['altLabel'])
        for t in ['Sam, hi there', 'Sam - hi']:     # not a dict word -> just sends
            r = m2.rc.route_reply(t, '', c2, m2.rc.load_roster(), NOW)
            self.assertIsNone(r.get('confirm'), t)

    def test_explicit_colon_stays_unconfirmed(self):
        r = self.route('Will: hello there')
        self.assertEqual(r['by'], 'prefix')
        self.assertIsNone(r.get('confirm'))


class DirectiveConfirmHostW1(_HostBase):
    """End to end: a held directive posts a label-only ❓ prompt (no body) and /yes carrying the handle
    sends to that person, never the recent writer."""
    def send_c(self, text, confirmed=False, handle=None, t=None):
        if t is not None:
            self.t = t
        cfg = self.m.rc.load_config()
        msg = {'type': 'send', 'text': text, 'conversationId': CONV, 'ts': ''}
        if confirmed:
            msg['confirmed'] = True
        if handle is not None:
            msg['handle'] = handle
        return self.m.host.handle_request(
            msg, cfg=cfg, runner=self.runner, sleeper=lambda x: None, now=lambda: self.t,
            poster=lambda u, p, token='', timeout=4: self.posts.append(p) or True)

    def test_directive_confirm_then_yes_sends_to_target(self):
        self.m.rc.record_inbound(A, NOW - 60, label='Robin')         # Robin is the recent writer
        r = self.send_c('Sam, grab milk', t=NOW)                     # directive to Sam
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(r.get('handle'), S)                         # carries Sam's handle
        self.assertEqual(r.get('reason'), 'directive')
        self.assertEqual(len(self.osa()), 0, 'nothing sent before /yes')
        p = [p for p in self.posts if p.get('type') == 'needs_confirm'][-1]
        self.assertNotIn('grab milk', json.dumps(p), 'the stripped body is never shown')
        r2 = self.send_c('grab milk', confirmed=True, handle=S, t=NOW + 5)
        self.assertTrue(r2.get('sent'), r2)
        self.assertEqual(self.osa()[-1][3], S, 'sent to the confirmed directive target')


# ---------- v1.3.5 W4: verb + phone/email never falls back to the last sender ----------
class DirectiveRefuseW4(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc
        C = [{'name': 'Robin Lee', 'handle': '+15555550101'}, {'name': 'Will Smith', 'handle': '+15555550102'}]
        self.c = self.rc.sanitize_config({'relayConversationId': CONV, 'contacts': C})
        self.rc.record_inbound('+15555550101', NOW - 30, label='Robin')   # a last sender exists

    def route(self, t):
        return self.rc.route_reply(t, '', self.c, self.rc.load_roster(), NOW)

    def test_unknown_phone_refuses_never_last_sender(self):
        r = self.route('text +1 555 999 8888 hi')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertIn('never texted', r['reason'])

    def test_unknown_email_refuses_never_last_sender(self):
        r = self.route('text robin@example.com hi')
        self.assertTrue(r.get('needsTarget'), r)
        self.assertIn('never texted', r['reason'])

    def test_verb_plus_known_handle_routes_to_that_handle(self):
        # the digits match a known contact (Will) -> route to Will, not the last sender (Robin)
        r = self.route('text 555 555 0102 hi there')
        self.assertFalse(r.get('needsTarget'), r)
        self.assertEqual(r['contact']['handle'], '+15555550102')
        self.assertNotEqual(r['contact']['handle'], '+15555550101')


# ---------- v1.3.5 nits: /add dry-run + name mismatch ----------
class AddCommandNits(_HostBase):
    def test_dry_run_add_yes_does_not_mutate_config(self):
        self.cfgd['dryRun'] = True
        self._write()
        self.m.rc.record_inbound('+15555559999', NOW - 30, label='+1 (555) 555-9999')
        self.send('/add Kent', t=NOW)
        r = self.send('/add yes', t=NOW + 5)
        self.assertTrue(r.get('dryRun'))
        self.assertFalse(any(c['name'] == 'Kent' for c in self.m.rc.load_config()['contacts']),
                         'dry run must not write the contact')

    def test_add_name_mismatch_refuses_and_keeps_pending(self):
        self.m.rc.record_inbound('+15555559999', NOW - 30, label='+1 (555) 555-9999')
        self.send('/add Kent', t=NOW)
        r = self.send('/add Wrong yes', t=NOW + 5)
        self.assertFalse(r.get('added'))
        self.assertIn('doesn', self.posts[-1]['detail'])         # "doesn't match"
        self.assertFalse(any(c['name'] == 'Wrong' for c in self.m.rc.load_config()['contacts']))
        # the original offer survives: "/add yes" still adds Kent
        r2 = self.send('/add yes', t=NOW + 10)
        self.assertEqual(r2.get('added'), 'Kent')


# ---------- v1.3.5 nit: incoming tapbacks do not update last-sender ----------
class TapbackLastSenderW(_WatchBase):
    def test_tapback_does_not_update_last_sender(self):
        self.w.tick()
        before = self.m.rc.load_roster()
        before_at = {r['handle']: r.get('lastInboundAt') for r in before}
        self.db.add(self.ALLOWED, text='x', assoc=2000, assoc_guid='p:0/none')   # a lone tapback
        self.w.tick()
        after = {r['handle']: r.get('lastInboundAt') for r in self.m.rc.load_roster()}
        self.assertEqual(after.get(self.ALLOWED), before_at.get(self.ALLOWED),
                         'a tapback must not advance the last-inbound time')
