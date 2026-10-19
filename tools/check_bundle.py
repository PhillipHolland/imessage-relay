#!/usr/bin/env python3
"""CI guard (item 15): regenerate build_bundle.py from bundle/ into a temp file and confirm it is
byte-for-byte identical to the committed build_bundle.py. Exit 1 if they differ (someone edited the
source tree without re-running tools/pack.py)."""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
COMMITTED = os.path.join(ROOT, 'build_bundle.py')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        regen = os.path.join(tmp, 'build_bundle.py')
        subprocess.run([sys.executable, os.path.join(HERE, 'pack.py'),
                        os.path.join(ROOT, 'bundle'), regen], check=True, capture_output=True)
        a = open(COMMITTED, 'rb').read()
        b = open(regen, 'rb').read()
    if a != b:
        print('MISMATCH: build_bundle.py is stale. Run: python3 tools/pack.py', file=sys.stderr)
        return 1
    print('ok: build_bundle.py matches tools/pack.py output (%d bytes)' % len(a))
    return 0


if __name__ == '__main__':
    sys.exit(main())
