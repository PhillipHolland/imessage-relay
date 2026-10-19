#!/usr/bin/env bash
# W2 regression: the routine's payload heredoc must write the relayed body VERBATIM and execute nothing.
# A literal, single-quoted RELAY_EOF_<hex> delimiter turns off all shell expansion inside the body, so
# a payload containing $EOF, backticks, $(...), and a line "RELAY_EOF_other" lands on disk unchanged.
set -u
fail() { echo "FAIL: $1"; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
canary="$tmp/canary"            # any command substitution that RAN would create this; it must not exist
out="$tmp/evt.json"

# Step 1: a random hex, obtained in its own step (here inline for the test).
hex="$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
delim="RELAY_EOF_${hex}"

# The adversarial payload the routine would have received as a relayed message. Note the embedded
# $EOF, backticks, $(...) touching the canary file, and a decoy "RELAY_EOF_other" delimiter line.
payload='{"type":"message","text":"hi $EOF `touch '"$canary"'` $(touch '"$canary"') RELAY_EOF_other end"}'

# Guard: the random delimiter must not occur in the payload (it is random; it never will).
case "$payload" in
  *"$delim"*) fail "random delimiter collided with payload (astronomically unlikely)";;
esac

# Step 2: literal single-quoted delimiter; payload on one line exactly as received.
eval "cat > \"$out\" <<'$delim'
$payload
$delim"

[ -e "$canary" ] && fail "command substitution in the payload EXECUTED (canary created)"

# The file must contain exactly the payload line, byte for byte.
got="$(cat "$out")"
[ "$got" = "$payload" ] || fail "payload not written verbatim:
  want: $payload
  got:  $got"

# And the dangerous fragments must survive as literal text.
grep -qF '$EOF' "$out"              || fail '$EOF not preserved literally'
grep -qF '`touch' "$out"           || fail 'backticks not preserved literally'
grep -qF '$(touch' "$out"          || fail '$(...) not preserved literally'
grep -qF 'RELAY_EOF_other' "$out"  || fail 'decoy delimiter line not preserved'

echo "ok: payload written verbatim, nothing executed"
