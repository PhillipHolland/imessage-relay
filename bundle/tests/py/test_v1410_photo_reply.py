"""v1.4.10: an emoji-only native Reply to a relayed photo (image bubble or its caption) reaches the host."""
from helpers import read_log
from test_v142_quote import Quote, RORY

HEARTS = '\u2764\ufe0f\u2764\ufe0f\u2764\ufe0f'


class PhotoReply(Quote):
    def test_caption_quote_routes_to_caption_name(self):
        r = self.send_q(HEARTS, 'iMessage Rory: \U0001F4F7 Photo')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.osa()[0][2:4], [HEARTS, RORY])
        self.assertNotIn(HEARTS, read_log())

    def test_image_only_quote_asks_never_silent(self):
        r = self.send_q(HEARTS, 'iMessage \U0001F4F7 Photo')
        self.assertTrue(r.get('needsConfirm') or r.get('needsTarget'), r)
        self.assertEqual(self.osa(), [])
        self.assertTrue([p for p, _ in self.posts if p['type'] in ('needs_confirm', 'needs_target')])


del Quote
