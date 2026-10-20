"""v1.4.6: attachments both ways (1:1 photos in, his photos/files out). In-flight temp files only (mode
600), deleted right after use or on error; logs carry counts, sizes and types only. Fictional names only."""
import base64
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
import unittest

from helpers import FakeRunner, read_log
import test_watch
from test_v146 import HostBase, CONV, XU, CHECK

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

ALLOWED = test_watch.ALLOWED
# a tiny valid PNG (1x1)
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
OK = (0, '')
PHOTO = '\u26a0\ufe0f photo didn\u2019t send to %s, send it from Messages'
PHOTO_BARE = '\u26a0\ufe0f photo didn\u2019t send'


def upload(m, data, uid='a' * 32):
    for seq, i in enumerate(range(0, len(data), 7)):
        r = m.media.stage_chunk(uid, seq, base64.b64encode(data[i:i + 7]).decode())
        assert r['ok'], r
    return {'uploadId': uid, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'mime': 'image/png',
            'name': '../../etc/Jordan beach.png'}


class OsaScript(unittest.TestCase):
    def setUp(self):
        from helpers import fresh, tmpdir
        self.m = fresh(tmpdir())

    def test_file_mode_argv(self):
        rc = self.m.rc
        c = rc.build_osascript_cmd('/x/photo.png', '+15555550123', 'auto', target='participant', mode='file')
        self.assertEqual(c[-5:], ['/x/photo.png', '+15555550123', 'auto', 'participant', 'file'])
        c = rc.build_osascript_cmd('/x/photo.png', 'iMessage;+;chat1', 'auto', target='chat', mode='file')
        self.assertEqual(c[-2:], ['chat', 'file'])
        self.assertEqual(rc.build_osascript_cmd('hi', '+1', 'auto', target='chat')[-1], 'chat', 'v1.4.5 unchanged')
        self.assertEqual(rc.build_osascript_cmd('hi', '+1')[-1], 'auto')

    def test_applescript_resolves_the_file_before_messages(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'mac-helper',
                               'send_imessage.applescript')) as fh:
            src = fh.read()
        self.assertIn('set theItem to (POSIX file thePayload) as alias', src)
        self.assertLess(src.index('as alias'), src.index('tell application "Messages"'))
        self.assertNotIn('run script', src)


