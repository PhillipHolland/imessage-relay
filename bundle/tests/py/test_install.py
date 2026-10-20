"""install.sh / uninstall.sh in a fake HOME (Linux-runnable: launchctl is skipped when absent).

Two suites:
  Install                  -- full installer/uninstaller behaviour (idempotency, upgrade-in-place,
                              migration, foreign-app safety, non-Mac refusal).
  InstallCopiesEveryModule -- every mac-helper/*.py lands in bin/ (the v1.3.6 drift guard), plus
                              stale-module pruning (v1.3.7).
"""
import json
import re
import os
import ast
import plistlib
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, ChatDb

APP_NAME = 'iMessage Relay Watcher.app'
# Not the live id (com.imessage-relay.watcher). The extra suffix keeps a test applet from being
# the same bundle as ~/Applications/iMessage Relay Watcher.app. uninstall.sh still removes it,
# because that live id is a prefix of this string.
TEST_BUNDLE_ID = 'com.imessage-relay.watcher.test'
LIVE_BUNDLE_ID = 'com.imessage-relay.watcher'

# Stand-ins for the macOS tools the installer uses to build the watcher app.
# These never exec, and they never register anything with Launch Services.
FAKE_OSACOMPILE = r"""#!/bin/bash
out=""; script=""
while [[ $# -gt 0 ]]; do
  case "$1" in -o) out="$2"; shift 2 ;; -e) script+="$2"$'\n'; shift 2 ;; *) shift ;; esac
done
echo "osacompile $out" >> "$HOME/tool.log"
mkdir -p "$out/Contents/MacOS" "$out/Contents/Resources"
printf '#!/bin/bash\nexit 0\n' > "$out/Contents/MacOS/applet"; chmod 755 "$out/Contents/MacOS/applet"
printf '%s' "$script" > "$out/Contents/Resources/script.txt"
cat > "$out/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.apple.ScriptEditor.id.applet</string>
<key>CFBundleExecutable</key><string>applet</string></dict></plist>
EOF
"""
FAKE_CODESIGN = '#!/bin/bash\necho "codesign $*" >> "$HOME/tool.log"\n'
# If a test forgets the fake tools, these run instead of /usr/bin/osacompile, codesign, tccutil,
# or lsregister. Exit non-zero so install.sh cannot keep going into a real code-sign.
BLOCK_TOOL = """#!/bin/bash
echo "blocked:${0##*/}" >> "$HOME/tool.log"
exit 99
"""


def shield_real_mac_tools(home, env):
    """Put non-running stand-ins ahead of /usr/bin and force a test-only bundle id."""
    bindir = os.path.join(home, 'bin')
    os.makedirs(bindir, exist_ok=True)
    for name in ('osacompile', 'codesign', 'tccutil', 'lsregister'):
        path = os.path.join(bindir, name)
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write(BLOCK_TOOL)
            os.chmod(path, 0o755)
    env['PATH'] = bindir + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    env['IMESSAGE_RELAY_WATCHER_BUNDLE_ID'] = TEST_BUNDLE_ID
    return env


