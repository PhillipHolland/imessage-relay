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
        # v1.4.6 (item 7): the overnight recap is ONE tidy post: a header, then each person once with their
        # texts under them. It carries bodies, so it is a body line.
        ev = rp.format_event({'type': 'digest', 'id': 'd', 'messages': [
            {'label': 'Alex', 'text': 'one'},
            {'id': 'x9', 'label': 'Sam', 'text': 'two', 'group': {'name': 'Crew'}},
            {'label': 'Alex', 'text': 'three'}]})
        self.assertEqual(ev['kind'], 'digest')
        self.assertEqual(len(ev['lines']), 1)
        self.assertEqual(ev['lines'][0]['text'],
                         'While you were away \u00b7 3 texts\n\n'
                         'Alex:\n\u2002\u2022 one\n\u2002\u2022 three\n\n'
                         'Crew \u00b7 Sam:\n\u2002\u2022 two')
        self.assertEqual(ev['lines'][0]['id'], 'digest:d')
        self.assertTrue(ev['lines'][0]['body'])

    def test_digest_single_message_header(self):
        ev = rp.format_event({'type': 'digest', 'id': 'd', 'messages': [{'label': 'Alex', 'text': 'hi'}]})
        self.assertTrue(ev['lines'][0]['text'].startswith('While you were away \u00b7 1 text\n\n'))

    def test_digest_is_capped(self):
        ev = rp.format_event({'type': 'digest', 'id': 'd', 'messages': [{'label': 'Alex', 'text': 'x' * 3000}] * 5})
        self.assertLessEqual(len(ev['lines'][0]['text']), rp.DIGEST_MAX_CHARS)
        self.assertTrue(ev['lines'][0]['text'].endswith('More is in Messages.'))

    def test_sent_and_dry_run(self):
        # item 6: delivered -> double check, otherwise one check. The words "sent" and "delivered" stay off the line.
        self.assertEqual(self.one({'type': 'sent', 'label': 'Alex', 'delivered': True})[1]['text'], '\u2192 Alex \u2713\u2713')
        self.assertEqual(self.one({'type': 'sent', 'label': 'Alex'})[1]['text'], '\u2192 Alex \u2713')
        self.assertEqual(self.one({'type': 'sent', 'label': 'Alex', 'dryRun': True})[1]['text'], '\u2192 Alex (dry run, not sent)')
        self.assertEqual(self.one({'type': 'dry_run', 'label': 'Alex'})[1]['text'], '\u2192 Alex (dry run, not sent)')
        self.assertFalse(self.one({'type': 'sent', 'label': 'Alex', 'delivered': True})[1]['body'], 'confirmation is silent')

    def test_send_failed_needs_target(self):
        _, ln = self.one({'type': 'send_failed', 'label': 'Alex', 'error': '-1712'})
        self.assertEqual(ln['text'], '\u26a0\ufe0f Didn\u2019t reach Alex.')
        self.assertNotIn('-1712', ln['text'], 'the raw Messages code stays out of the chat line')
        self.assertFalse(ln['body'])
        _, ln = self.one({'type': 'needs_target', 'detail': 'Who\u2019s this for? Start with a name, like Alex: \u2026'})
        self.assertTrue(ln['text'].startswith('Who'))

    def test_send_failed_unconfirmed_and_superseded(self):
        # W3: no row ever -> "couldn't confirm"; a late failure after a soft check -> a correction line.
        _, ln = self.one({'type': 'send_failed', 'label': 'Alex', 'reason': 'unconfirmed'})
        self.assertEqual(ln['text'], '\u26a0\ufe0f Couldn\u2019t confirm it reached Alex.')
        self.assertFalse(ln['body'])
        _, ln = self.one({'type': 'send_failed', 'label': 'Alex', 'error': '-1712', 'superseded': True})
        self.assertEqual(ln['text'], '\u26a0\ufe0f The earlier \u2713 to Alex was wrong. It did not deliver.')
        self.assertNotIn('-1712', ln['text'])
        self.assertFalse(ln['body'])

    def test_block_unblock(self):
        self.assertEqual(self.one({'type': 'blocked', 'label': 'Spam'})[1]['text'], 'Muted Spam. /unblock Spam')
        self.assertEqual(self.one({'type': 'blocked', 'label': 'Spam', 'dryRun': True})[1]['text'], 'Dry run. Would mute Spam.')
        self.assertEqual(self.one({'type': 'unblocked', 'label': 'Spam'})[1]['text'], 'Unmuted Spam.')

    def test_status_variants(self):
        self.assertIn('Catching up', self.one({'type': 'status', 'status': 'catching_up'})[1]['text'])
        self.assertIn('skipped after a gap', self.one({'type': 'resynced'})[1]['text'])
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
               'conversation_id': 'AAA-BBB', 'xchat': {'events': [], 'conversation_key_events': ['K'], 'signing_keys': [{'user_id': 'U'}]}, 'dedupe_path': self.dedupe}
        r = rp.process(req, runner=self.ok_runner(calls))
        self.assertEqual(r['action'], 'send')
        self.assertEqual(len(r['sends']), 1)
        self.assertTrue(r['encrypted'])
        self.assertEqual(calls[0]['text'], 'Alex: dinner at 7?')           # body only ever on stdin
        self.assertNotIn('dinner', open(self.dedupe).read())               # dedupe file holds ids only
        r2 = rp.process(req, runner=self.ok_runner([]))
        self.assertEqual(r2['action'], 'skip')

    def test_no_keys_and_no_cache_asks_to_fetch(self):
        # v1.4.5: was an error; now the routine fetches the keys and runs again (nothing committed)
        r = rp.process({'payload': {'type': 'message', 'id': 'g', 'label': 'A', 'text': 'x'}, 'conversation_id': 'C', 'dedupe_path': self.dedupe})
        self.assertEqual((r['action'], r['need']), ('fetch', ['conversation_key_events', 'signing_keys']))
        self.assertFalse(os.path.exists(self.dedupe))

    def test_unknown_type_is_a_clean_error(self):
        with self.assertRaises(ValueError):
            rp.process({'payload': {'type': 'bogus'}, 'conversation_id': 'C', 'dedupe_path': self.dedupe}, runner=self.ok_runner([]))

    def test_encrypt_failure_is_body_free_error(self):
        def boom(argv, input=b''):
            return FakeProc('', returncode=1, stderr=b'kaboom')
        r = rp.process({'payload': {'type': 'message', 'id': 'g', 'label': 'A', 'text': 'secret'},
                        'conversation_id': 'C', 'xchat': {'conversation_key_events': ['K'], 'signing_keys': ['S']}, 'dedupe_path': self.dedupe}, runner=boom)
        self.assertEqual(r['action'], 'error')
        self.assertNotIn('secret', json.dumps(r))


