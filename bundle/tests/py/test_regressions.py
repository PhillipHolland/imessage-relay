"""Regression tests for the v1.3.2 audit fixes. Each test pins behavior that the adversarial probes
showed was wrong in v1.2.1 (echo-back, unresolvable-quote fallthrough, lost inbound on scan errors,
silent queue drops, group/ordered delivery matching, timeout double-send) and the new block/mute flow."""
import os
import subprocess
import time
import unittest

from helpers import fresh, tmpdir, FakeClock, FakeRunner, ChatDb

A = '+15555550001'
B = '+15555550002'
OWNER = '+15555550199'
CONV = '1-2'


# ---------- item 1: echo guard ----------
class EchoGuard(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc

    def test_bot_output_formats_are_refused(self):
        for t in ('\u2192 Alex \u2713', '\u26a0 could not send', "Who's this for? Start with a name, like Sam: \u2026"):
            self.assertTrue(self.rc.looks_like_bot_output(t), t)
        self.assertFalse(self.rc.looks_like_bot_output('running late, see you at 7'))

    def test_relayed_inbound_bounced_back_is_detected_via_hmac(self):
        now = 1_800_000_000.0
        self.rc.record_relayed_text('are we still on for dinner?', now)
        self.assertTrue(self.rc.is_echoed_inbound('are we still on for dinner?', 'are we still on for dinner?', now + 20))
        self.assertFalse(self.rc.is_echoed_inbound('a brand new reply', 'a brand new reply', now + 20))
        self.assertFalse(self.rc.is_echoed_inbound('are we still on for dinner?', 'are we still on for dinner?',
                                                   now + self.rc.ECHO_TTL_S + 10), 'expires after the TTL')

    def test_echo_store_and_secret_hold_no_body(self):
        body = 'secret dinner plans for this weekend at the lake'   # >= ECHO_MIN_LEN so it is recorded
        self.rc.record_relayed_text(body, 1_800_000_000.0, handle='+15555550001')
        for p in (self.rc.ECHO_STORE_PATH, self.rc.ECHO_SECRET_PATH):
            with open(p, 'rb') as f:
                self.assertNotIn(body.encode(), f.read())


# ---------- item 2: routing ----------
class Routing(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc
        self.cfg = self.rc.sanitize_config({'contacts': [{'name': 'Alex A', 'handle': A}, {'name': 'Bob B', 'handle': B}],
                                            'relayConversationId': CONV})

    def test_date_like_prefix_is_not_a_handle(self):
        for t in ('2026-10-18: works for me', '10.18.2026: ok', '555-0123: x'):
            self.assertEqual(self.rc.parse_prefix(t, self.cfg)[2], 'none', t)

    def test_unresolvable_quote_asks_instead_of_recent(self):
        now = 1_800_000_000.0
        roster = [{'handle': A, 'label': '', 'lastInboundAt': now - 600},
                  {'handle': B, 'label': '', 'lastInboundAt': now - 60}]
        for q in ('Al\u2026 running late', 'someone sent: running late', 'no name here'):
            r = self.rc.route_reply('ok see you then', q, self.cfg, roster, now)
            # v1.4.2: never silent: either it asks, or it falls back to the last texter flagged for a confirm
            self.assertTrue(r.get('needsTarget') or r.get('quoteUnresolved'), q)

    def test_typo_timezone_disables_quiet_hours(self):
        c = self.rc.sanitize_config({'quietHours': {'start': '22:00', 'end': '07:00', 'tz': 'America/Chcago'}})
        self.assertIsNone(c['quietHours'])


# ---------- item 4 / S1: send path ----------
class SendPath(unittest.TestCase):
    def setUp(self):
        self.rc = fresh(tmpdir()).rc

    def test_local_timeout_is_not_retried(self):
        n = {'c': 0}

        def runner(cmd, **kw):
            if cmd[0].endswith('osascript'):
                n['c'] += 1
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0, '', '')
        ok, err = self.rc.send_imessage('hi', A, runner=runner, sleeper=lambda s: None)
        self.assertEqual((ok, err, n['c']), (False, 'timeout', 1), 'a local timeout must not re-send')

    def test_messages_busy_retries_once(self):
        results = [(1, '(-1712)'), (0, '')]
        r = FakeRunner(results)
        ok, err = self.rc.send_imessage('hi', A, runner=r, sleeper=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(r.count('/usr/bin/osascript'), 2)


# ---------- item 5: delivery proof ----------
class Delivery(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.now = time.time()

    def test_earliest_matching_row_is_claimed_first(self):
        self.db.add(A, text='first', from_me=1, delivered=0, error=22, when=self.now - 5)
        self.db.add(A, text='second', from_me=1, delivered=1, when=self.now - 1)
        status, err, rowid = self.rc.check_delivery_once(A, self.now - 40)
        self.assertEqual(status, 'failed', 'the first (failed) send is matched, not the later delivered one')

    def test_group_send_never_counts_as_one_to_one_delivery(self):
        self.db.add(A, text='1:1 failed', from_me=1, delivered=0, error=22, when=self.now - 5)
        self.db.add(A, text='in group', from_me=1, delivered=1, when=self.now - 1, group='fam', members=(A, B))
        self.assertEqual(self.rc.check_delivery_once(A, self.now - 40)[0], 'failed')

    def test_pending_is_per_file_so_a_concurrent_add_is_not_lost(self):
        id_a = self.rc.add_pending_send(A, self.now, 3, label='Alex')
        snapshot = self.rc.load_pending()                     # watcher loaded
        self.rc.add_pending_send(B, self.now, 3, label='Bob')  # host appends concurrently
        self.rc.remove_pending({'id': id_a})                  # watcher resolves only A
        handles = {e['handle'] for e in self.rc.load_pending()}
        self.assertEqual(handles, {B}, "Bob's concurrently-added record survives")

    def test_pending_record_has_no_body_or_hash(self):
        self.rc.add_pending_send(A, self.now, 11, label='Alex')
        rec = self.rc.load_pending()[0]
        self.assertNotIn('hash', rec)
        self.assertEqual(set(rec) & {'text', 'body'}, set())


# ---------- item 3: inbound durability (watcher) ----------
class _WatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.m = fresh(self.tmp)
        self.db = ChatDb(os.environ['IMESSAGE_RELAY_CHAT_DB'])
        self.db.add(A, text='history before install')
        self.calls = []
        self.ok = {'v': True}
        self.now = time.time()
        self.cfg = {'contacts': [{'name': 'Alex A', 'handle': A}, {'name': 'Bob B', 'handle': B}],
                    'relayConversationId': CONV, 'webhookUrl': 'https://example.invalid/hook',
                    'relayAll': True, 'quietHours': None, 'dryRun': False, 'ownerHandle': OWNER}
        self.w = self.m.watch.Watcher(poster=self.poster, runner=FakeRunner(), now=lambda: self.now,
                                      config_loader=lambda: self.m.rc.sanitize_config(self.cfg), sleeper=lambda s: None)

    def poster(self, url, payload, token='', timeout=15):
        import json
        if self.ok['v']:                       # only a delivered (2xx) POST is recorded, like a real server
            self.calls.append(json.loads(json.dumps(payload)))
        return self.ok['v']

    def texts(self):
        return [c.get('text') for c in self.calls if c.get('type') == 'message']


class Durability(_WatchBase):
    def test_every_payload_carries_a_dedupe_id(self):
        self.w.tick()
        self.db.add(A, text='hi', when=self.now)
        self.w.tick()
        self.assertIn('id', self.calls[-1], 'routine-side dedupe needs a stable id on every payload')

    def test_exception_mid_scan_does_not_lose_the_rest_of_the_batch(self):
        self.w.tick()
        orig = self.m.watch.message_text

        def boom(t, ab, att):
            if t == 'poison':
                raise RuntimeError('decoder blew up')
            return orig(t, ab, att)
        self.m.watch.message_text = boom
        try:
            self.db.add(A, text='before-poison', when=self.now)
            self.db.add(B, text='poison', when=self.now)
            self.db.add(A, text='after-poison', when=self.now)
            c = self.w.tick()
        finally:
            self.m.watch.message_text = orig
        self.assertIn('before-poison', self.texts())
        self.assertIn('after-poison', self.texts(), 'a later row in the batch is still relayed')
        self.assertEqual(c['rowErrors'], 1)

    def test_backpressure_never_drops_and_delivers_all_after_recovery(self):
        self.w.tick()
        self.ok['v'] = False
        for i in range(250):
            self.db.add(A, text='m%d' % i, when=self.now)
        c = self.w.tick()
        self.assertNotIn('droppedOverflow', c, 'the drop-oldest cap was replaced by backpressure')
        self.assertLessEqual(len(self.w.retry), self.m.watch.MAX_QUEUE)
        self.ok['v'] = True
        for _ in range(60):                       # drain, rate-limited per tick
            self.now += 1
            self.w.tick()
            if not self.w.retry and not self.w.held:
                break
        got = self.texts()
        self.assertEqual(len(got), 250, 'every queued message is eventually delivered')
        self.assertEqual(len(set(got)), 250, 'and none is duplicated')

    def test_messages_older_than_a_day_are_skipped(self):
        self.w.tick()
        for i in range(5):
            self.db.add(A, text='stale%d' % i, when=self.now - 86400 * 2)
        c = self.w.tick()
        self.assertEqual(self.texts(), [])
        self.assertEqual(c['tooOld'], 5)

    def test_posts_are_rate_limited_per_tick(self):
        self.w.tick()
        for i in range(60):
            self.db.add(A, text='r%d' % i, when=self.now)
        c = self.w.tick()
        posted = len([x for x in self.calls if x.get('type') == 'message'])
        self.assertLessEqual(posted, self.m.watch.MAX_POSTS_PER_TICK)
        self.assertEqual(c['rateLimited'], 1)


# ---------- item 17: conversational block / mute ----------
class BlockMute(unittest.TestCase):
    """Item 5: muting happens ONLY on an explicit '/'-prefixed (or 'relay:'-prefixed) command with a
    confirmation step. Ordinary sentences — even ones containing block/mute words — are relayed."""
    def setUp(self):
        self.m = fresh(tmpdir())
        self.rc = self.m.rc
        self.host = self.m.host
        self.clock = FakeClock()
        self.posts = []
        self.raw = {'dryRun': False, 'relayConversationId': CONV, 'ownerHandle': OWNER, 'confirmSends': False,
                    'relayAll': True, 'webhookUrl': 'https://example.invalid/hook',
                    'contacts': [{'name': 'Alex A', 'handle': A}, {'name': 'Bob B', 'handle': B}]}
        self.rc.write_json_atomic(self.rc.CONFIG_PATH, self.raw)   # single source of truth on disk
        self.rc.record_inbound(A, self.clock.t - 30)      # Alex was the most recent sender

    def poster(self, url, payload, token='', timeout=15):
        self.posts.append(payload)
        return True

    def send(self, text, quoted=None, **over):
        if over:
            self.rc.write_json_atomic(self.rc.CONFIG_PATH, dict(self.raw, **over))
        msg = {'type': 'send', 'text': text, 'conversationId': CONV, 'ts': '2026-10-18T21:32:00Z'}
        if quoted is not None:
            msg['quotedText'] = quoted
        r = FakeRunner([(0, '')] * 4)
        self.runner = r
        return self.host.handle_request(msg, cfg=self.rc.load_config(), runner=r, sleeper=self.clock.sleep,
                                        now=self.clock.now, poster=self.poster)

    def test_parse_command_only_matches_explicit_commands(self):
        p = self.host.parse_command
        self.assertEqual(p('/block'), {'action': 'block', 'target': '', 'confirm': False})
        self.assertEqual(p('/block yes'), {'action': 'block', 'target': '', 'confirm': True})
        self.assertEqual(p('/block Bob'), {'action': 'block', 'target': 'Bob', 'confirm': False})
        self.assertEqual(p('/unblock Bob'), {'action': 'unblock', 'target': 'Bob', 'confirm': False})
        self.assertEqual(p('relay: block'), {'action': 'block', 'target': '', 'confirm': False})
        self.assertEqual(p('/mute Dad yes'), {'action': 'block', 'target': 'Dad', 'confirm': True})
        for ordinary in ('block', 'Stop relaying.', 'mute dad', 'Mute the TV when you get home',
                         'Block party tonight?', 'Taylor: block', 'see you at 7'):
            self.assertIsNone(p(ordinary), ordinary)

    def test_ordinary_sentences_with_block_words_are_relayed_not_muted(self):
        # the exact reviewer-B probe set: none may mute anyone, all must send normally. Bare "block"
        # is covered separately (item N1): it no longer texts, it nudges toward /block.
        for t in ('Mute Dad lol', 'mute dad', 'Block 5 on the calendar',
                  'Mute the TV when you get home'):
            r = self.send(t)
            self.assertTrue(r['ok'], (t, r))
            self.assertNotIn('action', r if r.get('action') in ('blocked', 'unblocked') else {})
            self.assertEqual(self.rc.read_raw_config().get('mute') or [], [], t)
            self.assertGreaterEqual(len(self.runner.calls), 1, '%s must be texted to someone' % t)

    def test_block_needs_confirmation_then_mutes(self):
        r = self.send('/block Bob')
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(self.runner.calls, [], 'a command is never texted to anyone')
        self.assertEqual(self.rc.load_config()['mute'], [], 'not muted until confirmed')
        self.assertEqual(self.posts[-1]['reason'], 'confirm-block')
        r2 = self.send('/block yes')
        self.assertEqual(r2['action'], 'blocked')
        self.assertIn(B, self.rc.load_config()['mute'])
        self.assertEqual(self.posts[-1]['type'], 'blocked')
        self.assertEqual(self.posts[-1]['label'], 'Bob')

    def test_confirm_without_pending_does_nothing(self):
        r = self.send('/block yes')          # nothing staged
        self.assertFalse(r['ok'])
        self.assertEqual(self.rc.load_config()['mute'], [])

    def test_unblock_is_immediate_no_confirmation(self):
        self.send('/block Bob'); self.send('/block yes')
        self.assertIn(B, self.rc.load_config()['mute'])
        r = self.send('/unblock Bob')
        self.assertEqual(r['action'], 'unblocked')
        self.assertNotIn(B, self.rc.load_config()['mute'])
        self.assertEqual(self.posts[-1]['type'], 'unblocked')

    def test_dry_run_never_mutates_config(self):
        self.send('/block Bob', dryRun=True)
        r = self.send('/block yes', dryRun=True)
        self.assertTrue(r.get('dryRun'))
        self.assertEqual(self.rc.read_raw_config().get('mute') or [], [], 'dry run must not write config.mute')



if __name__ == '__main__':
    unittest.main()