def rtext(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def rj(p):
    return json.loads(rtext(p))


def wj(p, obj):
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(obj, f)


def extension_id():
    import base64
    import hashlib
    key = rj(os.path.join(ROOT, 'extension', 'manifest.json'))['key']
    h = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return ''.join(chr(ord('a') + int(c, 16)) for c in h)


class Install(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='imr-home-')
        self.support = os.path.join(self.home, 'Library', 'Application Support')
        os.makedirs(os.path.join(self.support, 'BraveSoftware', 'Brave-Browser'))   # Brave present, Edge/Arc not
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('IMESSAGE_RELAY_')}
        self.env.update(HOME=self.home, IMESSAGE_RELAY_ALLOW_NON_MAC='1', IMESSAGE_RELAY_PYTHON=sys.executable,
                        PATH='/usr/bin:/bin')
        self.app = os.path.join(self.home, '.imessage-relay')
        self.wapp = os.path.join(self.home, 'Applications', APP_NAME)
        # install.sh bootout/kickstart uses the real uid's launchd label. A test must never reach
        # /bin/launchctl (that would restart the live watcher). `open` would also pop System Settings.
        bindir = os.path.join(self.home, 'bin')
        os.makedirs(bindir)
        with open(os.path.join(bindir, 'launchctl'), 'w') as f:
            f.write('#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$HOME/launchctl.log"\nexit 1\n')
        with open(os.path.join(bindir, 'open'), 'w') as f:
            f.write('#!/bin/bash\nprintf \'%s\\n\' "$*" >> "$HOME/open.log"\nexit 0\n')
        os.chmod(os.path.join(bindir, 'launchctl'), 0o755)
        os.chmod(os.path.join(bindir, 'open'), 0o755)
        self.env['PATH'] = bindir + ':' + self.env['PATH']
        shield_real_mac_tools(self.home, self.env)

    def fake_mac_tools(self):
        d = os.path.join(self.home, 'fakebin')
        os.makedirs(d)
        for name, body in (('osacompile', FAKE_OSACOMPILE), ('codesign', FAKE_CODESIGN)):
            with open(os.path.join(d, name), 'w') as f:
                f.write(body)
            os.chmod(os.path.join(d, name), 0o755)
        self.env['PATH'] = d + ':' + self.env['PATH']

    def tool_log(self):
        p = os.path.join(self.home, 'tool.log')
        return rtext(p).splitlines() if os.path.exists(p) else []

    def plist(self):
        with open(os.path.join(self.home, 'Library', 'LaunchAgents', 'com.imessage-relay.watch.plist'), 'rb') as f:
            return plistlib.load(f)

    def sh(self, script, *args):
        return subprocess.run(['bash', os.path.join(ROOT, script)] + list(args), env=self.env, capture_output=True,
                              text=True, timeout=60)

    def manifest(self, sub):
        return rj(os.path.join(self.support, sub, 'NativeMessagingHosts', 'com.imessage_relay.host.json'))

    def test_install_idempotent_and_uninstall(self):
        self.fake_mac_tools()
        p = self.sh('install.sh', '--owner-handle', '(555) 555-0199')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        ext = extension_id()
        # files
        for f in ('relay_common.py', 'typedstream.py', 'imessage_relay_host.py', 'imessage_relay_watch.py', 'watch_launcher.py',
                  'relayctl.py', 'send_imessage.applescript'):
            self.assertTrue(os.path.isfile(os.path.join(self.app, 'bin', f)), f)
        self.assertTrue(os.access(os.path.join(self.app, 'bin', 'imessage_relay_host.py'), os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(self.app, 'extension', 'manifest.json')))
        self.assertFalse(os.path.exists(os.path.join(self.app, 'extension', 'options.html')))
        # config: safe defaults, owner normalized, no example contact
        cfg = rj(os.path.join(self.app, 'config.json'))
        self.assertEqual((cfg['dryRun'], cfg['paused'], cfg['contacts'], cfg['ownerHandle'], cfg['autoReopenMessages']),
                         (True, False, [], '+15555550199', False))
        self.assertEqual(cfg['relayConversationId'], '', 'no placeholder chat id in a real config')
        self.assertEqual(oct(os.stat(os.path.join(self.app, 'config.json')).st_mode & 0o777), '0o600')
        # host manifests: Chrome always, Brave because present, Edge/Arc not
        for sub in ('Google/Chrome', 'BraveSoftware/Brave-Browser'):
            m = self.manifest(sub)
            self.assertEqual(m['name'], 'com.imessage_relay.host')
            self.assertEqual(m['path'], os.path.join(self.app, 'bin', 'imessage_relay_host.py'))
            self.assertEqual(m['allowed_origins'], ['chrome-extension://%s/' % ext])
        self.assertFalse(os.path.exists(os.path.join(self.support, 'Microsoft Edge')))
        # watcher app: applet wrapper running the stable launcher, own bundle id, no Dock icon, ad-hoc signed
        with open(os.path.join(self.wapp, 'Contents', 'Info.plist'), 'rb') as f:
            info = plistlib.load(f)
        self.assertEqual((info['CFBundleIdentifier'], info['LSUIElement']), (TEST_BUNDLE_ID, True))
        self.assertNotEqual(info['CFBundleIdentifier'], LIVE_BUNDLE_ID)
        script = rtext(os.path.join(self.wapp, 'Contents', 'Resources', 'script.txt'))
        self.assertIn('do shell script', script)
        self.assertIn('quoted form of "%s"' % os.path.join(self.app, 'bin', 'watch_launcher.py'), script)
        self.assertIn('quoted form of "%s"' % sys.executable, script)
        self.assertTrue(script.startswith('try') and script.rstrip().endswith('end try'), 'no error dialogs')
        self.assertIn('codesign --force --deep -s - ' + self.wapp, self.tool_log())
        # LaunchAgent runs the applet (that is what holds Full Disk Access)
        pl = self.plist()
        self.assertEqual(pl['Label'], 'com.imessage-relay.watch')
        self.assertEqual(pl['ProgramArguments'], [os.path.join(self.wapp, 'Contents', 'MacOS', 'applet')])
        self.assertTrue(pl['KeepAlive'] and pl['RunAtLoad'])
        self.assertEqual(rtext(os.path.join(self.app, 'fda_binary.txt')).strip(), self.wapp)
        # next steps mention the exact things to do
        for frag in ('chrome://extensions', 'Developer mode', 'drag the folder', ext, "drag 'iMessage Relay Watcher' into Full Disk Access",
                     self.wapp, 'control Messages', 'Messages app running', 'watcher access: missing (new app)'):
            self.assertIn(frag, p.stdout)
        self.assertNotIn('Python.app', p.stdout)
        self.assertNotIn('Cmd+Shift+G', p.stdout)

        # second run: keeps config edits, adds an extra extension id, keeps the old one
        cfg['contacts'] = [{'name': 'Test Contact', 'handle': '+15555550123'}]
        cfg['dryRun'] = False
        wj(os.path.join(self.app, 'config.json'), cfg)
        marker = os.path.join(self.wapp, 'Contents', 'granted.marker')
        wj(marker, {})                                    # stands in for the app's FDA grant
        with open(os.path.join(self.app, 'fdatest.py'), 'w') as f:
            f.write('# hand-built launcher from an early test\n')
        extra = 'a' * 32
        p2 = self.sh('install.sh', '--extension-id', extra)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertTrue(os.path.exists(marker), 'existing app is never rebuilt (that would void its grant)')
        self.assertEqual(sum(l.startswith('osacompile') for l in self.tool_log()), 1)
        self.assertIn('(kept)', p2.stdout)
        shim = rtext(os.path.join(self.app, 'fdatest.py'))
        self.assertIn("'bin', 'watch_launcher.py'", shim, 'old path is a thin shim to the stable launcher')
        self.assertLess(len(shim.splitlines()), 10)
        p3 = self.sh('install.sh', '--rebuild-watcher-app')
        self.assertEqual(p3.returncode, 0, p3.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(sum(l.startswith('osacompile') for l in self.tool_log()), 2)
        cfg2 = rj(os.path.join(self.app, 'config.json'))
        self.assertEqual((cfg2['dryRun'], len(cfg2['contacts'])), (False, 1))
        self.assertEqual(cfg2.get('relayConversationId', ''), '', 'new-user template: no legacy migration')
        self.assertNotIn('xConversationId', json.dumps(cfg2))
        self.assertEqual((cfg2['replyWindowMinutes'], cfg2['confirmSends']), (120, True))
        self.assertEqual(self.manifest('Google/Chrome')['allowed_origins'],
                         ['chrome-extension://%s/' % ext, 'chrome-extension://%s/' % extra])
        # relayctl wrapper works
        r = subprocess.run([os.path.join(self.app, 'relayctl'), 'contacts'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout)['contacts'][0]['handle'], '+15555550123')
        # bad option
        self.assertNotEqual(self.sh('install.sh', '--extension-id', 'bad').returncode, 0)

        # uninstall (keep config), then full uninstall; both idempotent
        u = self.sh('uninstall.sh', '--keep-config')
        self.assertEqual(u.returncode, 0, u.stderr)
        self.assertEqual(os.listdir(self.app), ['config.json'])
        self.assertFalse(os.path.exists(self.wapp), 'watcher app removed')
        self.assertFalse(os.path.exists(os.path.join(self.support, 'Google/Chrome/NativeMessagingHosts/com.imessage_relay.host.json')))
        self.assertFalse(os.path.exists(os.path.join(self.home, 'Library', 'LaunchAgents', 'com.imessage-relay.watch.plist')))
        self.assertEqual(self.sh('uninstall.sh').returncode, 0)
        self.assertFalse(os.path.exists(self.app))
        self.assertEqual(self.sh('uninstall.sh').returncode, 0)
        self.assertFalse(any('tccutil' in ln for ln in self.tool_log()))

    def test_uninstall_keep_logs_and_cleans_extras(self):
        # item 14: default removes the log; --keep-logs keeps it; extracted src + bundle are removed.
        os.makedirs(self.app, exist_ok=True)
        wj(os.path.join(self.app, 'config.json'), {'contacts': []})
        logp = os.path.join(self.home, 'Library', 'Logs', 'imessage-relay.log')
        os.makedirs(os.path.dirname(logp), exist_ok=True)
        with open(logp, 'w') as fh:
            fh.write('{"event":"tick"}\n')
        src = os.path.join(self.home, 'imessage-relay-src')
        os.makedirs(src, exist_ok=True)
        bundlef = os.path.join(self.home, 'imessage-relay-build_bundle.py')
        with open(bundlef, 'w') as fh:
            fh.write('x')
        u = self.sh('uninstall.sh', '--keep-config', '--keep-logs')
        self.assertEqual(u.returncode, 0, u.stderr)
        self.assertTrue(os.path.exists(logp), '--keep-logs keeps the log')
        self.assertFalse(os.path.exists(src), 'extracted source is removed')
        self.assertFalse(os.path.exists(bundlef), 'the single-file bundle is removed')
        self.assertEqual(os.listdir(self.app), ['config.json'], '--keep-config keeps the webhook key')
        u2 = self.sh('uninstall.sh')
        self.assertEqual(u2.returncode, 0, u2.stderr)
        self.assertFalse(os.path.exists(logp), 'the log is removed by default')
        self.assertFalse(os.path.exists(self.app))
        self.assertFalse(any('tccutil' in ln for ln in self.tool_log()))

    def test_launcher_runs_watcher_and_status_reads_the_log(self):
        self.env['IMESSAGE_RELAY_SKIP_WATCHER_APP'] = '1'   # do not compile, even if osacompile exists
        p = self.sh('install.sh', '--no-launchagent')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('watcher app: not built', p.stdout)
        self.assertEqual(self.tool_log(), [])
        self.assertFalse(os.path.exists(self.wapp))
        launcher = os.path.join(self.app, 'bin', 'watch_launcher.py')
        self.assertEqual(self.plist()['ProgramArguments'], [sys.executable, launcher])
        env = dict(self.env)
        once = lambda path: subprocess.run([sys.executable, path, '--once'], env=env, capture_output=True, text=True, timeout=30)  # noqa: E731
        status = lambda: json.loads(subprocess.run([os.path.join(self.app, 'relayctl'), 'status'], env=env,  # noqa: E731
                                                   capture_output=True, text=True, timeout=30).stdout)
        st = status()
        self.assertIsNone(st['watcherCanReadChatDb'])
        self.assertIn('watcher app missing', ' | '.join(st['problems']))
        r = once(launcher)                                  # no chat.db in this HOME = like missing FDA
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('scanned', json.loads(r.stdout))
        st = status()
        self.assertEqual((st['watcherCanReadChatDb'], st['watcherChatDbEvent']['event']), (False, 'chat_db_unavailable'))
        self.assertIn("drag 'iMessage Relay Watcher'", ' | '.join(st['problems']))
        os.makedirs(os.path.join(self.home, 'Library', 'Messages'))
        ChatDb(os.path.join(self.home, 'Library', 'Messages', 'chat.db'))
        compat = os.path.join(self.app, 'fdatest.py')        # the old app's path: install turns it into a shim
        with open(compat, 'w') as f:
            f.write('# hand-built\n')
        self.assertEqual(self.sh('install.sh', '--no-launchagent').returncode, 0)
        r = once(compat)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('scanned', json.loads(r.stdout), 'shim runs the real watcher')
        st = status()
        self.assertTrue(st['watcherCanReadChatDb'])
        self.assertIn(st['watcherChatDbEvent']['event'], ('watch_baseline', 'chat_db_ok'))
        self.assertNotIn('cannot read chat.db', ' | '.join(st['problems']))

    def test_upgrade_in_place_keeps_the_live_app_config_and_roster(self):
        # Layout of a Mac running an earlier version: a hand-built watcher app at its own path (holding
        # the FDA grant) started by our LaunchAgent and running ~/.imessage-relay/fdatest.py.
        self.fake_mac_tools()
        live = os.path.join(self.home, 'Applications', 'Hand Built Watcher.app')
        os.makedirs(os.path.join(live, 'Contents', 'MacOS'))
        with open(os.path.join(live, 'Contents', 'MacOS', 'applet'), 'w') as f:
            f.write('#!/bin/bash\nexit 0\n')
        os.chmod(os.path.join(live, 'Contents', 'MacOS', 'applet'), 0o755)
        with open(os.path.join(live, 'Contents', 'Info.plist'), 'wb') as f:
            plistlib.dump({'CFBundleIdentifier': 'com.example.handmade'}, f)
        info_before = rtext(os.path.join(live, 'Contents', 'Info.plist'))
        agents = os.path.join(self.home, 'Library', 'LaunchAgents')
        os.makedirs(agents)
        with open(os.path.join(agents, 'com.imessage-relay.watch.plist'), 'wb') as f:
            plistlib.dump({'Label': 'com.imessage-relay.watch', 'ProgramArguments': [os.path.join(live, 'Contents', 'MacOS', 'applet')]}, f)
        with open(os.path.join(agents, 'com.example.test-watch.plist'), 'wb') as f:
            plistlib.dump({'Label': 'com.example.test-watch', 'ProgramArguments': ['/usr/bin/python3', self.app + '/fdatest.py']}, f)
        os.makedirs(self.app)
        with open(os.path.join(self.app, 'fdatest.py'), 'w') as f:
            f.write('# hand-built launcher\n')
        wj(os.path.join(self.app, 'config.json'), {'relayAll': True, 'dryRun': False, 'relayConversationId': '1111-2222',
                                                  'contacts': [{'name': 'Test Contact', 'handle': '+15555550123'}]})
        roster = os.path.join(self.app, 'roster.json')
        wj(roster, {'version': 1, 'entries': [{'handle': '+15555550150', 'label': 'Pat', 'lastInboundAt': 1.0}]})
        roster_before = rtext(roster)
        p = self.sh('install.sh')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.tool_log(), [], 'no osacompile, no codesign: the app is not rebuilt or re-signed')
        self.assertEqual(rtext(os.path.join(live, 'Contents', 'Info.plist')), info_before)
        self.assertFalse(os.path.exists(self.wapp), 'no second app built at the default path')
        self.assertEqual(self.plist()['ProgramArguments'], [os.path.join(live, 'Contents', 'MacOS', 'applet')])
        self.assertEqual(rtext(os.path.join(self.app, 'fda_binary.txt')).strip(), live)
        self.assertEqual(rtext(roster), roster_before, 'roster untouched')
        cfg = rj(os.path.join(self.app, 'config.json'))
        self.assertEqual((cfg['relayAll'], cfg['dryRun'], cfg['rosterDays'], cfg['mute'], cfg['skipShortCodes']),
                         (True, False, 30, [], True))
        self.assertIn("'bin', 'watch_launcher.py'", rtext(os.path.join(self.app, 'fdatest.py')))
        self.assertIn('com.example.test-watch.plist also starts the relay watcher', p.stdout)
        self.assertIn('(kept)', p.stdout)
        # uninstall removes only what we built: the hand-made app (other bundle id) stays
        u = self.sh('uninstall.sh')
        self.assertTrue(os.path.exists(live))
        self.assertIn('Left', u.stdout)

    def test_upgrade_migrates_last_inbound_to_roster(self):
        self.env['IMESSAGE_RELAY_SKIP_WATCHER_APP'] = '1'
        os.makedirs(self.app)
        wj(os.path.join(self.app, 'last_inbound.json'), {'recent': [{'handle': '+1 555-555-0123', 'at': 5.0}]})
        p = self.sh('install.sh', '--no-launchagent')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.tool_log(), [])
        self.assertFalse(os.path.exists(self.wapp))
        self.assertFalse(os.path.exists(os.path.join(self.app, 'last_inbound.json')))
        self.assertEqual(rj(os.path.join(self.app, 'roster.json'))['entries'],
                         [{'handle': '+15555550123', 'label': '', 'lastInboundAt': 5.0}])
        self.assertEqual(rj(os.path.join(self.app, 'config.json'))['relayAll'], False, 'template default')

    def test_uninstall_leaves_a_foreign_app_alone(self):
        os.makedirs(os.path.join(self.wapp, 'Contents'))
        with open(os.path.join(self.wapp, 'Contents', 'Info.plist'), 'wb') as f:
            plistlib.dump({'CFBundleIdentifier': 'com.example.other'}, f)
        u = self.sh('uninstall.sh')
        self.assertEqual(u.returncode, 0, u.stderr)
        self.assertTrue(os.path.exists(self.wapp))
        self.assertIn('Left', u.stdout)
        self.assertFalse(any('tccutil' in ln for ln in self.tool_log()))


    def test_prunes_stale_bin_modules_but_keeps_everything_else(self):
        # v1.3.7: a module removed from the bundle is pruned from bin/ on the next install, while config,
        # roster, the fdatest shim, the applescript and live modules are untouched.
        self.env['IMESSAGE_RELAY_SKIP_WATCHER_APP'] = '1'
        p = self.sh('install.sh', '--no-launchagent')
        self.assertEqual(p.returncode, 0, p.stderr)
        binp = os.path.join(self.app, 'bin')
        stale = os.path.join(binp, 'relay_removed_module.py')
        with open(stale, 'w') as f:
            f.write('x = 1\n')
        wj(os.path.join(self.app, 'config.json'),
           {'contacts': [{'name': 'Keep Me', 'handle': '+15555550123'}], 'dryRun': False})
        roster = os.path.join(self.app, 'roster.json')
        wj(roster, {'version': 1, 'entries': [{'handle': '+15555550150', 'label': 'Pat', 'lastInboundAt': 1.0}]})
        roster_before = rtext(roster)
        with open(os.path.join(self.app, 'fdatest.py'), 'w') as f:
            f.write('# hand-built\n')
        p2 = self.sh('install.sh', '--no-launchagent')
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertFalse(os.path.exists(stale), 'stale module pruned from bin/')
        self.assertIn('pruned', p2.stdout)
        # live modules and the applescript survive
        self.assertTrue(os.path.exists(os.path.join(binp, 'relay_common.py')))
        self.assertTrue(os.path.exists(os.path.join(binp, 'relay_directive.py')))
        self.assertTrue(os.path.exists(os.path.join(binp, 'send_imessage.applescript')))
        # config, roster and the shim are never touched by pruning
        cfg = rj(os.path.join(self.app, 'config.json'))
        self.assertEqual((cfg['dryRun'], cfg['contacts'][0]['name']), (False, 'Keep Me'))
        self.assertEqual(rtext(roster), roster_before, 'roster untouched')
        self.assertIn("'bin', 'watch_launcher.py'", rtext(os.path.join(self.app, 'fdatest.py')))
        self.assertEqual(self.tool_log(), [])
        self.assertFalse(os.path.exists(self.wapp))

    def test_refuses_live_bundle_id_outside_the_live_app(self):
        # Fake compilers only: if the guard fails open, the log shows osacompile and the real
        # tool is still not on PATH. A temp-home build with the live bundle id is refused even
        # with --rebuild-watcher-app, because that flag rebuilds only the installed app.
        self.fake_mac_tools()
        self.env.pop('IMESSAGE_RELAY_WATCHER_BUNDLE_ID', None)
        p = self.sh('install.sh', '--no-launchagent')
        self.assertEqual(p.returncode, 5, p.stderr + p.stdout)
        self.assertIn('refusing to build or sign', p.stderr)
        self.assertEqual(self.tool_log(), [])
        self.assertFalse(os.path.exists(self.wapp))
        p2 = self.sh('install.sh', '--no-launchagent', '--rebuild-watcher-app')
        self.assertEqual(p2.returncode, 5, p2.stderr + p2.stdout)
        self.assertEqual(self.tool_log(), [])
        self.assertFalse(os.path.exists(self.wapp))

    def test_blocker_runs_instead_of_real_osacompile(self):
        p = self.sh('install.sh', '--no-launchagent')
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(any(ln == 'blocked:osacompile' for ln in self.tool_log()), self.tool_log())
        self.assertFalse(any('tccutil' in ln or ln.startswith('blocked:codesign') for ln in self.tool_log()))
        self.assertFalse(os.path.exists(os.path.join(self.wapp, 'Contents', 'MacOS', 'applet')))

    def test_installer_source_refuses_to_touch_the_live_app(self):
        src = rtext(os.path.join(ROOT, 'install.sh'))
        un = rtext(os.path.join(ROOT, 'uninstall.sh'))
        self.assertIn('WAPP_ID="com.imessage-relay.watcher"', src)
        self.assertIn('refusing to rebuild or re-sign the live watcher app without --rebuild-watcher-app', src)
        self.assertIn('refusing to build or sign the live watcher bundle id', src)
        self.assertNotIn('lsregister', src)
        self.assertNotIn('lsregister', un)
        self.assertIn('[[ "${IMESSAGE_RELAY_ALLOW_NON_MAC:-}" != "1" ]] && command -v tccutil', un)
        self.assertLess(un.index('tccutil reset'), un.index('client-secret'))

    def test_loaded_applet_restarts_with_kickstart_not_bootout(self):
        # v1.4.23: an upgrade whose LaunchAgent already runs the watcher applet must restart with
        # `launchctl kickstart -k` and must not `bootout`. bootout is what unloaded the agent and,
        # when it hung, got the applet's Full Disk Access grant deleted. A job that is not that
        # applet still bootstraps (first install, or a different program).
        self.fake_mac_tools()
        ctl = os.path.join(self.home, 'bin', 'launchctl')
        with open(ctl, 'w') as f:
            f.write(r"""#!/bin/bash
printf '%s\n' "$*" >> "$HOME/launchctl.log"
if [[ "$1" == "print" ]]; then
  if [[ -f "$HOME/loaded-program" ]]; then
    printf '\tprogram = %s\n' "$(cat "$HOME/loaded-program")"
    exit 0
  fi
  exit 1
fi
if [[ "$1" == "kickstart" || "$1" == "bootstrap" ]]; then
  mkdir -p "$HOME/Library/Logs"
  printf '%s\n' '{"event":"chat_db_ok"}' >> "$HOME/Library/Logs/imessage-relay.log"
fi
exit 0
""")
        os.chmod(ctl, 0o755)
        logp = os.path.join(self.home, 'launchctl.log')

        def calls():
            return rtext(logp).splitlines() if os.path.exists(logp) else []

        first = self.sh('install.sh', '--no-open-settings')
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        applet = os.path.join(self.wapp, 'Contents', 'MacOS', 'applet')
        self.assertTrue(os.path.isfile(applet))
        got = calls()
        self.assertTrue(any(l.startswith('bootout ') for l in got), got)
        self.assertTrue(any(l.startswith('bootstrap ') for l in got), got)
        self.assertFalse(any(l.startswith('kickstart') for l in got), got)

        with open(os.path.join(self.home, 'loaded-program'), 'w') as f:
            f.write(applet)
        os.remove(logp)
        second = self.sh('install.sh', '--no-open-settings')
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertIn('watcher:   restarted', second.stdout)
        got = calls()
        self.assertTrue(any(l.startswith('kickstart -k ') for l in got), got)
        self.assertFalse(any(l.startswith('bootout') for l in got), got)
        self.assertFalse(any(l.startswith('bootstrap') for l in got), got)
        self.assertIn('chat.db:   watcher access: ok', second.stdout)

        with open(os.path.join(self.home, 'loaded-program'), 'w') as f:
            f.write('/usr/bin/python3')
        os.remove(logp)
        third = self.sh('install.sh', '--no-open-settings')
        self.assertEqual(third.returncode, 0, third.stderr + third.stdout)
        got = calls()
        self.assertTrue(any(l.startswith('bootout ') for l in got), got)
        self.assertTrue(any(l.startswith('bootstrap ') for l in got), got)
        self.assertFalse(any(l.startswith('kickstart') for l in got), got)
        self.assertEqual(self.plist()['ProgramArguments'], [applet])

    def test_refuses_non_mac_without_override(self):
        env = dict(self.env)
        env.pop('IMESSAGE_RELAY_ALLOW_NON_MAC')
        if sys.platform == 'darwin':
            self.skipTest('running on a Mac')
        p = subprocess.run(['bash', os.path.join(ROOT, 'install.sh')], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)


