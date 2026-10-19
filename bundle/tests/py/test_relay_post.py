"""Tests for the routine helper relay_post.py (bot-side): formatting per event type, id dedupe
(ids only, no bodies on disk), and that message text is passed to xchat_lite on STDIN, never argv."""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'routine'))
import relay_post as rp  # noqa: E402


class FakeProc:
    def __init__(self, stdout, returncode=0, stderr=b''):
        self.stdout = stdout if isinstance(stdout, bytes) else stdout.encode()
        self.stderr = stderr if isinstance(stderr, bytes) else stderr.encode()
        self.returncode = returncode


class Formatting(unittest.TestCase):
    def one(self, payload):
        ev = rp.format_event(payload)
        self.assertEqual(len(ev['lines']), 1)
        return ev['kind'], ev['lines'][0]

    def test_message_and_group(self):
        k, ln = self.one({'type': 'message', 'id': 'g1', 'label': 'Alex', 'text': 'dinner at 7?'})
        self.assertEqual((k, ln['text'], ln['body'], ln['id']), ('message', 'Alex: dinner at 7?', True, 'g1'))
        _, ln = self.one({'type': 'message', 'id': 'g2', 'label': 'Sam', 'text': 'yo', 'group': {'label': 'Family'}})
        self.assertEqual(ln['text'], 'Family \u00b7 Sam: yo')

    def test_digest_header_and_per_person_grouping(self):
        # item 7: a header line (no body/notify), then ONE line per person with their texts grouped.
        ev = rp.format_event({'type': 'digest', 'id': 'd', 'messages': [
            {'label': 'Alex', 'text': 'one'},
            {'id': 'x9', 'label': 'Sam', 'text': 'two', 'group': {'name': 'Crew'}},
            {'label': 'Alex', 'text': 'three'}]})
        self.assertEqual(ev['kind'], 'digest')
        self.assertEqual([l['text'] for l in ev['lines']],
                         ['\U0001f4ec 3 messages while you were away from 2 people.',
                          'Alex: one\n\u2002three', 'Crew \u00b7 Sam: two'])
        self.assertEqual([l['id'] for l in ev['lines']], ['d:header', 'digest:d#0+d#2', 'digest:x9'])
        self.assertFalse(ev['lines'][0]['body'])          # header is a status line: should_notify false
        self.assertTrue(ev['lines'][1]['body'] and ev['lines'][2]['body'])

    def test_digest_single_message_header(self):
        ev = rp.format_event({'type': 'digest', 'id': 'd', 'messages': [{'label': 'Alex', 'text': 'hi'}]})
        self.assertEqual(ev['lines'][0]['text'], '\U0001f4ec 1 message while you were away.')

    def test_sent_and_dry_run(self):
        # item 6: delivered -> double check, otherwise the ~20s "sent" fallback.
        self.assertEqual(self.one({'type': 'sent', 'label': 'Alex', 'delivered': True})[1]['text'], '\u2192 Alex \u2713\u2713 delivered')
        self.assertEqual(self.one({'type': 'sent', 'label': 'Alex'})[1]['text'], '\u2192 Alex \u2713 sent')
        self.assertEqual(self.one({'type': 'sent', 'label': 'Alex', 'dryRun': True})[1]['text'], '\u2192 Alex (dry run, not sent)')
        self.assertEqual(self.one({'type': 'dry_run', 'label': 'Alex'})[1]['text'], '\u2192 Alex (dry run, not sent)')
        self.assertFalse(self.one({'type': 'sent', 'label': 'Alex', 'delivered': True})[1]['body'], 'confirmation is silent')

    def test_send_failed_needs_target(self):
        _, ln = self.one({'type': 'send_failed', 'label': 'Alex', 'error': '-1712'})
        self.assertIn('Couldn', ln['text'])
        self.assertIn('-1712', ln['text'])
        self.assertFalse(ln['body'])
        _, ln = self.one({'type': 'needs_target', 'detail': 'Who\u2019s this for? Start with a name, like Alex: \u2026'})
        self.assertTrue(ln['text'].startswith('Who'))

    def test_send_failed_unconfirmed_and_superseded(self):
        # W3: no row ever -> "couldn't confirm"; a late failure after a soft check -> a correction line.
        _, ln = self.one({'type': 'send_failed', 'label': 'Alex', 'reason': 'unconfirmed'})
        self.assertIn('confirm', ln['text'])
        self.assertFalse(ln['body'])
        _, ln = self.one({'type': 'send_failed', 'label': 'Alex', 'error': '-1712', 'superseded': True})
        self.assertIn('Correction', ln['text'])
        self.assertIn('\u2713 sent', ln['text'])     # names the earlier soft confirmation it supersedes
        self.assertIn('-1712', ln['text'])
        self.assertFalse(ln['body'])

    def test_block_unblock(self):
        self.assertEqual(self.one({'type': 'blocked', 'label': 'Spam'})[1]['text'], '\U0001f6ab Muted Spam. Say "/unblock Spam" to undo.')
        self.assertEqual(self.one({'type': 'blocked', 'label': 'Spam', 'dryRun': True})[1]['text'], '\U0001f6ab (dry run) would mute Spam. Say "/unblock Spam" to undo once live.')
        self.assertEqual(self.one({'type': 'unblocked', 'label': 'Spam'})[1]['text'], '\u2705 Unmuted Spam.')

    def test_status_variants(self):
        self.assertIn('Catching up', self.one({'type': 'status', 'status': 'catching_up'})[1]['text'])
        self.assertIn('Resynced', self.one({'type': 'resynced'})[1]['text'])
        self.assertIn('Catching up', self.one({'type': 'catching_up'})[1]['text'])
        self.assertIn('Full Disk Access', self.one({'type': 'status', 'status': 'fda_missing'})[1]['text'])

    def test_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            rp.format_event({'type': 'nope'})


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, 'sub', 'seen.json')

    def test_remember_prune_and_persist(self):
        t = [1000.0]
        dd = rp.Dedupe(self.p, ttl_seconds=100, now=lambda: t[0])
        self.assertFalse(dd.seen('a'))
        dd.commit(['a'])                      # commit = remember ids + persist atomically under a lock
        self.assertTrue(dd.seen('a'))
        self.assertEqual(oct(os.stat(self.p).st_mode & 0o777), '0o600')
        # a later load within TTL keeps it; past TTL prunes it
        dd2 = rp.Dedupe(self.p, ttl_seconds=100, now=lambda: 1050.0)
        self.assertTrue(dd2.seen('a'))
        dd3 = rp.Dedupe(self.p, ttl_seconds=100, now=lambda: 2000.0)
        self.assertFalse(dd3.seen('a'))

    def test_only_ids_on_disk_no_bodies(self):
        dd = rp.Dedupe(self.p, ttl_seconds=100)
        dd.commit(['guid-123'])
        disk = open(self.p).read()
        self.assertIn('guid-123', disk)
        self.assertNotIn('dinner', disk)


