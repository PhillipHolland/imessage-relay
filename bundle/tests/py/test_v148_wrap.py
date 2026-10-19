"""v1.4.8: the per-photo key travels sealed inside the drop blob (wrap key), so the webhook item carries no
key and no url; relay_post derives the url from drop.json; body-free media_status codes + counts-only
media.log. Regression: an item whose key did not survive the routine's hands is diagnosed ('no_key'),
never fetched. No network; fake drop. Fictional names only."""
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest

from helpers import fresh, tmpdir
import test_v146_media as v146

PNG = v146.PNG

import relay_drop  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

DROP = 'https://drop.example.invalid'
WK = relay_drop.b64u(bytes(range(32)))
TOK = 'e' * 32


class Wrap(unittest.TestCase):
    def test_round_trip_fresh_and_bound(self):
        wk = bytes(range(32))
        w1, w2 = relay_drop.seal_v2(TOK, PNG, wk), relay_drop.seal_v2(TOK, PNG, wk)
        self.assertEqual(w1[:1], b'\x02')
        self.assertEqual(len(w1), len(PNG) + 28 + relay_drop.V2_HEADER)
        self.assertNotEqual(w1[1:13], w2[1:13], 'fresh wrap nonce')
        self.assertNotEqual(w1[relay_drop.V2_HEADER:][:12], w2[relay_drop.V2_HEADER:][:12], 'fresh data nonce')
        self.assertEqual(relay_drop.open_v2(TOK, w1, wk), PNG)
        for tok, key in (('f' * 32, wk), (TOK, bytes(32))):
            with self.assertRaises(ValueError):
                relay_drop.open_v2(tok, w1, key)           # another token, or the wrong wrap key
        bad = bytearray(w1)
        bad[5] ^= 1
        with self.assertRaises(ValueError):
            relay_drop.open_v2(TOK, bytes(bad), wk)          # a tampered wrapped key
        self.assertNotIn(PNG[:16], w1)

    def test_wrap_key_parsing(self):
        self.assertEqual(relay_drop.wrap_key({'dropWrapKey': WK}), bytes(range(32)))
        for bad in ('', 'short', WK[:-1], '!' * 43):
            self.assertIsNone(relay_drop.wrap_key({'dropWrapKey': bad}))


