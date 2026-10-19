#!/bin/bash
# Runs every test. Needs python3 (3.9+); the JS tests also need node and jsdom
# (NODE_PATH pointing at a node_modules with jsdom, or `npm i jsdom` here).
set -uo pipefail
cd "$(dirname "$0")"
fail=0
echo "== Python (mac-helper) =="
python3 -m unittest discover -s tests/py -t tests/py 2>&1 | tail -4 || fail=1
python3 -m unittest discover -s tests/py -t tests/py >/dev/null 2>&1 || fail=1
echo "== Shell (routine heredoc, W2) =="
for t in tests/sh/*.sh; do
  [[ -e "$t" ]] || continue
  bash "$t" || fail=1
done
# Try to find jsdom automatically (global node_modules) if it isn't already on NODE_PATH.
if command -v node >/dev/null 2>&1 && ! node -e "require('jsdom')" >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  _gr="$(npm root -g 2>/dev/null)"; [[ -n "$_gr" ]] && export NODE_PATH="${NODE_PATH:+$NODE_PATH:}$_gr"
fi
if command -v node >/dev/null 2>&1 && node -e "require('jsdom')" >/dev/null 2>&1; then
  for t in detect frames owner native guess yes reaction quote probe xreply trash e2etest chips smoke-chrome; do
    echo "== JS: $t =="
    node "tests/js/$t.test.js" 2>&1 | grep -E 'passed|FAIL|PASS chrome' ; [[ ${PIPESTATUS[0]} -eq 0 ]] || fail=1
  done
else
  echo "== JS tests skipped: node or jsdom not found. Install jsdom (npm i jsdom) or set"
  echo "   NODE_PATH to a node_modules that has jsdom, then re-run. =="
fi
[[ $fail -eq 0 ]] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit $fail