class Encrypt(unittest.TestCase):
    def test_session_encrypt_stdin_carries_text_and_keys(self):
        body = rp.session_encrypt_stdin('AAA-BBB', 'secret body', {
            'events': ['e'], 'conversation_key_events': ['k'], 'signing_keys': [{'id': '1'}], 'prepare': {'conversation_id': 'AAA-BBB'}})
        self.assertEqual(body['text'], 'secret body')
        self.assertEqual(body['conversation_key_events'], ['k'])
        self.assertEqual(body['prepare']['conversation_id'], 'AAA-BBB')

    def test_encrypt_line_passes_text_on_stdin_never_argv(self):
        seen = {}

        def runner(argv, input=b''):
            seen['argv'] = argv
            seen['stdin'] = json.loads(input.decode())
            return FakeProc(json.dumps({'message_id': 'm1', 'encoded_message_create_event': 'EV', 'encoded_message_event_signature': 'SIG',
                                        'conversation_key_b64': 'KEY', 'conversation_key_version': '3'}))
        helper = {'python': '/x/py', 'script': '/x/xchat_lite.py', 'user_id': 'U', 'key_version': '3', 'juicebox': '/x/jb.json'}
        send = rp.encrypt_line('AAA-BBB', 'top secret body', {'events': [], 'conversation_key_events': []}, helper, runner)
        self.assertNotIn('top secret body', ' '.join(seen['argv']), 'the body must never appear on the command line')
        self.assertEqual(seen['stdin']['text'], 'top secret body')
        self.assertNotIn('conversation_key_b64', seen['stdin'], 'no dead conversation-key cache goes to the helper')
        self.assertEqual(seen['argv'][:2], ['/x/py', '/x/xchat_lite.py'])
        self.assertIn('session-encrypt', seen['argv'])
        self.assertEqual((send['message_id'], send['encoded_message_create_event'], send['encoded_message_event_signature']), ('m1', 'EV', 'SIG'))

    def test_encrypt_line_surfaces_add_conversation_keys_for_empty_thread(self):
        def runner(argv, input=b''):
            return FakeProc(json.dumps({'message_id': 'm', 'encoded_message_create_event': 'E', 'encoded_message_event_signature': 'S',
                                        'needs_add_conversation_keys_before_send': True, 'add_conversation_keys': {'body': 1}}))
        send = rp.encrypt_line('AAA-BBB', 'hi', {'prepare': {'conversation_id': 'AAA-BBB'}}, {'python': '/x/py', 'script': '/x/s.py'}, runner)
        self.assertEqual(send['add_conversation_keys'], {'body': 1})

    def test_encrypt_line_uses_server_conversation_id(self):
        def runner(argv, input=b''):
            return FakeProc(json.dumps({'id': 'SERVER-CONV', 'message_id': 'm', 'encoded_message_create_event': 'E',
                                        'encoded_message_event_signature': 'S'}))
        send = rp.encrypt_line('AAA-BBB', 'hi', {'events': []}, {'python': '/x/py', 'script': '/x/s.py'}, runner)
        self.assertEqual(send['conversation_id'], 'SERVER-CONV')


