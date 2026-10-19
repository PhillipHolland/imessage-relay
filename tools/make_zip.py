#!/usr/bin/env python3
"""Deterministic release zip for iMessage Relay (item 15).

Extracts the committed build_bundle.py into a temp tree (the same bytes a Mac user would get),
then writes a byte-for-byte reproducible zip: files in sorted order, a fixed 1980-01-01 timestamp,
and explicit Unix modes (755 for scripts, 644 otherwise). Includes tests/ so run_tests.sh works.

Usage: python3 tools/make_zip.py [out_zip]   (default /workspace/imr-out/imessage-relay-<ver>.zip)
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
BUNDLE_PY = os.path.join(ROOT, 'build_bundle.py')
VERSION = re.search(r"VERSION = '([^']+)'", open(os.path.join(ROOT, 'bundle', 'mac-helper', 'relay_common.py')).read()).group(1)
TOPDIR = 'imessage-relay-' + VERSION


def main(argv):
    out = os.path.abspath(argv[0]) if argv else '/workspace/imr-out/imessage-relay-%s.zip' % VERSION
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, 'tree')
        subprocess.run([sys.executable, BUNDLE_PY, src], check=True, capture_output=True)
        files = []
        for root, dirs, names in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            for n in sorted(names):
                if n.endswith('.pyc'):
                    continue
                full = os.path.join(root, n)
                rel = os.path.relpath(full, src).replace(os.sep, '/')
                files.append((rel, full))
        files.sort()
        if os.path.exists(out):
            os.remove(out)
        with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for rel, full in files:
                with open(full, 'rb') as f:
                    data = f.read()
                base = rel.rsplit('/', 1)[-1]
                executable = rel.endswith('.sh') or base in ('imessage_relay_host.py', 'imessage_relay_watch.py',
                                                             'watch_launcher.py', 'relayctl.py')
                info = zipfile.ZipInfo(TOPDIR + '/' + rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3                        # Unix, so the mode bits are honored on extract
                mode = (0o755 if executable else 0o644) | 0o100000   # S_IFREG: mark as a regular file
                info.external_attr = (mode & 0xFFFF) << 16
                z.writestr(info, data)
    sha = hashlib.sha256(open(out, 'rb').read()).hexdigest()
    print('wrote %s (%d bytes)' % (out, os.path.getsize(out)))
    print('sha256 %s' % sha)
    print('files  %d under %s/' % (len(files), TOPDIR))


if __name__ == '__main__':
    main(sys.argv[1:])
