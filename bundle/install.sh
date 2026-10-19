#!/bin/bash
# iMessage Relay v1.3.8 installer (macOS, current user, no sudo). Idempotent: safe to re-run.
# Upgrades in place: replaces program files and the extension copy; keeps config.json,
# roster.json, and the existing "iMessage Relay Watcher" app (never rebuilt or re-signed, so
# its Full Disk Access grant survives), and keeps the LaunchAgent pointing at that app.
#
# Usage: ./install.sh [--owner-handle "+1 555 555 0123"] [--extension-id <32 letters a-p>]...
#                     [--no-launchagent] [--open-settings|--no-open-settings]
#                     [--rebuild-watcher-app] [--brief]
#   --owner-handle    your own iMessage number/email (used only by the self-test)
#   --extension-id    extra extension id(s) to allow (the bundled extension has a fixed id:
#                     gpnjllicnleeogdmgpjekgflakdiifee); existing allowed ids are kept
#   --no-launchagent  do not (re)load the inbound watcher
#   --open-settings   always open System Settings > Full Disk Access and reveal the watcher app
#                     (default: only when the watcher cannot read chat.db yet)
#   --no-open-settings never open them
#   --rebuild-watcher-app  rebuild ~/Applications/iMessage Relay Watcher.app. Rebuilding (re-signing)
#                     invalidates its Full Disk Access grant: drag it in again afterwards.
#   --brief           guided setup (v1.3.8): print only a short checklist instead of the long
#                     "Next steps" (the bot runs `relayctl setup` right after this)
# An existing install is detected (bin/relay_common.py VERSION) and upgraded in place.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="${IMESSAGE_RELAY_HOME:-$HOME/.imessage-relay}"
BIN="$APP/bin"
EXT_DIR="$APP/extension"
DEFAULT_EXT_ID="gpnjllicnleeogdmgpjekgflakdiifee"
PY="${IMESSAGE_RELAY_PYTHON:-/usr/bin/python3}"
LABEL="com.imessage-relay.watch"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
LOGS="$HOME/Library/Logs"
SUPPORT="$HOME/Library/Application Support"
WAPP_DIR="$HOME/Applications"
WAPP="$WAPP_DIR/iMessage Relay Watcher.app"
WAPP_ID="com.imessage-relay.watcher"

OWNER=""; EXTRA_IDS=(); LOAD_AGENT=1; OPEN_SETTINGS=auto; REBUILD_APP=0; BRIEF=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner-handle) OWNER="${2:-}"; shift 2 ;;
    --extension-id)
      if [[ ! "${2:-}" =~ ^[a-p]{32}$ ]]; then echo "error: extension id must be 32 letters a-p" >&2; exit 1; fi
      EXTRA_IDS+=("$2"); shift 2 ;;
    --no-launchagent) LOAD_AGENT=0; shift ;;
    --open-settings) OPEN_SETTINGS=always; shift ;;
    --no-open-settings) OPEN_SETTINGS=never; shift ;;
    --rebuild-watcher-app) REBUILD_APP=1; shift ;;
    --brief) BRIEF=1; shift ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "error: unknown option $1" >&2; exit 1 ;;
  esac
done

# macOS has no `timeout` command: run a command with a time limit via perl alarm.
with_timeout() { local s="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$s" "$@"; }

if [[ "$(uname -s)" != "Darwin" && "${IMESSAGE_RELAY_ALLOW_NON_MAC:-}" != "1" ]]; then
  echo "error: iMessage Relay runs on macOS only." >&2; exit 2
fi
if [[ "$(uname -s)" == "Darwin" && "$PY" == "/usr/bin/python3" ]] && ! xcode-select -p >/dev/null 2>&1; then
  echo "error: /usr/bin/python3 needs the Command Line Tools. Run: xcode-select --install  (then re-run this installer)" >&2
  exit 3
