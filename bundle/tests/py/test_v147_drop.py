"""v1.4.7: the encrypted photo drop (Mac encrypt + PUT, relay_post fetch + decrypt). No network; fake drop.
Fictional names only."""
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

import relay_drop  # noqa: E402  (mac-helper is on sys.path via helpers)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402

DROP = 'https://drop.example.invalid'


class Gcm(unittest.TestCase):
    def test_nist_vectors(self):
        # NIST GCM spec test cases 13 and 14 (AES-256, zero key and IV)
        self.assertEqual(relay_drop.gcm_encrypt(bytes(32), bytes(12), b'', b'').hex(), '530f8afbc74536b9a963b4f1c4cb738b')
        self.assertEqual(relay_drop.gcm_encrypt(bytes(32), bytes(12), bytes(16), b'').hex(),
                         'cea7403d4d606b6e074ec5d3baf39d18' + 'd0d1c8a799996bf0265b98b5d48ab919')

    def test_seal_is_fresh_and_bound_to_the_token(self):
        w1, k1 = relay_drop.seal('a' * 32, PNG)
        w2, k2 = relay_drop.seal('a' * 32, PNG)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(w1[:12], w2[:12])
        self.assertEqual(len(w1), len(PNG) + 28)
        key = base64.urlsafe_b64decode(k1 + '=')
        self.assertEqual(relay_drop.gcm_decrypt(key, w1[:12], w1[12:], b'imr-drop-v1:' + b'a' * 32), PNG)
        with self.assertRaises(ValueError):
            relay_drop.gcm_decrypt(key, w1[:12], w1[12:], b'imr-drop-v1:' + b'b' * 32)   # another token
        bad = bytearray(w1)
        bad[20] ^= 1
        with self.assertRaises(ValueError):
            relay_drop.gcm_decrypt(key, bytes(bad[:12]), bytes(bad[12:]), b'imr-drop-v1:' + b'a' * 32)


