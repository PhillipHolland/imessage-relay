"""attributedBody (typedstream NSAttributedString) decoding."""
import os
import re
import unittest

from helpers import fresh, tmpdir, attributed_body, HERE


class Decode(unittest.TestCase):
    def setUp(self):
        self.ts = fresh(tmpdir()).ts

    def test_fixture_from_real_layout(self):
        with open(os.path.join(HERE, 'fixtures', 'attributed_body_note_to_self.hex')) as fh:
            h = re.sub(r'\s', '', fh.read())
        self.assertEqual(self.ts.decode_attributed_body(bytes.fromhex(h)), 'Note to self')

    def test_short_long_unicode(self):
        for s in ['hi', 'Héllo 👋 wörld', 'x' * 127, 'y' * 128, 'long ' * 300, 'line one\nline two']:
            self.assertEqual(self.ts.decode_attributed_body(attributed_body(s)), s.strip(), len(s))

    def test_32bit_length(self):
        s = 'z' * 70000
        self.assertEqual(self.ts.decode_attributed_body(attributed_body(s)), s)

    def test_attachment_placeholder_stripped(self):
        self.assertEqual(self.ts.decode_attributed_body(attributed_body('\ufffc look')), 'look')
        self.assertIsNone(self.ts.decode_attributed_body(attributed_body('\ufffc')))

    def test_legacy_fallback(self):
        # no NSString class record, only the legacy start/end markers
        blob = b'\x04\x0bstreamtyped junk \x01+\x05hello\x86\x84 tail'
        self.assertEqual(self.ts.decode_attributed_body(blob), 'hello')

    def test_garbage_never_raises(self):
        for blob in [None, b'', b'\x00\x01\x02', b'streamtyped', b'NSString\x84\x01+\x81', memoryview(b'\xff' * 50),
                     attributed_body('ok')[:60]]:
            self.ts.decode_attributed_body(blob)

    def test_message_text_preference(self):
        mt = self.ts.message_text
        self.assertEqual(mt('plain', attributed_body('other')), 'plain')
        self.assertEqual(mt(None, attributed_body('from blob')), 'from blob')
        self.assertEqual(mt(None, None, has_attachments=True), '[attachment]')
        self.assertIsNone(mt('', None))


if __name__ == '__main__':
    unittest.main()
