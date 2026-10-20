"""v1.4.19: the watcher notices a chat.db change on the next 0.25 s step, and the routine key
cache lasts 24 h. No live messages. Fictional names only."""
import os
import signal
import sys
import unittest

from helpers import fresh, tmpdir

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'routine'))
import relay_post as rp  # noqa: E402


class Wake(unittest.TestCase):
    def test_a_db_change_wakes_on_the_next_step(self):
        """Messages rewrites chat.db often. The wait must not ignore the first second."""
        m = fresh(tmpdir())
        w = m.watch.Watcher(db_path=os.path.join(tmpdir(), 'absent.db'))
        sleeps, ticks = [], []
        n = {'i': 0}

        def sig():
            n['i'] += 1
            return (n['i'],)

        w.db_signature = sig
        w.tick = lambda: ticks.append(round(sum(sleeps), 2))

        class Stop(Exception):
            pass

        def sl(s):
            sleeps.append(s)
            if sum(sleeps) >= 1.0:
                raise Stop()

        w.sleeper = sl
        old = signal.getsignal(signal.SIGTERM)
        try:
            w.run_forever()
        except Stop:
            pass
        finally:
            signal.signal(signal.SIGTERM, old)
        # 0, then a tick after every 0.25 s step. The old gate produced only the tick at 0 before 1 s.
        self.assertGreaterEqual(len(ticks), 4, ticks)
        self.assertEqual(ticks[1], 0.25)

    def test_key_cache_lasts_a_day(self):
        self.assertEqual(rp.KEY_CACHE_TTL_S, 24 * 3600)


if __name__ == '__main__':
    unittest.main()