class Outbound(HostBase):
    def test_photo_and_text_go_out_as_two_messages_with_one_check(self):
        att = upload(self.m, PNG)
        runner = FakeRunner([OK, OK])
        r = self.req({'type': 'send', 'text': 'Jordan: look', 'attachments': [att], 'ui': True, 'xUuid': XU}, runner=runner)
        self.assertTrue(r['ok'] and r['sent'])
        osa = [c for c in runner.calls if c[0] == '/usr/bin/osascript']
        self.assertEqual(osa[0][2:], ['look', '+15555550123', 'auto'])
        path = osa[1][2]
        self.assertEqual(osa[1][-2:], ['participant', 'file'])
        self.assertTrue(path.startswith(self.m.media.SEND_STAGE_DIR))
        self.assertEqual(os.path.basename(path), 'Jordan beach.png', 'path parts stripped')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        pend = self.m.rc.load_pending()
        self.assertEqual(sorted(p.get('part', 'text') for p in pend), ['file', 'text'])
        react = r['ui']['react'][0]
        self.assertEqual(react['emoji'], CHECK)
        self.assertEqual(set(react['pendingIds']), {p['id'] for p in pend})
        self.assertFalse(os.path.exists(os.path.join(self.m.media.INFLIGHT_DIR, att['uploadId'])), 'upload consumed')
        self.req({'type': 'ui_ack', 'pendingId': react['pendingId'], 'pendingIds': react['pendingIds'], 'ok': True})
        self.assertTrue(all(p.get('uiConfirmed') for p in self.m.rc.load_pending()))

    def test_same_photo_twice_within_a_minute_is_one_send(self):
        self.req({'type': 'send', 'text': '', 'attachments': [upload(self.m, PNG)]}, runner=FakeRunner([OK]))
        r = self.req({'type': 'send', 'text': '', 'attachments': [upload(self.m, PNG, 'b' * 32)]}, runner=FakeRunner([]))
        self.assertEqual(r.get('skipped'), 'duplicate within 60s')
        self.assertFalse(os.path.exists(os.path.join(self.m.media.INFLIGHT_DIR, 'b' * 32)), 'its upload is dropped')
        self.assertFalse(any('photo didn' in (p.get('detail') or '') for p in self.posts), 'a duplicate is not a failed photo')

    def test_photo_only(self):
        att = upload(self.m, PNG)
        runner = FakeRunner([OK])
        r = self.req({'type': 'send', 'text': 'Jordan:', 'attachments': [att]}, runner=runner)
        self.assertTrue(r.get('sent'), r)
        self.assertEqual(r.get('routedBy'), 'prefix')
        self.assertEqual(runner.count('/usr/bin/osascript'), 1)
        osa = [c for c in runner.calls if c[0] == '/usr/bin/osascript']
        self.assertEqual(osa[0][-2:], ['participant', 'file'])
        self.assertNotIn('Jordan:', ' '.join(str(x) for x in osa[0]))
        log = read_log()
        self.assertNotIn('Jordan:', log)
        self.assertIn('"kinds": ["png"]', log)
        r = self.req({'type': 'send', 'text': '', 'attachments': [upload(self.m, PNG + b'2', 'b' * 32)]}, runner=FakeRunner([OK]))
        self.assertTrue(r.get('sent'), 'a bare photo goes to the recent person like a bare text')

    def test_bad_checksum_sends_nothing_at_all(self):
        att = upload(self.m, PNG)
        att['sha256'] = '0' * 64
        runner = FakeRunner([])
        r = self.req({'type': 'send', 'text': 'Jordan: look', 'attachments': [att]}, runner=runner)
        self.assertFalse(r['ok'])
        self.assertEqual(r['errorCode'], 'attachment_unavailable')
        self.assertEqual(runner.count('/usr/bin/osascript'), 0)
        self.assertEqual(os.listdir(self.m.media.SEND_STAGE_DIR) if os.path.isdir(self.m.media.SEND_STAGE_DIR) else [], [])
        self.assertEqual(self.posts[-1]['detail'], PHOTO % 'Jordan')

    def test_failed_file_send_deletes_the_staged_copy(self):
        att = upload(self.m, PNG)
        r = self.req({'type': 'send', 'text': 'Jordan: look', 'attachments': [att]}, runner=FakeRunner([OK, (1, 'execution error (-1700)')]))
        self.assertEqual(r['errorCode'], 'attachment_failed')
        stage = self.m.media.SEND_STAGE_DIR
        self.assertEqual([f for _, _, fs in os.walk(stage) for f in fs], [])
        self.assertEqual(self.posts[-1]['type'], 'status')
        self.assertEqual(self.posts[-1]['detail'], PHOTO % 'Jordan')

    def test_group_photo_needs_the_confirm_and_keeps_the_upload_until_then(self):
        att = upload(self.m, PNG)
        runner = FakeRunner([])
        self.m.rc.record_inbound('group:chat900', self.clock.t - 30, label='Family')
        h = self.h
        h.group_guid_lookup = lambda key, sleeper, now: 'iMessage;+;chat900'
        r = self.req({'type': 'send', 'text': 'Family: look', 'attachments': [att]}, runner=runner, relayAll=True)
        self.assertTrue(r.get('needsConfirm'), r)
        self.assertEqual(runner.count('/usr/bin/osascript'), 0)
        self.assertTrue(os.path.exists(os.path.join(self.m.media.INFLIGHT_DIR, att['uploadId'])), 'kept for the yes')
        runner = FakeRunner([OK, OK])
        r = self.req({'type': 'send', 'text': 'Family: look', 'attachments': [att], 'confirmed': True,
                      'handle': r['handle']}, runner=runner, relayAll=True)
        self.assertTrue(r.get('sent'), r)
        osa = [c for c in runner.calls if c[0] == '/usr/bin/osascript']
        self.assertEqual(osa[1][3:], ['iMessage;+;chat900', 'auto', 'chat', 'file'])

    def test_dry_run_discards_the_upload(self):
        att = upload(self.m, PNG)
        runner = FakeRunner([])
        r = self.req({'type': 'send', 'text': 'Jordan: look', 'attachments': [att]}, runner=runner, dryRun=True)
        self.assertTrue(r['dryRun'])
        self.assertEqual(runner.count('/usr/bin/osascript'), 0)
        self.assertFalse(os.path.exists(os.path.join(self.m.media.INFLIGHT_DIR, att['uploadId'])))
        self.assertFalse(any('photo didn' in (p.get('detail') or '') for p in self.posts))

    def test_limits(self):
        r = self.req({'type': 'send', 'text': 'Jordan: x', 'attachments': [{'uploadId': 'c' * 32, 'size': 26 * 1024 * 1024,
                                                                            'sha256': 'f' * 64, 'mime': 'video/mp4'}]})
        self.assertIn('too large', r['error'])
        self.assertIn('The attachment is over 25 MB.', self.posts[-1]['detail'])
        r = self.req({'type': 'send', 'text': 'Jordan: x', 'attachments': [{'uploadId': '../x', 'size': 5, 'sha256': 'f' * 64}]})
        self.assertEqual(r['error'], 'bad attachment')
        r = self.req({'type': 'attach_chunk', 'uploadId': 'd' * 32, 'seq': 1, 'data': 'aGk='})
        self.assertEqual(r['error'], 'chunk out of order')
        r = self.req({'type': 'attach_chunk', 'uploadId': 'd' * 32, 'seq': 0, 'data': 'aGk='}, relayAttachments=False)
        self.assertFalse(r['ok'])

    def test_unreadable_photos_get_a_clear_notice(self):
        runner = FakeRunner([])
        r = self.req({'type': 'send', 'text': '', 'attachmentsSkipped': 2}, runner=runner)
        self.assertFalse(r['ok'])
        self.assertEqual(runner.count('/usr/bin/osascript'), 0)
        self.assertEqual(self.posts[-1]['type'], 'status')
        self.assertEqual(self.posts[-1]['detail'], PHOTO_BARE)
        self.assertEqual(rp.format_event(self.posts[-1])['lines'][0]['text'], PHOTO_BARE)
        self.posts.clear()
        r = self.req({'type': 'send', 'text': 'Jordan: hi', 'attachmentsSkipped': 1}, runner=FakeRunner([OK]))
        self.assertTrue(r['sent'])
        self.assertEqual(self.posts[-1]['detail'], PHOTO % 'Jordan')
        self.assertEqual(rp.format_event(self.posts[-1])['lines'][0]['text'], PHOTO % 'Jordan')

    def test_photo_only_quote_sends_like_an_emoji_reply(self):
        self.h._note_prompt(self.clock.now())
        att = upload(self.m, PNG, 'e' * 32)
        r = self.req({'type': 'send', 'text': '', 'quotedText': 'Jordan: on my way', 'attachments': [att]},
                     runner=FakeRunner([OK]))
        self.assertTrue(r.get('sent'), r)
        self.assertEqual(r.get('routedBy'), 'quote')
        log = read_log()
        self.assertNotIn('on my way', log)
        self.assertIn('"kinds": ["png"]', log)

    def test_type_cap_refuses_and_heic_is_allowed(self):
        att = upload(self.m, PNG, 'f' * 32)
        att['mime'] = 'application/pdf'
        r = self.req({'type': 'send', 'text': 'Jordan: look', 'attachments': [att]}, runner=FakeRunner([]))
        self.assertFalse(r['ok'])
        self.assertEqual(r['error'], 'attachment type not allowed')
        self.assertEqual(self.posts[-1]['detail'], PHOTO_BARE)
        stage = self.m.media.SEND_STAGE_DIR
        self.assertEqual([f for _, _, fs in os.walk(stage) for f in fs] if os.path.isdir(stage) else [], [])
        self.assertFalse(os.path.exists(os.path.join(self.m.media.INFLIGHT_DIR, att['uploadId'])))
        heic = upload(self.m, PNG + b'h', '9' * 32)
        heic['mime'] = 'image/heic'
        r = self.req({'type': 'send', 'text': 'Jordan: look', 'attachments': [heic]}, runner=FakeRunner([OK, OK]))
        self.assertTrue(r.get('sent'), r)
        self.assertIn('"kinds": ["heic"]', read_log())

    def test_prefix_only_unreadable_photo_names_the_person(self):
        r = self.req({'type': 'send', 'text': 'Jordan:', 'attachmentsSkipped': 1}, runner=FakeRunner([]))
        self.assertFalse(r['ok'])
        self.assertEqual(r['error'], 'attachments could not be read')
        self.assertEqual(self.posts[-1]['type'], 'status')
        self.assertEqual(self.posts[-1]['detail'], PHOTO % 'Jordan')
        self.assertNotIn('Jordan:', read_log())

    def test_skip_mail_card_logs_fingerprint_only(self):
        secret = 'Note for Jordan about Saturday'
        fp = 'E=0.div@message-<self>{data-dm-message-row,data-send-status}'
        r = self.req({'type': 'signal', 'status': 'skip_mail_card', 'fingerprint': fp,
                      'text': secret, 'quotedText': secret, 'body': secret})
        self.assertTrue(r.get('ok'), r)
        self.assertEqual(self.posts, [])
        log = read_log()
        evs = [json.loads(line) for line in log.splitlines() if '"skip_mail_card"' in line]
        self.assertEqual(len(evs), 1)
        self.assertEqual(set(evs[0]), {'ts', 'event', 'fingerprint'})
        self.assertEqual(evs[0]['event'], 'skip_mail_card')
        self.assertIn('data-dm-message-row', evs[0]['fingerprint'])
        self.assertNotIn('quote_probe', log)
        self.assertNotIn(secret, log)
        self.assertNotIn('"text"', log)

    def test_photo_held_logs_counts_only(self):
        r = self.req({'type': 'signal', 'status': 'photo_held', 'count': 1, 'bytes': 12,
                      'kinds': ['png', 'nope', 'jpeg']})
        self.assertTrue(r.get('ok'), r)
        self.assertEqual(self.posts, [])
        log = read_log()
        self.assertIn('"event": "photo_held"', log)
        self.assertIn('"count": 1', log)
        self.assertIn('"bytes": 12', log)
        self.assertIn('"jpeg"', log)
        self.assertIn('"png"', log)
        self.assertNotIn('nope', log)
        self.assertNotIn('photo didn', log)

    def test_photo_miss_signal_has_no_name(self):
        r = self.req({'type': 'signal', 'status': 'photo_miss'})
        self.assertTrue(r.get('ok'), r)
        self.assertEqual(self.posts[-1]['type'], 'status')
        self.assertEqual(self.posts[-1]['detail'], PHOTO_BARE)
        self.assertEqual(rp.format_event(self.posts[-1])['lines'][0]['text'], PHOTO_BARE)
        log = read_log()
        self.assertNotIn('photo didn', log)
        self.assertIn('"status": "photo_miss"', log)

    def test_logs_counts_sizes_types_only(self):
        att = upload(self.m, PNG)
        self.req({'type': 'send', 'text': 'Jordan: look', 'attachments': [att]}, runner=FakeRunner([OK, OK]))
        log = read_log() if isinstance(read_log(), str) else json.dumps(read_log())
        for secret in ('beach', att['sha256'], att['uploadId'], 'look', 'send-stage'):
            self.assertNotIn(secret, log)
        self.assertIn('"attachments": 1', log)
        self.assertIn('"kinds": ["png"]', log)