class SpeedV145(unittest.TestCase):
    """v1.4.5: key cache (key material only), fetch fallback, batch events."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.dedupe = os.path.join(self.d, 'seen.json')
        self.keys = os.path.join(self.d, 'xchat_keys.json')
        self.calls = []

    def runner(self, ok=True):
        def run(argv, input=b''):
            self.calls.append(json.loads(input.decode()))
            if not ok:
                return FakeProc('', returncode=1)
            return FakeProc(json.dumps({'message_id': 'm%d' % len(self.calls), 'encoded_message_create_event': 'E',
                                        'encoded_message_event_signature': 'S'}))
        return run

    def req(self, pid, xchat=None, **pay):
        p = dict({'type': 'message', 'id': pid, 'label': 'Alex', 'text': 'body-' + pid}, **pay)
        r = {'payload': p, 'conversation_id': 'AAA-BBB', 'dedupe_path': self.dedupe}
        if xchat is not None:
            r['xchat'] = xchat
        return r

    FULL = {'events': ['EVENT-WITH-BODY'], 'conversation_key_events': ['KEYEV'], 'signing_keys': [{'user_id': 'U'}]}

    def test_fetched_keys_are_cached_without_events_then_reused(self):
        r = rp.process(self.req('a', dict(self.FULL)), runner=self.runner())
        self.assertEqual(r['action'], 'send')
        with open(self.keys) as fh:
            raw = fh.read()
        self.assertNotIn('EVENT-WITH-BODY', raw)
        self.assertNotIn('body-a', raw)
        self.assertEqual(oct(os.stat(self.keys).st_mode & 0o777), '0o600')
        r2 = rp.process(self.req('b'), runner=self.runner())          # no fetch at all
        self.assertEqual(r2['action'], 'send')
        self.assertEqual((self.calls[-1]['conversation_key_events'], self.calls[-1]['signing_keys']), (['KEYEV'], [{'user_id': 'U'}]))
        self.assertEqual(self.calls[-1]['events'], [])

    def test_cache_is_per_conversation_and_expires(self):
        rp.process(self.req('a', dict(self.FULL)), runner=self.runner())
        other = self.req('b'); other['conversation_id'] = 'CCC-DDD'
        self.assertEqual(rp.process(other, runner=self.runner())['action'], 'fetch')
        with open(self.keys) as fh:
            d = json.load(fh)
        d['savedAt'] -= rp.KEY_CACHE_TTL_S + 5
        with open(self.keys, 'w') as fh:
            json.dump(d, fh)
        self.assertEqual(rp.process(self.req('c'), runner=self.runner())['action'], 'fetch')

    def test_cached_keys_that_fail_are_dropped_and_fetch_is_asked(self):
        rp.process(self.req('a', dict(self.FULL)), runner=self.runner())
        r = rp.process(self.req('b'), runner=self.runner(ok=False))
        self.assertEqual(r['action'], 'fetch')
        self.assertFalse(os.path.exists(self.keys))
        with open(self.dedupe) as fh:
            self.assertNotIn('b', json.load(fh))                       # not remembered: the rerun posts it

    def test_supplied_keys_always_win_and_refresh_the_cache(self):
        rp.process(self.req('a', dict(self.FULL)), runner=self.runner())
        new = dict(self.FULL, conversation_key_events=['KEYEV2'])
        rp.process(self.req('b', new), runner=self.runner())
        self.assertEqual(self.calls[-1]['conversation_key_events'], ['KEYEV2'])
        with open(self.keys) as fh:
            self.assertEqual(json.load(fh)['conversation_key_events'], ['KEYEV2'])

    def test_key_cache_can_be_disabled_and_forgotten(self):
        r = self.req('a', dict(self.FULL)); r['key_cache'] = False
        rp.process(r, runner=self.runner())
        self.assertFalse(os.path.exists(self.keys))
        cache = rp.KeyCache(self.keys)
        cache.save('AAA-BBB', ['K'], ['S'])
        cache.clear()
        self.assertIsNone(cache.load('AAA-BBB'))

    def test_batch_is_one_run_with_one_line_per_text_each_deduped(self):
        msgs = [{'id': 'm1', 'label': 'Alex', 'text': 'one'},
                {'id': 'm2', 'label': 'Sam', 'text': 'two', 'group': {'label': 'Family'}},
                {'id': 'm3', 'label': '(555) 555-0166', 'text': 'three', 'unknown': True}]
        r = rp.process({'payload': {'type': 'batch', 'id': 'b1', 'count': 3, 'messages': msgs}, 'conversation_id': 'AAA-BBB',
                        'xchat': dict(self.FULL), 'dedupe_path': self.dedupe, 'defer_commit': True}, runner=self.runner())
        self.assertEqual(r['action'], 'send')
        self.assertEqual([c['text'] for c in self.calls],
                         ['Alex: one', 'Family \u00b7 Sam: two', '\u26a0\ufe0f (555) 555-0166 \u00b7 not a contact: three'])
        self.assertTrue(all(s['should_notify'] for s in r['sends']))
        self.assertEqual(r['ack_ids'], ['m1', 'm2', 'm3'])
        rp.ack({'dedupe_path': self.dedupe, 'ids': ['m1']})
        r2 = rp.process({'payload': {'type': 'batch', 'id': 'b2', 'messages': msgs}, 'conversation_id': 'AAA-BBB',
                         'dedupe_path': self.dedupe}, runner=self.runner())
        self.assertEqual(r2['ack_ids'], ['m2', 'm3'])

    def test_reply_to_threads_only_when_events_were_fetched(self):
        rp.process(self.req('a', dict(self.FULL), type='sent', reply_to='EV1'), runner=self.runner())
        self.assertEqual(self.calls[-1].get('reply_to'), 'EV1')
        rp.process(self.req('b', type='sent', reply_to='EV2'), runner=self.runner())      # cached keys, no events
        self.assertNotIn('reply_to', self.calls[-1])

    def test_empty_batch_is_an_error(self):
        with self.assertRaises(ValueError):
            rp.format_event({'type': 'batch', 'messages': []})

    def test_forget_keys_cli(self):
        import io, contextlib
        saved = rp.KEY_CACHE_PATH
        rp.KEY_CACHE_PATH = self.keys                # never the real cache in $HOME
        try:
            rp.KeyCache().save('AAA-BBB', ['K'], ['S'])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(rp.main(['--forget-keys']), 0)
        finally:
            rp.KEY_CACHE_PATH = saved
        self.assertEqual(json.loads(buf.getvalue())['action'], 'forgot_keys')
        self.assertFalse(os.path.exists(self.keys))


if __name__ == '__main__':
    unittest.main()