class Process(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.dedupe = os.path.join(self.d, 'seen.json')

    def ok_runner(self, calls):
        def runner(argv, input=b''):
            calls.append(json.loads(input.decode()))
            return FakeProc(json.dumps({'message_id': 'm', 'encoded_message_create_event': 'E', 'encoded_message_event_signature': 'S'}))
        return runner

    def test_send_then_duplicate_is_skipped_and_no_body_on_disk(self):
        calls = []
        req = {'payload': {'type': 'message', 'id': 'g1', 'label': 'Alex', 'text': 'dinner at 7?'},
               'conversation_id': 'AAA-BBB', 'xchat': {'events': [], 'conversation_key_events': []}, 'dedupe_path': self.dedupe}
        r = rp.process(req, runner=self.ok_runner(calls))
        self.assertEqual(r['action'], 'send')
        self.assertEqual(len(r['sends']), 1)
        self.assertTrue(r['encrypted'])
        self.assertEqual(calls[0]['text'], 'Alex: dinner at 7?')           # body only ever on stdin
        self.assertNotIn('dinner', open(self.dedupe).read())               # dedupe file holds ids only
        r2 = rp.process(req, runner=self.ok_runner([]))
        self.assertEqual(r2['action'], 'skip')

    def test_error_when_no_xchat_context(self):
        r = rp.process({'payload': {'type': 'message', 'id': 'g', 'label': 'A', 'text': 'x'}, 'conversation_id': 'C', 'dedupe_path': self.dedupe})
        self.assertEqual(r['action'], 'error')
        self.assertFalse(r.get('encrypted', False))

    def test_unknown_type_is_a_clean_error(self):
        with self.assertRaises(ValueError):
            rp.process({'payload': {'type': 'bogus'}, 'conversation_id': 'C', 'dedupe_path': self.dedupe}, runner=self.ok_runner([]))

    def test_encrypt_failure_is_body_free_error(self):
        def boom(argv, input=b''):
            return FakeProc('', returncode=1, stderr=b'kaboom')
        r = rp.process({'payload': {'type': 'message', 'id': 'g', 'label': 'A', 'text': 'secret'},
                        'conversation_id': 'C', 'xchat': {'events': []}, 'dedupe_path': self.dedupe}, runner=boom)
        self.assertEqual(r['action'], 'error')
        self.assertNotIn('secret', json.dumps(r))


if __name__ == '__main__':
    unittest.main()
