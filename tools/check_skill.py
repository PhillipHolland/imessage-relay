#!/usr/bin/env python3
"""Regression check for SKILL.md / BOT_PROFILE.md (v1.3.8 guided setup). Exit 0 = ok.

The onboarding must stay a numbered, guided flow with at most three setup questions, secrets via
secret requests only, webhook FIELD links only (never the Routines tab link), one Mac command, the
final checklist, an error path per step, the standing defaults, and /test documented."""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def main():
    skill = open(os.path.join(ROOT, 'SKILL.md'), encoding='utf-8').read()
    prof = open(os.path.join(ROOT, 'BOT_PROFILE.md'), encoding='utf-8').read()
    m = re.search(r'^## Onboarding: one-minute guided setup\n(.*?)^## Daily operation', skill, re.S | re.M)
    errs = []
    if not m:
        print('FAIL: onboarding section missing')
        return 1
    ob = m.group(1)
    steps = re.findall(r'^### Step (\d+)\.', ob, re.M)
    if steps != [str(i) for i in range(0, 11)]:
        errs.append('steps must be numbered 0..10, got %r' % steps)
    qs = re.findall(r'^### Step \d+\. Q(\d):', ob, re.M)
    if qs != ['1', '2', '3']:
        errs.append('exactly three setup questions Q1..Q3, got %r' % qs)
    for i in range(0, 10):
        body = re.search(r'^### Step %d\..*?(?=^### )' % i, ob, re.S | re.M).group(0)
        if i in (0, 1, 2, 4, 5, 8, 9) and 'rror' not in body:
            errs.append('Step %d has no error/retry path' % i)
    for need in ('secret request', 'grokbot://app/v1/sidebar?target=webhook-url&automation=<folder>',
                 'grokbot://app/v1/sidebar?target=webhook-key&automation=<folder>', 'Full Disk Access',
                 'Cmd+R', 'x.com/i/chat', '/test', 'relayctl setup', '--webhook-key-stdin',
                 '--webhook-key-prompt', 'curl -fsSL', 'shasum -a 256 -c -', 'ListMachines', 'get_users_by_username',
                 'Step U', 'dry run **on**', 'quiet hours **off**', 'opt-in', 'Never rebuild or re-sign',
                 'gpnjllicnleeogdmgpjekgflakdiifee'):
        if need not in ob:
            errs.append('onboarding is missing %r' % need)
    pins = set(re.findall(r'releases/download/(v[\d.]+)/build_bundle\.py', skill))
    shas = set(re.findall(r'\b[0-9a-f]{64}\b', skill))
    if len(pins) != 1 or len(shas) != 1:
        errs.append('expected exactly one pinned release URL and one sha256, got %r / %d' % (pins, len(shas)))
    for bad in ('attached to this skill', 'CopyFromBox build_bundle', 'attach build_bundle'):
        if bad in skill:
            errs.append('build_bundle.py must be downloaded from the pinned release, not %r' % bad)
    if re.search(r'grokbot://[^\s)`]*routines', skill, re.I):
        errs.append('a Routines-tab link appears; use webhook field links only')
    for bad in ('--token ', 'CHAT_PIN=<', 'paste your PIN', 'paste the key here'):
        if bad in skill:
            errs.append('secret on argv / in chat: %r' % bad)
    if '/test' not in prof or 'three questions' not in prof:
        errs.append('BOT_PROFILE.md must describe the guided setup and /test')
    d = re.search(r'## Description \(≤300 characters\)\n(.+)\n', prof)
    if not d or len(d.group(1)) > 300:
        errs.append('BOT_PROFILE description missing or over 300 characters')
    for e in errs:
        print('FAIL:', e)
    print('check_skill: %s (%d steps, %d questions)' % ('ok' if not errs else 'FAILED', len(steps), len(qs)))
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