class WatchMedia(test_watch.Base):
    def setUp(self):
        super().setUp()
        con = sqlite3.connect(self.db.path)
        con.execute('ALTER TABLE attachment ADD COLUMN filename TEXT')
        con.execute('ALTER TABLE attachment ADD COLUMN total_bytes INTEGER')
        con.commit()
        con.close()
        # v1.4.7: inbound photos go through the encrypted drop (a fake here; no network)
        self.cfg.update(dropUrl='https://drop.example.invalid', dropKey='k' * 40)
        self.puts, self.drop_status = [], 201
        self.w = self.new_watcher()
        self.w.drop_requester = self.fake_drop
        self.w.tick()

    def fake_drop(self, method, url, key, body=None, timeout=None):
        self.puts.append((method, url, key, body))
        return self.drop_status if method == 'PUT' else 200

    def photo(self, data=PNG, name='IMG_0001.png', mime='image/png', write=True, **kw):
        p = os.path.join(self.tmp, 'Attachments', name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if write:
            with open(p, 'wb') as f:
                f.write(data)
        mid = self.db.add(kw.pop('handle', ALLOWED), attach={'transfer_name': name, 'mime': mime}, **kw)
        con = sqlite3.connect(self.db.path)
        con.execute('UPDATE attachment SET filename = ?, total_bytes = ? WHERE ROWID = (SELECT MAX(ROWID) FROM attachment)',
                    (p, len(data)))
        con.commit()
        con.close()
        return mid, p

    def msgs(self):
        return [p for _, p, _ in self.poster.calls if p['type'] == 'message']

    def test_a_photo_goes_to_the_drop_encrypted(self):
        import relay_drop
        self.photo(text='beach day')
        self.w.tick()
        [m] = self.msgs()
        self.assertEqual(m['text'], 'beach day')
        [md] = m['media']
        self.assertEqual(set(md), {'token', 'kind', 'mime', 'ext', 'size', 'sha256', 'url', 'key'}, 'no path, no name')
        self.assertEqual((md['mime'], md['size'], md['sha256']), ('image/png', len(PNG), hashlib.sha256(PNG).hexdigest()))
        self.assertEqual(md['url'], 'https://drop.example.invalid/d/' + md['token'])
        self.assertNotIn('IMG_0001', json.dumps(m))
        [(method, url, key, wire)] = self.puts
        self.assertEqual((method, url, key), ('PUT', md['url'], 'k' * 40))
        self.assertEqual(len(wire), len(PNG) + 28)
        self.assertNotIn(PNG[8:], wire, 'only ciphertext leaves the Mac')
        k = base64.urlsafe_b64decode(md['key'] + '=')
        self.assertEqual(relay_drop.gcm_decrypt(k, wire[:12], wire[12:], b'imr-drop-v1:' + md['token'].encode()), PNG)
        self.assertEqual([f for _, _, fs in os.walk(self.m.media.OUTBOX_DIR) for f in fs], [], 'temp copy deleted at once')
        line = rp.format_event(m)['lines'][0]
        self.assertEqual((line['media'][0]['url'], line['media'][0]['key']), (md['url'], md['key']))

    def test_without_a_drop_photos_just_say_photo(self):
        self.cfg.update(dropUrl='', dropKey='')
        self.photo(text='beach day')
        self.w.tick()
        [m] = self.msgs()
        self.assertNotIn('media', m)
        self.assertIn('\n\u2002Photo', m['text'])
        self.assertNotIn('\U0001f4f7', m['text'])
        self.assertEqual(self.puts, [])

    def test_a_failed_upload_gets_a_notice_and_leaves_nothing(self):
        self.drop_status = 503
        self.photo(text='beach day')
        self.w.tick()
        [m] = self.msgs()
        self.assertNotIn('media', m)
        self.assertIn('Couldn\u2019t relay it', m['text'])
        self.assertEqual([f for _, _, fs in os.walk(self.m.media.OUTBOX_DIR) for f in fs], [])

    def test_two_photos_get_different_keys(self):
        self.photo(text='a')
        self.w.tick()
        self.photo(data=PNG + b'x', name='IMG_0002.png', text='b')
        self.w.tick()
        keys = [m['media'][0]['key'] for m in self.msgs()]
        self.assertEqual(len(set(keys)), 2)
        self.assertNotEqual(self.puts[0][3][:12], self.puts[1][3][:12], 'fresh nonce')

    def test_photo_without_text_says_photo(self):
        self.photo()
        self.w.tick()
        self.assertEqual(self.msgs()[0]['text'], '')
        self.assertIn('media', self.msgs()[0])

    def test_non_image_gets_a_notice_and_no_bytes(self):
        self.photo(data=b'%PDF-1.4 fake', name='Lease.pdf', mime='application/pdf', text='see attached')
        self.w.tick()
        [m] = self.msgs()
        self.assertNotIn('media', m)
        self.assertIn('PDF file', m['text'])
        self.assertIn('Open Messages', m['text'])
        self.assertNotIn('Lease', m['text'])

    def test_still_downloading_is_retried_then_noticed(self):
        self.photo(write=False, when=self.now - 2)
        self.w.tick()
        self.assertEqual(self.msgs(), [], 'deferred while the file lands')
        self.now += 40
        self.w.tick()
        [m] = self.msgs()
        self.assertNotIn('media', m)
        self.assertIn('Couldn\u2019t relay it', m['text'])

    def test_groups_and_quiet_hours_are_not_staged(self):
        self.cfg['relayAll'] = True
        self.photo(handle='+15555550144', group=True, members=('+15555550144', '+15555550145'), display_name='Family')
        self.w.tick()
        self.assertTrue(all('media' not in p for p in self.msgs()))
        self.assertFalse(os.path.isdir(self.m.media.OUTBOX_DIR) and os.listdir(self.m.media.OUTBOX_DIR))

    def test_switch_off(self):
        self.cfg['relayAttachments'] = False
        self.photo()
        self.w.tick()
        self.assertNotIn('media', self.msgs()[0])

    def test_outbox_is_swept(self):
        os.makedirs(self.m.media.OUTBOX_DIR, exist_ok=True)
        p = os.path.join(self.m.media.OUTBOX_DIR, 'f' * 32 + '.png')
        with open(p, 'wb') as f:
            f.write(PNG)                                      # a leftover (e.g. a crash mid-upload)
        old = self.now - self.m.media.OUTBOX_TTL_S - 5     # the watcher sweeps on its own clock
        os.utime(p, (old, old))
        self.w.tick()
        self.assertFalse(os.path.exists(p))

    def test_logs_no_names_or_hashes(self):
        self.photo(text='hello')
        self.w.tick()
        log = json.dumps(read_log())
        self.assertNotIn('IMG_0001', log)
        self.assertNotIn(hashlib.sha256(PNG).hexdigest(), log)
        md = self.msgs()[0]['media'][0]
        self.assertNotIn(md['key'], log)
        self.assertNotIn(md['token'], log)
        self.assertIn('media_staged', log)

    def test_attachment_delivery_needs_an_attachment_row_and_cleans_up(self):
        rc = self.m.rc
        stage = os.path.join(self.m.media.SEND_STAGE_DIR, 'abcd')
        os.makedirs(stage)
        sp = os.path.join(stage, 'photo.png')
        with open(sp, 'wb') as fh:
            fh.write(PNG)
        rc.add_pending_send(ALLOWED, self.now - 30, 0, label='Jordan', part='file', staged_path=sp)
        self.db.add(ALLOWED, text='plain later text', from_me=1, delivered=1)
        self.w.tick()
        self.assertEqual(len(rc.load_pending()), 1, 'a text row never proves an attachment')
        self.db.add(ALLOWED, text='\ufffc', from_me=1, delivered=1, attachments=1)
        self.w.tick()
        self.assertTrue(os.path.exists(sp), 'a flag without an attachment row does not prove delivery')
        self.assertEqual(len(rc.load_pending()), 1)
        self.db.add(ALLOWED, text='\ufffc', from_me=1, delivered=1, attach={'transfer_name': 'photo.png', 'mime': 'image/png'})
        self.w.tick()
        self.assertEqual(rc.load_pending(), [])
        self.assertFalse(os.path.exists(sp))


class RoutineMedia(unittest.TestCase):
    """relay_post: run 1 -> upload parts, run 2 (media_keys) -> sends that carry the photo; always cleaned."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix='imr-rp-')
        self.media_dir = os.path.join(self.d, 'media')
        os.makedirs(self.media_dir, mode=0o700)
        self.calls = []
        self.item = {'token': 'e' * 32, 'kind': 'image', 'mime': 'image/png', 'ext': 'png', 'size': len(PNG),
                     'sha256': hashlib.sha256(PNG).hexdigest(), 'macPath': '/Users/someone/.imessage-relay/outbox/x.png'}
        with open(os.path.join(self.media_dir, 'e' * 32 + '.png'), 'wb') as f:
            f.write(PNG)
        self.helper = {'python': '/bin/py', 'script': '/old/xchat_lite.py', 'media_python': '/bin/py2', 'media_script': '/new/xchat_lite.py'}

    def runner(self, argv, input=b''):
        import subprocess
        stdin = json.loads(input.decode())
        self.calls.append((argv, stdin))
        if 'encrypt-media' in argv:
            out_dir = argv[argv.index('--out-dir') + 1]
            up = os.path.join(out_dir, 'upload-1')
            os.makedirs(up, mode=0o700, exist_ok=True)
            part = os.path.join(up, 'part-0')
            with open(part, 'w') as fh:
                fh.write('QUJD')
            res = {'upload_conversation_id': '1111:2222', 'upload_dir': up,
                   'media': [{'total_bytes': 123, 'num_parts': 1, 'parts': [{'segment_index': 0, 'path': part}],
                              'conversation_key_version': '1001'}], 'errors': {}}
        else:
            res = {'id': '1111-2222', 'message_id': 'mid-1', 'encoded_message_create_event': 'E', 'encoded_message_event_signature': 'S'}
        return subprocess.CompletedProcess(argv, 0, json.dumps(res).encode(), b'')

    def req(self, **over):
        r = {'payload': {'type': 'message', 'id': 'g-1', 'contactName': 'Jordan', 'label': 'Jordan', 'text': '\U0001f4f7 Photo',
                         'media': [self.item]},
             'conversation_id': '1111-2222', 'helper': self.helper, 'dedupe_path': os.path.join(self.d, 'seen.json'),
             'key_cache': False, 'media_dir': self.media_dir, 'media_upload': True,
             'xchat': {'conversation_key_events': ['k'], 'signing_keys': ['s']}}
        r.update(over)
        return r

    def files(self):
        return sorted(f for _, _, fs in os.walk(self.media_dir) for f in fs)

    def test_two_runs(self):
        r1 = rp.process(self.req(), runner=self.runner)
        self.assertEqual(r1['action'], 'upload')
        [u] = r1['uploads']
        self.assertEqual((u['token'], u['upload_conversation_id'], u['num_parts']), ('e' * 32, '1111:2222', '1'))
        self.assertTrue(u['parts'][0]['media'].startswith('$file:'))
        self.assertEqual(self.calls[0][0][:2], ['/bin/py2', '/new/xchat_lite.py'], 'the encrypt-media helper')
        self.assertNotIn('text', self.calls[0][1])
        self.assertFalse(os.path.exists(os.path.join(self.d, 'seen.json')), 'nothing committed yet')
        r2 = rp.process(self.req(media_keys={'e' * 32: 'HASHKEY'}), runner=self.runner)
        self.assertEqual(r2['action'], 'send')
        [s] = r2['sends']
        self.assertEqual(s['media_finalize'], [{'token': 'e' * 32, 'media_hash_key': 'HASHKEY', 'num_parts': '1'}])
        argv, stdin = self.calls[-1]
        self.assertEqual(argv[:2], ['/bin/py2', '/new/xchat_lite.py'])
        self.assertEqual(stdin['media'][0]['media_hash_key'], 'HASHKEY')
        self.assertEqual(stdin['media'][0]['conversation_key_version'], '1001')
        self.assertEqual(self.files(), [], 'photo, parts and state all deleted')

    def test_text_lines_keep_the_old_helper(self):
        req = self.req()
        req['payload'] = {'type': 'message', 'id': 'g-2', 'label': 'Jordan', 'text': 'hi'}
        r = rp.process(req, runner=self.runner)
        self.assertEqual(r['action'], 'send')
        self.assertEqual(self.calls[0][0][:2], ['/bin/py', '/old/xchat_lite.py'])

    def test_a_bad_copy_gets_a_notice_and_is_deleted(self):
        with open(os.path.join(self.media_dir, 'e' * 32 + '.png'), 'wb') as f:
            f.write(PNG[:-1] + b'X')
        r = rp.process(self.req(), runner=self.runner)
        self.assertEqual(r['action'], 'send', 'nothing uploadable: post the text')
        text = self.calls[-1][1]['text']
        self.assertIn('didn\u2019t come through', text)
        self.assertNotIn('media', self.calls[-1][1])
        self.assertEqual(self.files(), [])

    def test_missing_copy(self):
        os.remove(os.path.join(self.media_dir, 'e' * 32 + '.png'))
        r = rp.process(self.req(), runner=self.runner)
        self.assertEqual(r['action'], 'send')
        self.assertIn('Open Messages', self.calls[-1][1]['text'])

    def test_media_off_after_a_failed_upload(self):
        rp.process(self.req(), runner=self.runner)
        r = rp.process(self.req(media_off=True), runner=self.runner)
        self.assertEqual(r['action'], 'send')
        self.assertIn('didn\u2019t come through', self.calls[-1][1]['text'])
        self.assertEqual(self.files(), [])

    def test_text_with_photo_gets_an_appended_notice(self):
        self.assertEqual(rp.media_failed_text('Jordan: beach', 1),
                         'Jordan: beach\n\u2002Photo didn\u2019t come through. Open Messages.')
        self.assertEqual(rp.media_failed_text('Jordan: \U0001f4f7 3 photos', 3),
                         'Jordan: 3 photos didn\u2019t come through. Open Messages.')

    def test_invalid_media_meta_is_ignored(self):
        bad = [{'token': '../x', 'ext': 'png', 'size': 1, 'sha256': 'a' * 64}, {'token': 'f' * 32, 'ext': 'exe', 'size': 1, 'sha256': 'a' * 64},
               {'token': 'f' * 32, 'ext': 'png', 'size': 6 * 1024 * 1024, 'sha256': 'a' * 64}, 'x']
        self.assertEqual(rp.media_meta(bad), [])

    def test_leftovers_are_swept(self):
        p = os.path.join(self.media_dir, 'e' * 32 + '.png')
        old = time.time() - rp.MEDIA_TTL_S - 5
        os.utime(p, (old, old))
        self.assertEqual(rp.media_sweep(self.media_dir), 1)
        self.assertEqual(self.files(), [])


if __name__ == '__main__':
    unittest.main()
