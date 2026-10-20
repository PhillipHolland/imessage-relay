#!/bin/bash
# iMessage Relay uninstaller. Idempotent. Usage: ./uninstall.sh [--keep-config] [--keep-logs]
# Removes the LaunchAgent, the watcher app (~/Applications/iMessage Relay Watcher.app), the native
# host manifests (all browsers), ~/.imessage-relay (config, roster), the extracted source and
# bundle files, and the log. It also resets the watcher's Full Disk Access grant with tccutil.
#   --keep-config  keeps config.json (this also keeps the stored webhook key).
#   --keep-logs    keeps ~/Library/Logs/imessage-relay.log (counts only; never message bodies).
set -euo pipefail
APP="${IMESSAGE_RELAY_HOME:-$HOME/.imessage-relay}"
LOG="${IMESSAGE_RELAY_LOG:-$HOME/Library/Logs/imessage-relay.log}"
LABEL="com.imessage-relay.watch"
WATCHER_BUNDLE_ID="com.imessage-relay.watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SUPPORT="$HOME/Library/Application Support"
KEEP_CONFIG=0; KEEP_LOGS=0
for a in "$@"; do
  case "$a" in
    --keep-config) KEEP_CONFIG=1 ;;
    --keep-logs) KEEP_LOGS=1 ;;
    *) echo "unknown option: $a (use --keep-config and/or --keep-logs)" >&2; exit 2 ;;
  esac
done
with_timeout() { local s="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$s" "$@"; }

if command -v launchctl >/dev/null 2>&1; then
  with_timeout 15 launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || with_timeout 15 launchctl unload "$PLIST" >/dev/null 2>&1 || true
fi
# The watcher app the LaunchAgent starts (an upgrade may have kept one at a custom path).
AGENT_APP=""
if [[ -f "$PLIST" ]]; then
  AGENT_APP="$(/usr/bin/python3 - "$PLIST" <<'PY' 2>/dev/null || true
import plistlib, sys
prog = (plistlib.load(open(sys.argv[1], 'rb')).get('ProgramArguments') or [''])[0]
s = '/Contents/MacOS/applet'
print(prog[:-len(s)] if prog.endswith('.app' + s) else '')
PY
)"
fi
rm -f "$PLIST"
for WAPP in "$HOME/Applications/iMessage Relay Watcher.app" "$AGENT_APP"; do
  [[ -n "$WAPP" && -d "$WAPP" ]] || continue
  if grep -qa "$WATCHER_BUNDLE_ID" "$WAPP/Contents/Info.plist" 2>/dev/null; then
    rm -rf "$WAPP"; echo "Removed $WAPP"
  else
    echo "Left $WAPP in place (not built by iMessage Relay: bundle id differs; remove it by hand if you made it)"
  fi
done
# Native host manifests, then prune a NativeMessagingHosts dir we leave empty.
for sub in "Google/Chrome" "Google/Chrome Beta" "Google/Chrome Canary" "Chromium" "BraveSoftware/Brave-Browser" "Microsoft Edge" "Arc/User Data"; do
  nmh="$SUPPORT/$sub/NativeMessagingHosts"
  rm -f "$nmh/com.imessage_relay.host.json"
  [[ -d "$nmh" ]] && rmdir "$nmh" 2>/dev/null || true
done
# Reset the watcher's Full Disk Access grant so a fresh install re-prompts cleanly.
if command -v tccutil >/dev/null 2>&1; then
  tccutil reset SystemPolicyAllFiles "$WATCHER_BUNDLE_ID" >/dev/null 2>&1 || true
fi
# v1.4.18: direct-post secrets live in the login keychain, not in config.json. Delete them even
# with --keep-config. Tests set IMESSAGE_RELAY_ALLOW_NON_MAC=1 and must not touch a real keychain.
if [[ "${IMESSAGE_RELAY_ALLOW_NON_MAC:-}" != "1" ]]; then
  security delete-generic-password -a refresh-token -s com.imessage-relay.xpost >/dev/null 2>&1 || true
  security delete-generic-password -a client-secret -s com.imessage-relay.xpost >/dev/null 2>&1 || true
  security delete-generic-password -a private-key -s com.imessage-relay.xpost >/dev/null 2>&1 || true
fi
if [[ $KEEP_CONFIG -eq 1 && -f "$APP/config.json" ]]; then
  find "$APP" -mindepth 1 -maxdepth 1 ! -name config.json -exec rm -rf {} +
  echo "Removed iMessage Relay; kept $APP/config.json (and its webhook key)"
else
  rm -rf "$APP"
  echo "Removed iMessage Relay and $APP"
fi
# Extracted sources and the single-file bundle, if they were left in HOME.
rm -rf "$HOME/imessage-relay-src"
rm -f "$HOME/imessage-relay-build_bundle.py"
if [[ $KEEP_LOGS -eq 1 ]]; then
  echo "Kept log at $LOG (counts only)."
else
  rm -f "$LOG" "$HOME/Library/Logs/imessage-relay.watch.err"
  echo "Removed log $LOG (and the watcher stderr log)"
fi
# The bot-side routine's dedupe/seen-ids directory (ids only, never bodies), if it ran on this Mac.
rm -rf "$HOME/.imessage-relay-routine"

cat <<'DONE'
Also, by hand if you like:
  - chrome://extensions -> iMessage Relay -> Remove
  - System Settings > Privacy & Security > Full Disk Access -> remove "iMessage Relay Watcher"
    (and any Python entry added for an older version)
  - System Settings > Privacy & Security > Automation -> turn off Messages for Google Chrome
DONE