fi
[[ -d "$SRC/mac-helper" && -d "$SRC/extension" ]] || { echo "error: run from the unpacked bundle (needs mac-helper/ and extension/)" >&2; exit 1; }

echo "== iMessage Relay installer =="
# Existing install? (read before the program files are replaced) -> upgrade in place.
PREV_VERSION=""
if [[ -f "$BIN/relay_common.py" ]]; then
  PREV_VERSION="$(sed -n "s/^VERSION = '\([^']*\)'.*/\1/p" "$BIN/relay_common.py" | head -n 1)"
  [[ -n "$PREV_VERSION" ]] || PREV_VERSION="unknown"
fi
NEW_VERSION="$(sed -n "s/^VERSION = '\([^']*\)'.*/\1/p" "$SRC/mac-helper/relay_common.py" | head -n 1)"
if [[ -n "$PREV_VERSION" ]]; then
  echo "previous:  v$PREV_VERSION found: upgrading in place to v$NEW_VERSION (config, roster and watcher app kept)"
else
  echo "previous:  none: fresh install of v$NEW_VERSION"
fi
mkdir -p "$BIN" "$EXT_DIR" "$LOGS" "$APP/jobs" "$APP/selftest"
chmod 700 "$APP"

# 1) program files (overwritten on every run) + extension copy.
# Copy EVERY mac-helper Python module plus the AppleScript sender, so a newly added module (e.g.
# relay_directive.py, relay_echo.py) can never be left behind by a stale hard-coded list. The non-.py
# templates (config.example.json, *.template) are read straight from the bundle during install.
shopt -s nullglob
for f in "$SRC"/mac-helper/*.py "$SRC"/mac-helper/*.applescript; do
  cp "$f" "$BIN/$(basename "$f")"
done
# Prune any stale *.py left in bin/ by an earlier version whose module was since removed from the bundle,
# so a deleted module can never keep being imported. This only ever touches bin/*.py files: config.json,
# roster.json and logs live in $APP (not here), the fdatest shim is $APP/fdatest.py (not here), and the
# applet lives inside the watcher app, so none of them can be affected. send_imessage.applescript is kept.
for existing in "$BIN"/*.py; do
  base="$(basename "$existing")"
  if [[ ! -f "$SRC/mac-helper/$base" ]]; then
    rm -f "$existing"
    echo "pruned:    stale $base (no longer in the bundle)"
  fi
done
shopt -u nullglob
chmod 755 "$BIN/imessage_relay_host.py" "$BIN/imessage_relay_watch.py" "$BIN/watch_launcher.py" "$BIN/relayctl.py"
# Compatibility: an early hand-built watcher app runs `/usr/bin/python3 ~/.imessage-relay/fdatest.py`.
# Keep that path working as a thin shim to the stable launcher, so that app and its Full Disk
# Access grant keep working without a rebuild.
if [[ -f "$APP/fdatest.py" ]]; then
  cat > "$APP/fdatest.py" <<'SHIM'
#!/usr/bin/env python3
# iMessage Relay compatibility shim: an early "iMessage Relay Watcher" app runs this path.
# It only hands over to the stable launcher (bin/watch_launcher.py), which runs the watcher.
import os
import runpy
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'watch_launcher.py'), run_name='__main__')
SHIM
  chmod 755 "$APP/fdatest.py"
  echo "compat:    $APP/fdatest.py (shim -> bin/watch_launcher.py)"
fi
rm -rf "$EXT_DIR"; mkdir -p "$EXT_DIR"
for f in manifest.json content.js background.js popup.html popup.js popup.css; do cp "$SRC/extension/$f" "$EXT_DIR/$f"; done
printf '#!/bin/bash\nexec %q %q "$@"\n' "$PY" "$BIN/relayctl.py" > "$APP/relayctl"   # %q: safe even if a path has spaces
chmod 755 "$APP/relayctl"
echo "files:     $BIN"
echo "extension: $EXT_DIR"

# 2) config: create with safe defaults (dryRun on, no contacts) or add new keys to an existing one
"$PY" - "$APP/config.json" "$SRC/mac-helper/config.example.json" "$OWNER" <<'PY'
import json, os, sys
path, example, owner = sys.argv[1:4]
sys.path.insert(0, os.path.join(os.path.dirname(path), 'bin'))
import relay_common as rc
defaults = json.load(open(example))
defaults['contacts'] = []
defaults['relayConversationId'] = ''
try:
    cfg = json.load(open(path))
    created = False
except FileNotFoundError:
    cfg, created = {}, True
except ValueError:
    os.replace(path, path + '.broken')
    print('warning: config.json was not valid JSON; saved as config.json.broken and started fresh')
    cfg, created = {}, True
for k, v in defaults.items():
    cfg.setdefault(k, v)
if owner:
    h = rc.normalize_handle(owner, cfg.get('defaultCountryCode', '1'))
    if not rc.valid_handle(h):
        sys.exit('error: --owner-handle is not a valid phone number or email')
    cfg['ownerHandle'] = h
rc.write_json_atomic(path, cfg)
# v1.1 kept the last relayed senders in last_inbound.json; v1.2 keeps them in roster.json.
app_dir = os.path.dirname(path)
legacy, roster_path = os.path.join(app_dir, 'last_inbound.json'), os.path.join(app_dir, 'roster.json')
if os.path.exists(legacy):
    if not os.path.exists(roster_path):
        old = (rc.read_json(legacy, {}) or {}).get('recent') or []
        rc.save_roster([{'handle': rc.normalize_handle(r.get('handle')), 'label': '', 'lastInboundAt': r.get('at')}
                        for r in old if isinstance(r, dict) and r.get('handle')], roster_path)
        print('roster:    migrated %d entries from last_inbound.json' % len(old))
    os.remove(legacy)
c = rc.load_config(path)
print('config:    %s (%s; dryRun=%s, paused=%s, contacts=%d)' % (path, 'created' if created else 'kept', c['dryRun'], c['paused'], len(c['contacts'])))
PY

# 3) native messaging host manifest: Chrome always; Chrome Beta/Canary, Chromium, Brave, Edge, Arc if present
"$PY" - "$SRC/mac-helper/com.imessage_relay.host.json.template" "$BIN/imessage_relay_host.py" "$SUPPORT" "$DEFAULT_EXT_ID" "${EXTRA_IDS[@]+"${EXTRA_IDS[@]}"}" <<'PY'
import json, os, sys
tpl, host, support, default_id = sys.argv[1:5]
extra = sys.argv[5:]
browsers = [('Chrome', 'Google/Chrome', True), ('Chrome Beta', 'Google/Chrome Beta', False),
            ('Chrome Canary', 'Google/Chrome Canary', False), ('Chromium', 'Chromium', False),
            ('Brave', 'BraveSoftware/Brave-Browser', False), ('Edge', 'Microsoft Edge', False),
            ('Arc', 'Arc/User Data', False)]
for name, sub, always in browsers:
    base = os.path.join(support, sub)
    if not always and not os.path.isdir(base):
        continue
    d = os.path.join(base, 'NativeMessagingHosts')
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, 'com.imessage_relay.host.json')
    origins = ['chrome-extension://%s/' % default_id] + ['chrome-extension://%s/' % e for e in extra]
    try:
        for o in json.load(open(out)).get('allowed_origins', []):
            if o not in origins:
                origins.append(o)          # keep extra ids a prior run was given (e.g. an unpacked dev id)
    except Exception:
        pass
    m = json.load(open(tpl))
    m['path'] = host
    m['allowed_origins'] = origins
    with open(out, 'w') as f:
        json.dump(m, f, indent=2)
    print('host:      %-13s %s' % (name, out))
PY

# 4) The watcher app. macOS ignores Full Disk Access granted to Apple's Python (Python.app or
#    python3), so the watcher runs inside a tiny AppleScript applet that the owner drags into
#    Full Disk Access. The applet runs bin/watch_launcher.py, which runs the current watcher, so
#    updates never touch the app. Rebuilding/re-signing it would invalidate the grant: it is
#    built only when missing, or with --rebuild-watcher-app.
# An existing install may run a watcher app from another path (for example one built by hand):
# if the current LaunchAgent starts an applet that exists, keep using exactly that app.
EXISTING_APP="$("$PY" - "$PLIST" "$APP/fda_binary.txt" <<'PY'
import os, plistlib, sys
plist, fda = sys.argv[1], sys.argv[2]
suffix = '/Contents/MacOS/applet'


def app_from_args(args):
    if not args:
        return ''
    p0 = args[0]
    # form 1: the LaunchAgent runs the applet binary directly
    if p0.endswith('.app' + suffix) and os.path.isfile(p0):
        return p0[:-len(suffix)]
    # form 2: a hand-made v1.2 agent launches the app with "/usr/bin/open [-W/-g/-j] -a <app>".
    # Keep that existing app; never rebuild/re-sign it (item 12).
    if os.path.basename(p0) == 'open':
        for i, a in enumerate(args):
            if a == '-a' and i + 1 < len(args) and args[i + 1].endswith('.app') and os.path.isdir(args[i + 1]):
                return args[i + 1]
    return ''


prog = ''
try:
    with open(plist, 'rb') as f:
        prog = app_from_args(plistlib.load(f).get('ProgramArguments') or [])
except Exception:
    prog = ''
if not prog:                       # fall back to the recorded FDA binary path (points at the app)
    try:
        cand = open(fda).read().strip()
        if cand.endswith('.app') and os.path.isdir(cand):
            prog = cand
    except Exception:
        pass
print(prog)
PY
)"
if [[ -n "$EXISTING_APP" ]]; then WAPP="$EXISTING_APP"; fi
WAPP_STATE="kept"
as_str() { local v="${1//\\/\\\\}"; v="${v//\"/\\\"}"; printf '"%s"' "$v"; }   # AppleScript string literal
if [[ -d "$WAPP" && $REBUILD_APP -eq 0 ]]; then
  WAPP_STATE="kept"
elif command -v osacompile >/dev/null 2>&1 && command -v codesign >/dev/null 2>&1; then
  [[ -d "$WAPP" ]] && rm -rf "$WAPP"
  mkdir -p "$WAPP_DIR"
  osacompile -o "$WAPP" \
    -e 'try' \
    -e "do shell script \"exec \" & quoted form of $(as_str "$PY") & \" \" & quoted form of $(as_str "$BIN/watch_launcher.py") & \" >> \" & quoted form of $(as_str "$LOGS/imessage-relay.watch.err") & \" 2>&1\"" \
    -e 'end try'
  "$PY" - "$WAPP/Contents/Info.plist" "$WAPP_ID" <<'PY'
import plistlib, sys
path, bid = sys.argv[1:3]
with open(path, 'rb') as f:
    info = plistlib.load(f)
info.update(CFBundleIdentifier=bid, CFBundleName='iMessage Relay Watcher', LSUIElement=True)
with open(path, 'wb') as f:
    plistlib.dump(info, f)
PY
  codesign --force --deep -s - "$WAPP" >/dev/null 2>&1 || echo "warning: ad-hoc signing of the watcher app failed"
  WAPP_STATE="built"
else
  WAPP_STATE="unavailable"
fi
if [[ -x "$WAPP/Contents/MacOS/applet" ]]; then
  echo "watcher app: $WAPP ($WAPP_STATE)"
  echo "$WAPP" > "$APP/fda_binary.txt"
  PROGRAM_ARGS=("$WAPP/Contents/MacOS/applet")
else
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "error: could not build $WAPP (osacompile/codesign missing?)" >&2; exit 4
  fi
  echo "watcher app: not built (no osacompile: not a Mac); the LaunchAgent runs Python directly"
  echo "(not built)" > "$APP/fda_binary.txt"
  PROGRAM_ARGS=("$PY" "$BIN/watch_launcher.py")
fi

# 5) LaunchAgent for the inbound watcher (starts the app's applet; KeepAlive)
mkdir -p "$AGENTS"
"$PY" - "$SRC/mac-helper/com.imessage-relay.watch.plist.template" "$PLIST" "$LOGS/imessage-relay.watch.err" "${PROGRAM_ARGS[@]}" <<'PY'
import sys
from xml.sax.saxutils import escape
tpl, out, err = sys.argv[1:4]
args = '\n'.join('    <string>%s</string>' % escape(a) for a in sys.argv[4:])
s = open(tpl).read().replace('__PROGRAM_ARGUMENTS__', args).replace('__ERR_LOG__', escape(err))
open(out, 'w').write(s)
PY
echo "agent:     $PLIST"
# Other LaunchAgents that also run the watcher (hand-made during testing) would run a second copy.
"$PY" - "$AGENTS" "$PLIST" <<'PY'
import glob, os, plistlib, sys
agents, ours = sys.argv[1:3]
for p in sorted(glob.glob(os.path.join(agents, '*.plist'))):
    if os.path.abspath(p) == os.path.abspath(ours):
        continue
    try:
        with open(p, 'rb') as f:
            args = ' '.join(plistlib.load(f).get('ProgramArguments') or [])
    except Exception:
        continue
    if 'iMessage Relay Watcher.app' in args or 'imessage-relay' in args or 'fdatest.py' in args:
        print('warning:   %s also starts the relay watcher; unload it to avoid two copies:\n'
              '           launchctl bootout gui/$(id -u) "%s" && rm "%s"' % (p, p, p))
PY
LOG_FILE="${IMESSAGE_RELAY_LOG:-$LOGS/imessage-relay.log}"
LOG_OFFSET=0; [[ -f "$LOG_FILE" ]] && LOG_OFFSET=$(wc -c < "$LOG_FILE" | tr -d ' ')
AGENT_LOADED=0
if [[ $LOAD_AGENT -eq 1 ]] && command -v launchctl >/dev/null 2>&1; then
  with_timeout 15 launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  # Wait for the old service to actually leave the domain (up to ~10s) before bootstrapping the new
  # one; bootstrapping while the previous instance is still shutting down fails with "service already
  # loaded / in progress". Poll `launchctl print`, which errors once the label is gone.
  for _ in $(seq 1 20); do
    with_timeout 5 launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if with_timeout 15 launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || with_timeout 15 launchctl load -w "$PLIST" 2>/dev/null; then
    echo "watcher:   loaded ($LABEL)"; AGENT_LOADED=1
  else
    echo "watcher:   could not load; try: launchctl bootstrap gui/$(id -u) \"$PLIST\""
  fi
else
  echo "watcher:   not loaded (launchctl unavailable or --no-launchagent)"
fi

# 6) Can the watcher read chat.db? A freshly built app never has Full Disk Access yet. Otherwise
#    wait briefly for the restarted watcher to log watch_baseline/chat_db_ok or chat_db_unavailable.
FDA="unknown"
if [[ "$WAPP_STATE" == "built" ]]; then
  FDA="missing (new app)"
elif [[ $AGENT_LOADED -eq 1 ]]; then
  FDA=$("$PY" - "$LOG_FILE" "$LOG_OFFSET" <<'PY'
import json, sys, time
path, off = sys.argv[1], int(sys.argv[2] or 0)
end = time.time() + 12
while time.time() < end:
    try:
        with open(path, 'rb') as f:
            f.seek(off)
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            ev = json.loads(line).get('event')
        except ValueError:
            continue
        if ev in ('watch_baseline', 'chat_db_ok'):
            print('ok'); sys.exit()
        if ev == 'chat_db_unavailable':
            print('missing'); sys.exit()
    time.sleep(1)
print('unknown')
PY
)
fi
echo "chat.db:   watcher access: $FDA"
if [[ "$(uname -s)" == "Darwin" ]] && command -v open >/dev/null 2>&1 && [[ -d "$WAPP" ]]; then
  if [[ "$OPEN_SETTINGS" == "always" || ( "$OPEN_SETTINGS" == "auto" && "$FDA" != "ok" ) ]]; then
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" || true
    open -R "$WAPP" || true
    echo "opened:    Full Disk Access settings, and Finder showing 'iMessage Relay Watcher'"
  fi
fi

# One machine-readable line for the bot (no secrets, no message text).
printf 'summary:   {"version":"%s","previous":"%s","upgrade":%s,"watcherApp":"%s","chatDb":"%s"}\n' \
  "$NEW_VERSION" "$PREV_VERSION" "$([[ -n "$PREV_VERSION" ]] && echo true || echo false)" "$WAPP_STATE" "$FDA"

if [[ $BRIEF -eq 1 ]]; then
  cat <<BRIEF

== Your checklist ==
1. Full Disk Access: drag 'iMessage Relay Watcher' into System Settings > Privacy & Security >
   Full Disk Access and switch it on.$([[ "$FDA" == "ok" ]] && echo "  (already done)")
2. Extension: chrome://extensions > Developer mode on > drag in $EXT_DIR
   (already loaded? click its reload arrow instead).
3. Press Cmd+R on your x.com/i/chat tab.
4. Type /test in the relay chat.
BRIEF
  exit 0
fi

cat <<NEXT

== Next steps ==
1. Load the extension (the one manual step), in Chrome:
   open chrome://extensions and turn on "Developer mode" (top right), then drag the folder
     $EXT_DIR
   (Finder: open -R "$EXT_DIR") onto that page. ("Load unpacked" and choosing the folder works too.)
   The card should show ID $DEFAULT_EXT_ID. Click the puzzle icon and pin "iMessage Relay";
   its popup should say "Mac helper connected: yes".

2. macOS prompt A, Automation (appears on the first self-test or first real send):
   "Google Chrome wants access to control Messages" -> click OK.
   (Self-test with --direct shows it for the app running the command instead.)
   If you clicked Don't Allow: System Settings > Privacy & Security > Automation -> enable Messages.

3. Full Disk Access for the inbound watcher (no automatic prompt; add it by hand):
   drag 'iMessage Relay Watcher' into Full Disk Access.
   It is  $WAPP
   System Settings > Privacy & Security > Full Disk Access; drag the app from Finder into the
   list (or "+" and pick it), and make sure its switch is on. (macOS ignores this permission for
   Apple's Python, so the watcher runs inside this small wrapper app.) The installer opened both
   windows if access was missing; ./install.sh --open-settings opens them again.
   Check: $APP/relayctl status  -> "watcherCanReadChatDb": true

4. Tell the relay which X chat to watch (the sender<->owner chat; the id is in https://x.com/i/chat/<id>):
     $APP/relayctl set relayConversationId <id>
   and add contacts:  $APP/relayctl add-contact --name "Name" --handle "+1 ..." [--label Short]

5. Set the owner identity the extension gate checks (so a wrong X login pauses relaying):
     $APP/relayctl set ownerXUserId <numeric id from get_users_me>
     $APP/relayctl owner-handles add <@owner_handle>   (add every alias the owner posts under)
   Without ownerXUserId the gate falls back to the account-switcher avatar only; status flags this.

6. Relay all texts, or only people you pick?  $APP/relayctl relay-all on   (or off: contacts only)
   relay-all forwards every inbound text, including verification codes, into X Chat.

7. Keep the Messages app running (hidden is fine). Optional: $APP/relayctl auto-reopen on

8. Check everything:  $APP/relayctl status
   Self-test:         $APP/relayctl selftest
Log (counts only, never message text): $LOGS/imessage-relay.log
NEXT
