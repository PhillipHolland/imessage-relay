#!/usr/bin/python3
"""v1.4.18: watcher-side client for the direct poster. Stdlib. Does not import chatxdk.

The 3.12 venv runs relay_xpost.py. This module decides eligibility, plans the silent header, and
turns one JSON result line into posted / fallback / timeout. Message text goes to the child on
stdin only. It is never written to disk here and never placed in argv.
"""
import json
import os
import re
import subprocess
import time

import relay_common as rc
import relay_header as rh

POST_TIMEOUT_S = 8


def sanitize_code(code):
    return re.sub(r'[^a-z0-9_]', '', str(code or '').lower())[:40]


def venv_python():
    override = os.environ.get('IMESSAGE_RELAY_XPOST_PYTHON') or ''
    if override:
        return override
    return os.path.join(rc.APP_DIR, 'xpost-venv', 'bin', 'python')


def script_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay_xpost.py')


def eligible(cfg, m):
    """1:1 or group TEXT with no photo metadata and no reaction. Digests never reach this."""
    if not cfg or not cfg.get('directPost'):
        return False
    if not m or m.get('media') or m.get('reaction'):
        return False
    return bool(m.get('text'))


def plan_state(messages, gap, now=None):
    before = rh.load_state()
    _lines, after = rh.plan(messages, gap, before, now=now)
    return before, after


def save_after(after):
    if after:
        rh.save_state(after)


def _now(now):
    if callable(now):
        return now()
    if now is None:
        return time.time()
    return now


def _parse_out(out):
    if out is None:
        return None
    if isinstance(out, bytes):
        out = out.decode('utf-8', 'replace')
    line = ''
    for ln in str(out).splitlines():
        s = ln.strip()
        if s.startswith('{') and s.endswith('}'):
            line = s
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or 'ok' not in obj:
        return None
    return obj


def _marker(message_id, header_planned, after):
    ids = [str(message_id)]
    if header_planned:
        ids.append(str(message_id) + '#hdr')
    marker = {'type': 'direct', 'ids': ids, 'headerState': rh.public_state(after)}
    return marker


def _decision(action, code, stamp=None, marker=None, header_after=None, save_after_on_webhook=False):
    return {
        'action': action,
        'code': code or '',
        'stamp': stamp,
        'marker': marker,
        'header_after': header_after,
        'save_after_on_webhook': bool(save_after_on_webhook),
    }


def _fallback(code, before, after, skip_header=False, already_saved=False):
    stamp = {'headerState': rh.public_state(before)}
    if skip_header:
        stamp['skipHeader'] = True
        stamp['headerStateAfter'] = rh.public_state(after)
    return _decision('fallback', code, stamp=stamp, header_after=after,
                     save_after_on_webhook=not skip_header and not already_saved)


def _timeout(after):
    rh.save_state(after)
    return _decision('timeout', 'timeout', header_after=after, save_after_on_webhook=False)


def _from_outcome(obj, message_id, before, after, header_planned):
    if obj.get('ok'):
        rh.save_state(after)
        return _decision('posted', 'ok', marker=_marker(message_id, header_planned, after),
                         header_after=after)
    if obj.get('retryable'):
        skip = bool(obj.get('header_sent'))
        if skip:
            rh.save_state(after)
        return _fallback(obj.get('code') or 'failed', before, after, skip_header=skip, already_saved=skip)
    rh.save_state(after)
    return _decision('timeout', obj.get('code') or 'timeout', header_after=after)


def send(cfg, message, runner=None, now=None):
    """Post one public() message. Returns a decision dict and never logs the text.

    posted: header state already saved; `marker` is a body-free webhook.
    fallback: clear failure before a successful message POST (or header already sent). Caller
    webhooks the same id and saves `header_after` only after HTTP 2xx, unless the header was
    already saved (`save_after_on_webhook` false).
    timeout: the POST may have landed. No webhook, no retry. Header state already advanced.
    """
    t = _now(now)
    gap = cfg.get('headerGapMinutes', 30) if cfg else 30
    before = rh.load_state()
    lines, after = rh.plan([message], gap, before, now=t)
    header_planned = any(ln.get('header') for ln in lines)
    message_id = str((message or {}).get('id') or '')
    py = venv_python()
    if not os.path.isfile(py):
        return _fallback('no_venv', before, after)
    texts = [{'text': ln.get('text') or '', 'header': bool(ln.get('header')), 'notify': not bool(ln.get('header'))}
             for ln in lines]
    argv = [py, script_path(), 'post']
    data = json.dumps({'texts': texts}).encode('utf-8')
    run = runner or subprocess.run
    try:
        proc = run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=POST_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        parsed = _parse_out(getattr(e, 'stdout', None) or getattr(e, 'output', None))
        if parsed is None:
            return _timeout(after)
        return _from_outcome(parsed, message_id, before, after, header_planned)
    except OSError:
        return _fallback('spawn_failed', before, after)
    parsed = _parse_out(getattr(proc, 'stdout', '') or '')
    if parsed is None:
        return _timeout(after)
    return _from_outcome(parsed, message_id, before, after, header_planned)