# ---------- v1.3.6+ : the installer must copy EVERY mac-helper module into bin/ (no stale list) ----------
_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLE = os.path.abspath(os.path.join(_HERE, '..', '..'))        # the unpacked bundle/ root
_INSTALL = os.path.join(_BUNDLE, 'install.sh')
_MAC = os.path.join(_BUNDLE, 'mac-helper')


def _mac_py_stems():
    return sorted(f[:-3] for f in os.listdir(_MAC) if f.endswith('.py'))


@unittest.skipUnless(os.path.exists(_INSTALL), 'install.sh not present')
class InstallCopiesEveryModule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix='imr-install-')
        # Build a CLEAN env: other tests leak IMESSAGE_RELAY_HOME/LOG/CHAT_DB into os.environ, which would
        # redirect the installer away from $HOME/.imessage-relay. Drop every IMESSAGE_RELAY_* first.
        env = {k: v for k, v in os.environ.items() if not k.startswith('IMESSAGE_RELAY_')}
        env.update(HOME=cls.home, IMESSAGE_RELAY_ALLOW_NON_MAC='1',
                   IMESSAGE_RELAY_PYTHON=sys.executable, IMESSAGE_RELAY_SKIP_WATCHER_APP='1')
        shield_real_mac_tools(cls.home, env)
        cls.proc = subprocess.run(
            ['bash', _INSTALL, '--owner-handle', '+1 555 555 0123', '--no-launchagent'],
            env=env, cwd=_BUNDLE, capture_output=True, text=True, timeout=120)
        cls.bin = os.path.join(cls.home, '.imessage-relay', 'bin')

    def test_installer_succeeded(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr[-2000:])
        self.assertIn('watcher app: not built', self.proc.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'tool.log')))

    def test_every_mac_helper_py_landed_in_bin(self):
        installed = set(os.listdir(self.bin)) if os.path.isdir(self.bin) else set()
        for stem in _mac_py_stems():
            self.assertIn(stem + '.py', installed, '%s.py was not installed into bin/' % stem)

    def test_applescript_landed(self):
        self.assertTrue(os.path.exists(os.path.join(self.bin, 'send_imessage.applescript')))

    def test_entry_points_import_cleanly_from_bin(self):
        # the real entry points + the lazy re-exports that broke in 1.3.5
        code = ("import relay_common, relay_directive, relay_echo, imessage_relay_host, "
                "imessage_relay_watch, relayctl, watch_launcher, relay_send, typedstream, relay_test, relay_setup; "
                "relay_common.parse_directive; relay_common.is_echoed_inbound; print('ok')")
        r = subprocess.run([sys.executable, '-c', code], cwd=self.bin, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('ok', r.stdout)

    def test_no_installed_script_imports_a_missing_local_module(self):
        # Any module that exists as a mac-helper .py is a LOCAL module; every local module an installed
        # script imports must itself be installed in bin/. This catches a new module added to one file
        # but forgotten by the installer.
        local = set(_mac_py_stems())
        installed = {f[:-3] for f in os.listdir(self.bin) if f.endswith('.py')}
        missing = []
        for stem in installed:
            with open(os.path.join(self.bin, stem + '.py')) as _fh:
                src = _fh.read()
            for node in ast.walk(ast.parse(src)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split('.')[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split('.')[0]]
                for n in names:
                    if n in local and n not in installed:
                        missing.append('%s imports %s (not in bin/)' % (stem, n))
        self.assertEqual(missing, [], '; '.join(missing))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.home, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()


class InstallDetectsExistingAndBrief(unittest.TestCase):
    """v1.3.8 guided setup: the installer says whether it is a fresh install or an in-place upgrade
    (and from which version), prints one body-free summary line, and --brief prints only the short
    owner checklist."""

    def run_install(self, home, *extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith('IMESSAGE_RELAY_')}
        env.update(HOME=home, IMESSAGE_RELAY_ALLOW_NON_MAC='1', IMESSAGE_RELAY_PYTHON=sys.executable,
                   IMESSAGE_RELAY_SKIP_WATCHER_APP='1')
        shield_real_mac_tools(home, env)
        return subprocess.run(['bash', _INSTALL, '--no-launchagent'] + list(extra), env=env, cwd=_BUNDLE,
                              capture_output=True, text=True, timeout=120)

    def test_fresh_then_upgrade_in_place(self):
        home = tempfile.mkdtemp(prefix='imr-install-')
        p1 = self.run_install(home)
        self.assertEqual(p1.returncode, 0, p1.stderr[-2000:])
        self.assertIn('previous:  none: fresh install', p1.stdout)
        self.assertIn('watcher app: not built', p1.stdout)
        self.assertFalse(os.path.exists(os.path.join(home, 'tool.log')))
        self.assertIn('"upgrade":false', p1.stdout)
        # pretend the installed copy is an older release: the next run must report an upgrade from it
        rcp = os.path.join(home, '.imessage-relay', 'bin', 'relay_common.py')
        with open(rcp) as fh:
            src = fh.read()
        with open(rcp, 'w') as fh:
            fh.write(re.sub(r"^VERSION = '[^']*'", "VERSION = '1.3.5'", src, count=1, flags=re.M))
        cfgp = os.path.join(home, '.imessage-relay', 'config.json')
        with open(cfgp) as fh:
            cfg = json.load(fh)
        cfg['relayConversationId'] = '1234-5678'
        with open(cfgp, 'w') as fh:
            json.dump(cfg, fh)
        p2 = self.run_install(home, '--brief')
        self.assertEqual(p2.returncode, 0, p2.stderr[-2000:])
        self.assertIn('previous:  v1.3.5 found: upgrading in place', p2.stdout)
        self.assertIn('"previous":"1.3.5","upgrade":true', p2.stdout)
        with open(cfgp) as fh:
            self.assertEqual(json.load(fh)['relayConversationId'], '1234-5678', 'config kept on upgrade')
        self.assertIn('== Your checklist ==', p2.stdout)
        self.assertIn('Cmd+R', p2.stdout)
        self.assertIn('/test', p2.stdout)
        self.assertNotIn('== Next steps ==', p2.stdout)