class Upload(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.cfg = {'dropUrl': DROP + '/', 'dropKey': 'k' * 40}
        self.reqs = []

    def staged(self, data=PNG, tok='c' * 32):
        p = os.path.join(self.d, tok + '.png')
        with open(p, 'wb') as fh:
            fh.write(data)
        return {'token': tok, 'kind': 'image', 'mime': 'image/png', 'ext': 'png', 'size': len(PNG),
                'sha256': hashlib.sha256(PNG).hexdigest(), 'macPath': p}

    def requester(self, status=201):
        def r(method, url, key, body=None, timeout=None):
            self.reqs.append((method, url, key, body, [f for f in os.listdir(self.d)]))
            return status
        return r

    def test_deleted_before_the_upload(self):
        m = self.staged()
        out, failed = relay_drop.upload_staged([m], self.cfg, self.requester())
        self.assertEqual(failed, 0)
        self.assertEqual(self.reqs[0][4], [], 'the plaintext copy was gone before the PUT started')
        self.assertEqual(self.reqs[0][:3], ('PUT', DROP + '/d/' + 'c' * 32, 'k' * 40))
        self.assertEqual(set(out[0]), {'token', 'kind', 'mime', 'ext', 'size', 'sha256', 'url', 'key'})

    def test_failures_still_delete(self):
        for data, status in ((PNG + b'tampered', 201), (PNG, 503), (PNG, 409)):
            self.reqs = []
            out, failed = relay_drop.upload_staged([self.staged(data)], self.cfg, self.requester(status))
            self.assertEqual((out, failed), ([], 1))
            self.assertEqual(os.listdir(self.d), [])

    def test_not_configured_and_discard(self):
        self.assertFalse(relay_drop.configured({'dropUrl': 'http://x', 'dropKey': 'k' * 40}))
        self.assertFalse(relay_drop.configured({'dropUrl': DROP, 'dropKey': ''}))
        relay_drop.discard([{'token': 'c' * 32, 'url': DROP + '/d/' + 'c' * 32}, {'token': 'd' * 32}], self.cfg, self.requester())
        self.assertEqual([(r[0], r[1]) for r in self.reqs], [('DELETE', DROP + '/d/' + 'c' * 32)])


class RoutineDrop(unittest.TestCase):
    """relay_post fetches from OUR drop only, checks size + sha, decrypts, then the v1.4.6 upload flow."""
    runner = v146.RoutineMedia.runner
    req = v146.RoutineMedia.req
    files = v146.RoutineMedia.files

    def setUp(self):
        v146.RoutineMedia.setUp(self)
        os.remove(os.path.join(self.media_dir, 'e' * 32 + '.png'))       # nothing pre-copied: it comes from the drop
        self.wire, key = relay_drop.seal('e' * 32, PNG)
        self.item = {k: v for k, v in self.item.items() if k != 'macPath'}
        self.item.update(url=DROP + '/d/' + 'e' * 32, key=key)
        self.gets = []

    def getter(self, url, key, max_bytes):
        self.gets.append((url, key, max_bytes))
        w, self.wire = self.wire, None                                  # served once
        return w

    def dreq(self, **over):
        r = self.req(drop={'url': DROP, 'get_key': 'g' * 40}, _drop_getter=self.getter)
        r.update(over)
        return r

    def test_fetch_decrypt_upload_send_cleanup(self):
        r1 = rp.process(self.dreq(), runner=self.runner)
        self.assertEqual(r1['action'], 'upload', r1)
        self.assertEqual(self.gets, [(DROP + '/d/' + 'e' * 32, 'g' * 40, len(PNG) + 28)])
        r2 = rp.process(self.dreq(media_keys={'e' * 32: 'HASHKEY'}), runner=self.runner)
        self.assertEqual(r2['action'], 'send')
        self.assertEqual(len(self.gets), 1, 'run 2 reuses the decrypted copy; the drop is not asked again')
        self.assertEqual(r2['sends'][0]['media_finalize'][0]['media_hash_key'], 'HASHKEY')
        self.assertEqual(self.files(), [], 'decrypted copy, parts and state all deleted')

    def test_only_our_drop(self):
        self.item['url'] = 'https://elsewhere.example.invalid/d/' + 'e' * 32
        r = rp.process(self.dreq(), runner=self.runner)
        self.assertEqual(r['action'], 'send')
        self.assertEqual(self.gets, [], 'the get key never goes to another host')
        self.assertIn('didn\u2019t come through', self.calls[-1][1]['text'])

    def test_gone_tampered_or_wrong_size(self):
        for wire in (None, self.wire[:-1] + bytes([self.wire[-1] ^ 1]), self.wire + b'x'):
            self.wire, self.gets, self.calls = wire, [], []
            r = rp.process(self.dreq(payload=dict(self.req()['payload'], id='g-%d' % len(str(wire)))), runner=self.runner)
            self.assertEqual(r['action'], 'send')
            self.assertIn('didn\u2019t come through', self.calls[-1][1]['text'])
            self.assertEqual(self.files(), [])

    def test_a_prompt_without_the_photo_steps_never_gets_upload(self):
        r = rp.process(self.dreq(media_upload=False), runner=self.runner)
        self.assertEqual(r['action'], 'send', 'the text always posts')
        self.assertEqual(self.gets, [], 'nothing fetched: the drop copy just expires')
        self.assertIn('didn\u2019t come through', self.calls[-1][1]['text'])
        self.assertEqual(self.files(), [])

    def test_no_drop_configured(self):
        r = rp.process(self.req(drop_config=os.path.join(self.d, 'missing.json')), runner=self.runner)
        self.assertEqual(r['action'], 'send')
        self.assertIn('didn\u2019t come through', self.calls[-1][1]['text'])

    def test_drop_json_beside_the_dedupe_file(self):
        p = os.path.join(self.d, 'drop.json')
        with open(p, 'w') as fh:
            json.dump({'url': DROP + '/', 'get_key': 'g' * 40}, fh)
        self.assertEqual(rp.load_drop({}, os.path.join(self.d, 'seen.json'))['url'], DROP)
        self.assertIsNone(rp.load_drop({'drop': {'url': 'http://x', 'get_key': 'g' * 40}}, 'x'))

    def test_decrypt_via_another_python_key_on_stdin(self):
        out = os.path.join(self.media_dir, 'f' * 32 + '.png')
        wire, key = relay_drop.seal('f' * 32, PNG)
        seen = []

        def runner(argv, input=None, capture_output=None, timeout=None):
            seen.append((argv, input))
            return subprocess.run(argv, input=input, capture_output=True, timeout=60)
        mod = 'cryptography.hazmat.primitives.ciphers.aead'
        old = sys.modules.get(mod, 'absent')
        sys.modules[mod] = None                                # pretend this python has no cryptography
        real = rp._crypto_pythons
        rp._crypto_pythons = lambda drop: iter([sys.executable])   # the child is a separate process anyway
        try:
            ok = rp.drop_decrypt(wire, key, 'f' * 32, out, {'crypto_python': '/usr/bin/python3'}, runner)
        finally:
            rp._crypto_pythons = real
            if old == 'absent':
                del sys.modules[mod]
            else:
                sys.modules[mod] = old
        if not seen:
            self.skipTest('no second python with cryptography here')
        self.assertTrue(ok)
        with open(out, 'rb') as fh:
            self.assertEqual(fh.read(), PNG)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        self.assertNotIn(key, ' '.join(seen[0][0]), 'key not in argv')
        self.assertFalse(os.path.exists(out + '.wire'))


class Versions(unittest.TestCase):
    def test_extension_unchanged_in_147(self):
        m = fresh(tmpdir())
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, '..', '..', 'extension', 'manifest.json')) as fh:
            self.assertEqual(json.load(fh)['version'], m.rc.EXTENSION_VERSION)
        self.assertEqual(m.rc.VERSION, '1.4.23')
        with open(os.path.join(here, '..', '..', 'drop', 'core.mjs')) as fh:
            self.assertIn("export const VERSION = '1.4.7'", fh.read(), 'v1.4.8 does not change the drop')
        # the largest v2 blob (wrap header + nonce + tag) still fits the drop's MAX_BODY (4 MiB + 28)
        self.assertLessEqual(relay_drop.MAX_PLAIN + relay_drop.V2_HEADER + 28, 4 * 1024 * 1024 + 28)


