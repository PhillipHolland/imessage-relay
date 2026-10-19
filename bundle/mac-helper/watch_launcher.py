#!/usr/bin/env python3
"""iMessage Relay watcher launcher (run by the "iMessage Relay Watcher" app).

macOS ignores Full Disk Access granted to Apple's own Python (Python.app / python3), so the
watcher runs inside a tiny AppleScript applet, ~/Applications/iMessage Relay Watcher.app, and
the owner grants Full Disk Access to that app. The LaunchAgent starts the applet; the applet
runs `/usr/bin/python3 <this file>`; this file runs the current watcher with runpy.

The app only knows this file's path. Updating the watcher (re-running install.sh) replaces
bin/imessage_relay_watch.py and never touches the app, so the permission survives updates.
Rebuilding or re-signing the app invalidates the grant (install.sh --rebuild-watcher-app).
"""
import os
import runpy
import sys

# This file lives in <app dir>/bin (or, as a compatibility copy, directly in <app dir>).
_here = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.environ.get('IMESSAGE_RELAY_HOME') or (os.path.dirname(_here) if os.path.basename(_here) == 'bin' else _here)
WATCH = os.path.join(APP_DIR, 'bin', 'imessage_relay_watch.py')

if __name__ == '__main__':
    os.environ['IMESSAGE_RELAY_HOME'] = APP_DIR
    sys.path.insert(0, os.path.dirname(WATCH))
    sys.argv = [WATCH] + sys.argv[1:]
    runpy.run_path(WATCH, run_name='__main__')
