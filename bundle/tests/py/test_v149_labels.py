"""v1.4.9: a photo line always carries the sender label (never "quiet"), and a routine media_off may
carry a body-free media_error code. Replays 2026-10-19 16:45: a quiet, photo-only 1:1 message whose
upload failed in the routine posted an unnamed notice. Fictional names only; fake drop, no network."""
import json
import os
import unittest

import test_v146_media as v146
import test_v148_wrap as v148
import relay_drop  # noqa: E402
rp = v148.rp

NOTE = 'Photo didn\u2019t come through. Open Messages.'


class Labels(unittest.TestCase):
    runner = v146.RoutineMedia.runner
    req = v146.RoutineMedia.req
    files = v146.RoutineMedia.files
    getter = v148.RoutineV2.getter
    dreq = v148.RoutineV2.dreq

    def setUp(self):
        v148.RoutineV2.setUp(self)

    def quiet_req(self, text='', **over):
        r = self.dreq(**over)
        msg = dict(r['payload'], quiet=True, text=text)
        r['payload'] = {'type': 'batch', 'id': 'b-1', 'messages': [msg]}
        return r

    def log(self):
        with open(os.path.join(self.d, 'media.log')) as fh:
            return [json.loads(x)['status'] for x in fh]

    def test_replay_quiet_photo_upload_failed(self):
        r1 = rp.process(self.quiet_req(), runner=self.runner)
        self.assertEqual(r1['action'], 'upload')
        r2 = rp.process(self.quiet_req(media_off=True, media_error='initialize_403'), runner=self.runner)
        self.assertEqual(r2['action'], 'send')
        self.assertEqual(len(r2['sends']), 1, 'one post only')
        self.assertEqual(self.calls[-1][1]['text'], 'Jordan: ' + NOTE)
        self.assertEqual(self.log(), [{'upload_ready': 1}, {'routine_media_off:initialize_403': 1}])
        self.assertEqual(self.files(), [])

    def test_quiet_photo_attached_has_label_caption(self):
        rp.process(self.quiet_req(), runner=self.runner)
        r2 = rp.process(self.quiet_req(media_keys={v148.TOK: 'HASHKEY'}), runner=self.runner)
        self.assertEqual(r2['sends'][0]['media_finalize'][0]['media_hash_key'], 'HASHKEY')
        self.assertEqual(self.calls[-1][1]['text'], 'Jordan:')

    def test_quiet_photo_with_caption(self):
        rp.process(self.quiet_req(text='look'), runner=self.runner)
        rp.process(self.quiet_req(text='look', media_keys={v148.TOK: 'HASHKEY'}), runner=self.runner)
        self.assertEqual(self.calls[-1][1]['text'], 'Jordan: look')

    def test_quiet_photo_caption_with_failed_photo(self):
        rp.process(self.quiet_req(text='look', media_off=True), runner=self.runner)
        self.assertEqual(self.calls[-1][1]['text'], 'Jordan: look\n\u2002' + NOTE)

    def test_media_error_is_sanitized(self):
        rp.process(self.quiet_req(media_off=True, media_error='Append 413: <b>too big</b> ' + 'x' * 80), runner=self.runner)
        [st] = self.log()
        [code] = st
        self.assertRegex(code, r'^routine_media_off:[a-z0-9_]{1,32}$')
        self.assertNotIn('<', code)

    def test_text_lines_stay_quiet(self):
        line = rp._message_line({'label': 'Jordan', 'contactName': 'Jordan', 'text': 'on my way', 'quiet': True})
        self.assertEqual(line, 'on my way')

    def test_placeholder_only_line_is_named(self):
        line = rp._message_line({'label': 'Jordan', 'contactName': 'Jordan', 'text': '\U0001f4f7 Photo', 'quiet': True})
        self.assertEqual(line, 'Jordan: \U0001f4f7 Photo')

    def test_not_quiet_photo_only_notice(self):
        rp.process(self.dreq(media_off=True), runner=self.runner)
        self.assertEqual(self.calls[-1][1]['text'], 'Jordan: ' + NOTE)


if __name__ == '__main__':
    unittest.main()