class Ctl(unittest.TestCase):
    def setUp(self):
        self.m = fresh(tmpdir())
        self.ctl = self.m.ctl

    def test_set_drop_key_on_stdin_only_and_redacted(self):
        out = self.ctl.cmd_set_drop(DROP + '/', key_stdin=True, stdin=io.StringIO('  ' + 'k' * 40 + '\n'))
        self.assertNotIn('k' * 40, json.dumps(out))
        cfg = self.m.rc.load_config()
        self.assertEqual((cfg['dropUrl'], cfg['dropKey']), (DROP, 'k' * 40))
        self.assertEqual(self.ctl.public_config(cfg)['dropKey'], 'set')
        self.assertRaises(self.ctl.CtlError, self.ctl.cmd_set_drop, DROP)
        self.assertRaises(self.ctl.CtlError, self.ctl.cmd_set_drop, 'http://x', key_stdin=True, stdin=io.StringIO('k' * 40))
        self.assertRaises(self.ctl.CtlError, self.ctl.cmd_set_drop, DROP, key_stdin=True, stdin=io.StringIO('short'))
        self.ctl.cmd_set_drop(off=True)
        self.assertEqual(self.m.rc.load_config()['dropKey'], '')
        self.assertRaises(self.ctl.CtlError, self.ctl.cmd_set, 'dropKey', 'x')


if __name__ == '__main__':
    unittest.main()