class UploadV2(v146.unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.cfg = {'dropUrl': DROP, 'dropKey': 'k' * 40, 'dropWrapKey': WK}
        self.reqs = []

    def staged(self):
        p = os.path.join(self.d, TOK + '.png')
        with open(p, 'wb') as fh:
            fh.write(PNG)
        return {'token': TOK, 'kind': 'image', 'mime': 'image/png', 'ext': 'png', 'size': len(PNG),
                'sha256': hashlib.sha256(PNG).hexdigest(), 'macPath': p}

    def requester(self, method, url, key, body=None, timeout=None):
        self.reqs.append((method, url, body, os.listdir(self.d)))
        return 201

    def test_webhook_item_has_no_key_and_no_url(self):
        out, failed = relay_drop.upload_staged([self.staged()], self.cfg, self.requester)
        self.assertEqual(failed, 0)
        self.assertEqual(out[0], {'token': TOK, 'kind': 'image', 'mime': 'image/png', 'ext': 'png', 'size': len(PNG),
                                  'sha256': hashlib.sha256(PNG).hexdigest(), 'v': 2})
        method, url, body, files = self.reqs[0]
        self.assertEqual((method, url, files), ('PUT', DROP + '/d/' + TOK, []))
        self.assertEqual(relay_drop.open_v2(TOK, body, bytes(range(32))), PNG)
        relay_drop.discard(out, self.cfg, self.requester)
        self.assertEqual(self.reqs[-1][:2], ('DELETE', DROP + '/d/' + TOK), 'held spam is still discarded')

    def test_without_wrap_key_stays_v1(self):
        cfg = dict(self.cfg, dropWrapKey='')
        out, _ = relay_drop.upload_staged([self.staged()], cfg, self.requester)
        self.assertEqual(set(out[0]) - {'kind', 'mime', 'ext', 'size', 'sha256', 'token'}, {'url', 'key'})

    def test_largest_photo_fits_the_drop(self):
        self.assertLessEqual(relay_drop.MAX_PLAIN + relay_drop.V2_HEADER + 28, 4 * 1024 * 1024 + 28)


class RoutineV2(unittest.TestCase):
    runner = v146.RoutineMedia.runner
    req = v146.RoutineMedia.req
    files = v146.RoutineMedia.files

    def setUp(self):
        v146.RoutineMedia.setUp(self)
        os.remove(os.path.join(self.media_dir, TOK + '.png'))
        self.wire = relay_drop.seal_v2(TOK, PNG, bytes(range(32)))
        self.item = {k: v for k, v in self.item.items() if k != 'macPath'}
        self.item['v'] = 2
        self.gets = []

    def getter(self, url, key, max_bytes):
        self.gets.append((url, key, max_bytes))
        w, self.wire = self.wire, None
        return w

    def dreq(self, drop=None, **over):
        r = self.req(drop=drop or {'url': DROP, 'get_key': 'g' * 40, 'wrap_key': WK}, _drop_getter=self.getter)
        r.update(over)
        return r

    def log(self):
        p = os.path.join(self.d, 'media.log')
        with open(p) as fh:
            lines = [json.loads(x) for x in fh]
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        return lines

    def test_v2_fetch_decrypt_upload_send(self):
        r1 = rp.process(self.dreq(), runner=self.runner)
        self.assertEqual(r1['action'], 'upload', r1)
        self.assertEqual(self.gets, [(DROP + '/d/' + TOK, 'g' * 40, len(PNG) + 28 + rp.DROP_V2_HEADER)],
                         'url derived from drop.json, not the payload')
        self.assertEqual(r1['media_status'], {'upload_ready': 1})
        r2 = rp.process(self.dreq(media_keys={TOK: 'HASHKEY'}), runner=self.runner)
        self.assertEqual(r2['action'], 'send')
        self.assertEqual(r2['media_status'], {'attached': 1})
        self.assertEqual(self.files(), [])
        log = self.log()
        self.assertEqual([x['status'] for x in log], [{'upload_ready': 1}, {'attached': 1}])
        raw = json.dumps(log)
        for secret in (TOK, WK, 'g' * 40, DROP, self.item['sha256'], 'Jordan'):
            self.assertNotIn(secret, raw, 'media.log is counts only')

    def test_v2_payload_url_is_ignored(self):
        self.item['url'] = 'https://elsewhere.example.invalid/d/' + TOK
        r = rp.process(self.dreq(), runner=self.runner)
        self.assertEqual(r['action'], 'upload')
        self.assertEqual(self.gets[0][0], DROP + '/d/' + TOK)

    def test_v2_without_wrap_key_on_the_box(self):
        r = rp.process(self.dreq(drop={'url': DROP, 'get_key': 'g' * 40}), runner=self.runner)
        self.assertEqual((r['action'], r['media_status']), ('send', {'no_key': 1}))
        self.assertEqual(self.gets, [])
        self.assertIn('couldn\u2019t be relayed', self.calls[-1][1]['text'])

    def test_v2_wrong_wrap_key(self):
        r = rp.process(self.dreq(drop={'url': DROP, 'get_key': 'g' * 40, 'wrap_key': relay_drop.b64u(bytes(32))}),
                       runner=self.runner)
        self.assertEqual(r['media_status'], {'decrypt_failed': 1})
        self.assertEqual(self.files(), [])

    def test_regression_v1_key_lost_in_the_routine(self):
        # 2026-10-19: a v1 item reached relay_post without a usable key -> no GET, a notice; now diagnosable
        self.item.pop('v')
        self.item.update(url=DROP + '/d/' + TOK, key='[redacted]')
        r = rp.process(self.dreq(), runner=self.runner)
        self.assertEqual((r['action'], r['media_status']), ('send', {'no_key': 1}))
        self.assertEqual(self.gets, [])

    def test_not_requested_and_drop_json_gate(self):
        r = rp.process(self.dreq(media_upload=False), runner=self.runner)
        self.assertEqual(r['media_status'], {'not_requested': 1})
        self.assertEqual(self.gets, [])
        self.calls = []
        self.wire = relay_drop.seal_v2(TOK, PNG, bytes(range(32)))
        r = rp.process(self.dreq(drop={'url': DROP, 'get_key': 'g' * 40, 'wrap_key': WK, 'media_upload': True},
                                 media_upload=False, payload=dict(self.req()['payload'], id='g-2')), runner=self.runner)
        self.assertEqual(r['action'], 'upload', 'drop.json media_upload works without the request flag')

    def test_routine_media_off(self):
        r = rp.process(self.dreq(media_off=True), runner=self.runner)
        self.assertEqual(r['media_status'], {'routine_media_off': 1})

    def test_drop_json_fields(self):
        p = os.path.join(self.d, 'drop.json')
        with open(p, 'w') as fh:
            json.dump({'url': DROP, 'get_key': 'g' * 40, 'wrap_key': WK, 'media_upload': True}, fh)
        d = rp.load_drop({}, os.path.join(self.d, 'seen.json'))
        self.assertEqual((d['wrap_key'], d['media_upload']), (WK, True))
        with open(p, 'w') as fh:
            json.dump({'url': DROP, 'get_key': 'g' * 40, 'wrap_key': 'bad', 'media_upload': 'yes'}, fh)
        d = rp.load_drop({}, os.path.join(self.d, 'seen.json'))
        self.assertEqual((d['wrap_key'], d['media_upload']), ('', False))

    def test_v2_decrypt_via_another_python(self):
        out = os.path.join(self.media_dir, 'f' * 32 + '.png')
        wire = relay_drop.seal_v2('f' * 32, PNG, bytes(range(32)))
        seen = []

        def runner(argv, input=None, capture_output=None, timeout=None):
            seen.append((argv, input))
            return subprocess.run(argv, input=input, capture_output=True, timeout=60)
        mod = 'cryptography.hazmat.primitives.ciphers.aead'
        old = sys.modules.get(mod, 'absent')
        sys.modules[mod] = None
        real = rp._crypto_pythons
        rp._crypto_pythons = lambda drop: iter([sys.executable])
        try:
            ok = rp.drop_decrypt(wire, None, 'f' * 32, out, {'wrap_key': WK}, runner, v=2)
        finally:
            rp._crypto_pythons = real
            if old == 'absent':
                del sys.modules[mod]
            else:
                sys.modules[mod] = old
        self.assertTrue(seen)
        self.assertTrue(ok)
        with open(out, 'rb') as fh:
            self.assertEqual(fh.read(), PNG)
        self.assertNotIn(WK, ' '.join(seen[0][0]), 'wrap key not in argv')


class CtlWrap(unittest.TestCase):
    def test_set_wrap_key_on_stdin_and_redacted(self):
        m = fresh(tmpdir())
        out = m.ctl.cmd_set_drop(wrap_key_stdin=True, stdin=io.StringIO(WK + '\n'))
        self.assertEqual(out['set'], ['dropWrapKey'])
        self.assertNotIn(WK, json.dumps(out))
        cfg = m.rc.load_config()
        self.assertEqual(cfg['dropWrapKey'], WK)
        self.assertEqual(m.ctl.public_config(cfg)['dropWrapKey'], 'set')
        with self.assertRaises(m.ctl.CtlError):
            m.ctl.cmd_set_drop(wrap_key_stdin=True, stdin=io.StringIO('nope'))
        with self.assertRaises(m.ctl.CtlError):
            m.ctl.cmd_set_drop(url=DROP, key_stdin=True, wrap_key_stdin=True, stdin=io.StringIO(WK))
        m.ctl.cmd_set_drop(off=True)
        self.assertEqual(m.rc.load_config()['dropWrapKey'], '')

    def test_not_settable_from_argv(self):
        m = fresh(tmpdir())
        with self.assertRaises(m.ctl.CtlError):
            m.ctl.cmd_set('dropWrapKey', WK)


if __name__ == '__main__':
    unittest.main()
