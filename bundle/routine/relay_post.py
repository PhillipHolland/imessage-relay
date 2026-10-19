#!/usr/bin/env python3
"""iMessage Relay v1.4.7 — routine helper (runs on the BOT's computer, not the Mac).

The webhook routine writes one Mac webhook payload to a unique request file and runs this script
with `--in <file>` (the file is deleted right after it is read). The routine does nothing else:
it never reads or echoes message text, never runs Mac commands, and only reports a one-line,
body-free failure if this helper exits non-zero.

What it does, all in one process:
  1. Dedupe on the payload `id` (ids only, TTL; message bodies never touch disk). ids are committed
     under a file lock (mkstemp + flock) so concurrent runs never lose each other's ids.
  2. Format the line for the relay chat per event type (message, digest, sent, dry_run, send_failed,
     needs_target, blocked, unblocked, status, catching_up, resynced, wrong_account, refused, guess_expired,
     and v1.3.8's relay_test: the /test report, one X message, one short line per step).
  3. Encrypt each line for the relay conversation with xchat_lite's session-encrypt, which unwraps the
     conversation key from conversation_key_events, passing the text to xchat_lite on STDIN — never on
     argv, never to disk. v1.4.5: the key events and the public signing keys (key material only, never
     message events or bodies) are cached in ~/.imessage-relay-routine/xchat_keys.json (mode 600, 6 h), so
     the routine fetches nothing from X on most runs. With no usable cache the helper answers
     {"action":"fetch","need":[...]} and the routine fetches those and runs it again. A `batch` event
     (several texts from one watcher tick) becomes one X message per text in a single run.
  4. Print a JSON plan on stdout: the pre-encrypted send(s) the routine then hands to the X plugin's
     `send_chat_message` (plus `add_conversation_keys` first, for an empty thread).

xchat_lite is NOT vendored here: it comes from the x-api-mcp-guide skill (clone of
xdevplatform/xchat-grokbot-helper). This helper locates that install (or uses the paths the routine
passes in `helper`) and shells out to it. See SKILL.md "Webhook routine" for the dependency and the
exact routine prompt. If xchat_lite is missing, this helper formats + dedupes and returns the plan
with `encrypted:false` so the routine can report the setup problem — it never falls back to putting
text on a command line.

STDIN (one JSON object):
  {
    "payload": { ... one Mac webhook event ... },         # required
    "conversation_id": "AAA-BBB",                          # the relay conversation (defaults to payload.relayConversationId)
    "xchat": {                                             # context the routine fetched via the X plugin (optional; needed to actually encrypt)
      "events": [...], "conversation_key_events": [...], "signing_keys": [...],
      "prepare": null | { ...prepare-keys stdin... }       # set only for an empty / first-message thread
    },
    "helper": {                                            # optional explicit xchat_lite paths (else auto-located)
      "python": "/…/.venv/bin/python", "script": "/…/xchat_lite.py",
      "user_id": "…", "key_version": "…", "juicebox": "/…/juicebox.json",
      "xdg_config_home": "~/xchat/sender",                 # exported so xchat_lite finds the saved key
      "env": {"XDG_CONFIG_HOME": "~/xchat/sender"}          # extra env merged over os.environ
    },
    "dedupe_path": "~/.imessage-relay-routine/seen_ids.json",  # optional override
    "ttl_seconds": 86400,
    "defer_commit": false    # true = don't remember ids now; call `relay_post.py --ack` after send instead
  }
Read the request from a file with `--in <path>` (keeps untrusted text off the command line; the
file is deleted right after it is read). To keep the message BODY out of even the request file, write
the raw Mac webhook event (the `<webhook_event>` object, body included) to its own file and pass it with
`--payload-file <path>`: it becomes the request's `payload` and is deleted right after it is read, so the
`--in` request can be assembled with NO body in it at all (item B). A `--payload-file` must parse as
exactly one JSON object; a bare value, a list, truncation, or two concatenated objects (e.g. from a
heredoc delimiter that appeared inside a relayed message) is refused with a body-free error and nothing
is forwarded. Confirm a strict post-send dedupe
with `--ack ID1 ID2 ... [--dedupe-path P]` (ids on argv), or `--ack` reading {"dedupe_path","ids":[...]}
on stdin.

STDOUT (one JSON object):
  {"action":"send","sends":[{"conversation_id","message_id","encoded_message_create_event",
                             "encoded_message_event_signature","add_conversation_keys":null|{...},
                             "should_notify":bool,"reply_to":null|"<x event id>"}],
   "encrypted":true, "posted_kind":"message"}
  Pass each send's should_notify and reply_to straight to send_chat_message: a relayed text notifies
  the owner; every status/confirmation line is silent (should_notify:false) and threads under the
  owner's reply when reply_to is set (item 1).
  {"action":"skip","reason":"duplicate id …"}
  v1.4.7 photos (1:1 inbound): a message/batch line may carry `media` (token, ext, size, sha256, url, key).
  The Mac encrypted each photo with a fresh AES-256-GCM key and put only the ciphertext on the drop
  (served once, gone after 10 minutes). Run 1 fetches it from the drop configured in drop.json beside
  the dedupe file ({"url","get_key"}, mode 600; never from any other host), checks size + sha256,
  decrypts it into <media_dir>/<token>.<ext> (mode 600) and answers
  {"action":"upload","uploads":[{"token","upload_conversation_id","total_bytes","num_parts",
                                 "parts":[{"segment_index","media":"$file:/…/part"}]}]}
  -> chat_media_upload_initialize + _append per part, then run again (same payload) with
  "media_keys": {token: media_hash_key}. Each send then carries "media_finalize":[{token, media_hash_key,
  num_parts}] -> chat_media_upload_finalize (with that send's message_id) before send_chat_message.
  "media_off": true (an upload failed) posts the text with a clear "couldn't be relayed" notice; so does
  a photo the drop no longer has. Photos are only fetched and uploaded when the request has
  "media_upload": true (a routine prompt with the photo steps); without it the text posts with the notice.
  The routine never touches the Mac.
  Photos are in flight only: the copy, the parts and a tiny state file (mode 600) are deleted when the
  send is built, on any error, or by the sweep after 15 min. Nothing about them is logged.
  {"action":"fetch","need":["conversation_key_events","signing_keys"],"reason":"…"}   # v1.4.5: fetch, write again, rerun
  {"action":"error","reason":"…"}                          # body-free; the routine posts one failure line
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid as _uuid

GROUP_SEP = ' \u00b7 '   # "Family · Sam: …"


# ---------- formatting (pure; unit-tested) ----------
def _label(p, default='someone'):
    return str(p.get('label') or p.get('contactName') or default).strip() or default


UNKNOWN_TAG = '\u26a0\ufe0f Unknown \u00b7 '
TAGS = {'spam': '\U0001f6ab Spam \u00b7 ', 'promotion': '\U0001f3f7\ufe0f Promo \u00b7 ',   # v1.4.6 follow-messages
        'transaction': '\U0001f9fe '}
BATCH_MAX = 20
DIGEST_MAX_CHARS = 9000
KEY_CACHE_PATH = '~/.imessage-relay-routine/xchat_keys.json'
KEY_CACHE_TTL_S = 6 * 3600          # after 6 h the routine fetches keys again (a rotated key heals by then)
KEY_CACHE_MAX_BYTES = 512 * 1024
NEED_KEYS = ['conversation_key_events', 'signing_keys']


def _message_line(m):
    """`<label>: <text>` or `<group.label> · <label>: <text>`. Carries the (untrusted) body. A tapback
    line (item 5) joins with a space, not a colon: "Alex \u2764\ufe0f your message"."""
    label = _label(m)
    text = str(m.get('text') or '')
    g = m.get('group') or {}
    gl = str(g.get('label') or g.get('name') or '').strip()
    head = (gl + GROUP_SEP if gl else '') + label
    tag = TAGS.get(str(m.get('tag') or '')) if not gl else None
    if tag:
        head = tag + head               # v1.4.6: Messages filed it under Spam / Promotions / Transactions
    elif m.get('unknown') and not gl:
        head = UNKNOWN_TAG + head       # v1.4.5 spamMode tag: not in Contacts / flagged by Messages
    if m.get('quiet') and not gl and not m.get('reaction') and not tag and not m.get('unknown'):
        return text                     # v1.4.6 quiet names: same sender as the line just above
    sep = ' ' if m.get('reaction') else ': '
    return head + sep + text


# ---------- v1.4.6 photos (in-flight only) ----------
MEDIA_DIR = '~/.imessage-relay-routine/media'
MEDIA_TTL_S = 15 * 60               # a photo the routine never finished with is swept on the next run
MEDIA_MAX_BYTES = 5 * 1024 * 1024   # the X Chat image limit (xchat_lite encrypt-media)
MEDIA_EXTS = ('jpg', 'jpeg', 'png', 'gif', 'webp')
MEDIA_MAX_PER_LINE = 10
_HEX32 = re.compile(r'^[0-9a-f]{32}$')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_PHOTO_PLACEHOLDER = re.compile(r'\U0001f4f7 (?:Photo|\d+ photos)$')


_B64U_KEY = re.compile(r'^[A-Za-z0-9_-]{43}$')
DROP_CONFIG = '~/.imessage-relay-routine/drop.json'
DROP_TIMEOUT_S = 30
DROP_AAD = b'imr-drop-v1:'
DROP_OVERHEAD = 12 + 16


def media_meta(items):
    """The valid photo entries of a webhook message (metadata only: token, ext, size, sha256 and, v1.4.7,
    the drop url + one-time key)."""
    out = []
    for x in (items if isinstance(items, list) else [])[:MEDIA_MAX_PER_LINE]:
        if not isinstance(x, dict):
            continue
        tok, ext, sha = str(x.get('token') or ''), str(x.get('ext') or '').lower(), str(x.get('sha256') or '').lower()
        size = x.get('size')
        if (_HEX32.match(tok) and ext in MEDIA_EXTS and _HEX64.match(sha) and isinstance(size, int)
                and not isinstance(size, bool) and 0 < size <= MEDIA_MAX_BYTES):
            it = {'token': tok, 'ext': ext, 'size': size, 'sha256': sha}
            url, key = str(x.get('url') or ''), str(x.get('key') or '')
            if url.startswith('https://') and len(url) <= 300 and _B64U_KEY.match(key):
                it.update(url=url, key=key)
            out.append(it)
    return out


def _with_media(line, m):
    media = media_meta((m or {}).get('media'))
    if media:
        line['media'] = media
    return line


def media_box_path(item, media_dir=None):
    return os.path.join(os.path.expanduser(media_dir or MEDIA_DIR), '%s.%s' % (item['token'], item['ext']))


def _media_state_path(token, media_dir=None):
    return os.path.join(os.path.expanduser(media_dir or MEDIA_DIR), '%s.json' % token)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _rmtree_upload(path, media_dir=None):
    """Delete an encrypt-media upload_dir (only its own part files; never anything else)."""
    if not path or not os.path.isdir(path):
        return
    for f in os.listdir(path):
        _rm(os.path.join(path, f))
    try:
        os.rmdir(path)
    except OSError:
        pass


def media_cleanup(tokens_items, media_dir=None):
    """Delete every in-flight file of these photos: the plaintext copy, the upload parts, the state file."""
    for it in tokens_items:
        st = _read_media_state(it['token'], media_dir)
        _rmtree_upload(st.get('upload_dir'), media_dir)
        _rm(media_box_path(it, media_dir))
        _rm(_media_state_path(it['token'], media_dir))


def _read_media_state(token, media_dir=None):
    try:
        with open(_media_state_path(token, media_dir)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def media_sweep(media_dir=None, now=None, ttl=MEDIA_TTL_S):
    """Remove photos and upload parts left behind by an unfinished run (older than ttl)."""
    d = os.path.expanduser(media_dir or MEDIA_DIR)
    now = time.time() if now is None else now
    if not os.path.isdir(d):
        return 0
    n = 0
    for f in os.listdir(d):
        p = os.path.join(d, f)
        try:
            if now - os.lstat(p).st_mtime <= ttl:
                continue
            if os.path.isdir(p):
                _rmtree_upload(p)
            else:
                os.remove(p)
            n += 1
        except OSError:
            pass
    return n


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def media_verify(item, media_dir=None):
    """True when the routine copied the photo intact (size + sha256 match the Mac's metadata)."""
    p = media_box_path(item, media_dir)
    try:
        if os.path.getsize(p) != item['size']:
            return False
        if os.stat(p).st_mode & 0o077:
            os.chmod(p, 0o600)                 # in-flight copy: owner-only
        return _sha256_file(p) == item['sha256']
    except OSError:
        return False


# ---------- v1.4.7 the encrypted drop ----------
def load_drop(req, dedupe_path):
    """{'url','get_key'} from req['drop'] or drop.json beside the dedupe file (mode 600), else None."""
    d = req.get('drop') if isinstance(req.get('drop'), dict) else None
    if d is None:
        p = req.get('drop_config') or os.path.join(os.path.dirname(os.path.expanduser(dedupe_path)) or '.', 'drop.json')
        try:
            with open(os.path.expanduser(p)) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
    url, key = str((d or {}).get('url') or '').rstrip('/'), str((d or {}).get('get_key') or '')
    if not url.startswith('https://') or len(key) < 16:
        return None
    return {'url': url, 'get_key': key, 'crypto_python': str((d or {}).get('crypto_python') or '')}


def _b64u_decode(s):
    import base64
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def drop_get(url, key, max_bytes, timeout=DROP_TIMEOUT_S):
    """GET one ciphertext (served once). bytes or None. Never logs anything."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key, 'User-Agent': 'imessage-relay-post/1.4.7'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = r.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if len(data) <= max_bytes else None


_DECRYPT_CHILD = (
    "import json,sys,os\n"
    "from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
    "a=json.load(sys.stdin)\n"
    "w=open(a['in'],'rb').read()\n"
    "import base64\n"
    "k=base64.urlsafe_b64decode(a['key']+'='*(-len(a['key'])%4))\n"
    "p=AESGCM(k).decrypt(w[:12],w[12:],a['aad'].encode())\n"
    "fd=os.open(a['out'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\n"
    "os.write(fd,p);os.close(fd)\n")


def _crypto_pythons(drop):
    # by exact path: a venv's python is a symlink to the system one but has other site-packages
    seen = [sys.executable]
    for c in (drop.get('crypto_python'), '/usr/bin/python3', '/usr/local/bin/python3', '/opt/homebrew/bin/python3'):
        if c and os.path.exists(c) and c not in seen:
            seen.append(c)
            yield c


def drop_decrypt(wire, key_b64u, token, out_path, drop, runner=subprocess.run):
    """Decrypt nonce||ct||tag into out_path (mode 600). In-process with `cryptography` when this python
    has it, else via a python that does (key on stdin, never argv). True/False."""
    aad = DROP_AAD + token.encode('ascii')
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        AESGCM = None
    if AESGCM is not None:
        try:
            plain = AESGCM(_b64u_decode(key_b64u)).decrypt(wire[:12], wire[12:], aad)
        except Exception:
            return False
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(plain)
        return True
    wire_path = out_path + '.wire'
    try:
        fd = os.open(wire_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(wire)
        for py in _crypto_pythons(drop):
            try:
                r = runner([py, '-c', _DECRYPT_CHILD], input=json.dumps(
                    {'in': wire_path, 'out': out_path, 'key': key_b64u, 'aad': aad.decode('ascii')}).encode(),
                    capture_output=True, timeout=60)
            except Exception:
                continue
            if getattr(r, 'returncode', 1) == 0 and os.path.exists(out_path):
                return True
            _rm(out_path)
        return False
    finally:
        _rm(wire_path)


def media_fetch(item, drop, media_dir=None, getter=drop_get, runner=subprocess.run):
    """v1.4.7: bring one photo from the drop: GET once (only from OUR drop's url), check the size, decrypt,
    check the sha256. Leaves <media_dir>/<token>.<ext> (mode 600) or nothing. True/False."""
    if not drop or not item.get('url') or not item.get('key'):
        return False
    if item['url'] != drop['url'] + '/d/' + item['token']:
        return False                                   # never fetch from anywhere else (and never send our key there)
    d = os.path.expanduser(media_dir or MEDIA_DIR)
    os.makedirs(d, mode=0o700, exist_ok=True)
    out = media_box_path(item, media_dir)
    if os.path.exists(out):
        return True
    wire = getter(item['url'], drop['get_key'], item['size'] + DROP_OVERHEAD)
    if not wire or len(wire) != item['size'] + DROP_OVERHEAD:
        return False
    try:
        if not drop_decrypt(wire, item['key'], item['token'], out, drop, runner):
            _rm(out)
            return False
    except OSError:
        _rm(out)
        return False
    finally:
        wire = None
    if not media_verify(item, media_dir):
        _rm(out)
        return False
    return True


def media_failed_text(text, n):
    """The line when its photo(s) could not be relayed: a clear notice instead of a silent drop."""
    note = '\U0001f4f7 %s couldn\u2019t be relayed \u2014 open Messages to see %s' % (
        ('Photo', 'it') if n == 1 else ('%d photos' % n, 'them'))
    if _PHOTO_PLACEHOLDER.search(text):
        return _PHOTO_PLACEHOLDER.sub(lambda _m: note, text)
    return text + '\n\u2002' + note


def locate_media_helper(helper):
    """(python, script) for an xchat_lite with encrypt-media (the newer helper; v1.4.6). helper.media_python /
    helper.media_script win; else ~/xchat/helper; else the regular helper if it has encrypt-media."""
    h = helper or {}
    if h.get('media_python') and h.get('media_script'):
        return os.path.expanduser(str(h['media_python'])), os.path.expanduser(str(h['media_script']))
    for root in (os.path.expanduser('~/xchat/helper'), os.path.expanduser('~/xchat-grokbot-helper')):
        sp = os.path.join(root, 'xchat_lite.py')
        v = os.path.join(root, '.venv', 'bin', 'python')
        if os.path.exists(sp) and _has_encrypt_media(sp):
            return (v if os.path.exists(v) else sys.executable), sp
    py, sp = locate_helper(helper)
    if sp and _has_encrypt_media(sp):
        return py, sp
    return None, None


def _has_encrypt_media(script):
    try:
        with open(script, encoding='utf-8', errors='replace') as fh:
            return 'encrypt-media' in fh.read()
    except OSError:
        return False


def _helper_argv(py, script, helper):
    base = [py, script]
    h = helper or {}
    for flag, key in (('--user-id', 'user_id'), ('--key-version', 'key_version'), ('--juicebox', 'juicebox')):
        if h.get(key):
            val = os.path.expanduser(str(h[key])) if key == 'juicebox' else str(h[key])
            base += [flag, val]
    return base


def media_prepare(conv, items, xchat, helper, runner, media_dir=None):
    """Encrypt each verified photo into upload parts (encrypt-media). Returns (uploads, failed_items).
    The plaintext stays only until the send run; the part files are mode 600 in a mode-700 upload_dir.
    Nothing is logged; on any error every file of a failed photo is deleted."""
    os.makedirs(os.path.expanduser(media_dir or MEDIA_DIR), mode=0o700, exist_ok=True)
    good, failed = [], []
    for it in items:
        (good if media_verify(it, media_dir) else failed).append(it)
    media_cleanup(failed, media_dir)
    if not good:
        return [], failed
    py, script = locate_media_helper(helper)
    if not py:
        media_cleanup(good, media_dir)
        return [], failed + good
    stdin_obj = {'conversation_id': conv, 'events': (xchat or {}).get('events') or [],
                 'conversation_key_events': (xchat or {}).get('conversation_key_events') or [],
                 'signing_keys': (xchat or {}).get('signing_keys') or [],
                 'media': [{'input': media_box_path(it, media_dir)} for it in good]}
    try:
        res = _run(_helper_argv(py, script, helper) + ['encrypt-media', '--out-dir', os.path.expanduser(media_dir or MEDIA_DIR)],
                   stdin_obj, runner)
    except Exception:
        media_cleanup(good, media_dir)
        return [], failed + good
    out = res.get('media') or []
    errors = res.get('errors') or {}
    upload_dir = str(res.get('upload_dir') or '')
    uploads, ok_items = [], []
    for i, it in enumerate(good):
        m = out[i] if i < len(out) and isinstance(out[i], dict) else None
        if m is None or str(i) in errors or i in errors or not m.get('parts'):
            failed.append(it)
            continue
        parts = [{'segment_index': p.get('segment_index'), 'media': '$file:' + str(p.get('path'))} for p in m['parts']]
        state = {'upload_dir': upload_dir, 'conversation_key_version': m.get('conversation_key_version'),
                 'num_parts': len(parts), 'total_bytes': m.get('total_bytes')}
        sp = _media_state_path(it['token'], media_dir)
        fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            json.dump(state, fh)
        ok_items.append(it)
        uploads.append({'token': it['token'], 'upload_conversation_id': res.get('upload_conversation_id'),
                        'total_bytes': m.get('total_bytes'), 'num_parts': str(len(parts)), 'parts': parts})
    for it in failed:
        _rm(media_box_path(it, media_dir))
    if not uploads:
        _rmtree_upload(upload_dir)
    return uploads, failed


def format_event(payload):
    """Return {"kind", "lines":[{"id","text","body":bool}]}.

    `body` is True when the line contains relayed (untrusted) message text; every line is still
    encrypted the same way. Raises ValueError on an unknown type.
    """
    t = str(payload.get('type') or '').strip()
    base_id = str(payload.get('id') or '')
    if t == 'message':
        return {'kind': 'message', 'lines': [_with_media({'id': base_id, 'text': _message_line(payload), 'body': True}, payload)]}
    if t == 'batch':
        # v1.4.5 speed: several texts the watcher found in one tick, one webhook / one routine run.
        # Still one X message per text (each pings), each deduped on its own id.
        msgs = (payload.get('messages') or [])[:BATCH_MAX]
        if not msgs:
            raise ValueError('empty batch')
        return {'kind': 'batch', 'lines': [_with_media({'id': str(m.get('id') or (base_id + '#' + str(i))), 'text': _message_line(m),
                                                        'body': True}, m) for i, m in enumerate(msgs)]}
    if t == 'digest':
        # item 7: a header line, then ONE X message per person (their texts grouped under one label).
        msgs = payload.get('messages') or []
        groups, index = [], {}
        for i, m in enumerate(msgs):
            mid = str(m.get('id') or (base_id + '#' + str(i)))
            label = _label(m)
            g = m.get('group') or {}
            gl = str(g.get('label') or g.get('name') or '').strip()
            head = (gl + GROUP_SEP if gl else '') + label
            if m.get('unknown') and not gl:
                head = UNKNOWN_TAG + head
            if head not in index:
                index[head] = len(groups)
                groups.append([head, [], []])
            gi = index[head]
            sep = ' ' if m.get('reaction') else ''
            groups[gi][1].append(sep + str(m.get('text') or '') if sep else str(m.get('text') or ''))
            groups[gi][2].append(mid)
        n, people = len(msgs), len(groups)
        header = '\U0001f319 Overnight: %d message%s%s' % (
            n, '' if n == 1 else 's', (' from %d people' % people) if people > 1 else '')
        # v1.4.6: ONE tidy post (header, then each person once with their texts under them); the line
        # ids still carry every message id so a retried webhook dedupes.
        blocks = [head + ':\n' + '\n'.join('\u2002\u2022 ' + x.replace('\n\u2002', '\n\u2002\u2002 ') for x in texts)
                  for head, texts, _ in groups]
        text = header + '\n\n' + '\n\n'.join(blocks)
        if len(text) > DIGEST_MAX_CHARS:
            text = text[:DIGEST_MAX_CHARS - 40].rstrip() + '\n\u2026 (more in Messages)'
        ids = '+'.join(i for _, _, ids_ in groups for i in ids_)
        return {'kind': 'digest', 'lines': [{'id': 'digest:' + (base_id or ids)[:200], 'text': text, 'body': True}]}
    if t == 'sent':
        # item 6: ONE confirmation, posted by the watcher: \u2713\u2713 when chat.db shows delivered, else \u2713
        # after the ~20s fallback. (Dry runs and delivery-check-off still come from the host.)
        label = _label(payload)
        note = str(payload.get('note') or '').strip()[:60]     # v1.4.1: "new contact, \u2022\u2022\u20220188"
        if payload.get('dryRun'):
            line = '\u2192 ' + label + ' (dry run, not sent)'
        elif note:
            line = '\u2192 ' + label + (' \u2713\u2713' if payload.get('delivered') else ' \u2713') + ' (' + note + ')'
        elif payload.get('delivered'):
            line = '\u2192 ' + label + ' \u2713\u2713 delivered'
        else:
            line = '\u2192 ' + label + ' \u2713 sent'
        return {'kind': 'sent', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'dry_run':
        return {'kind': 'dry_run', 'lines': [{'id': base_id, 'text': '\u2192 ' + _label(payload) + ' (dry run, not sent)', 'body': False}]}
    if t == 'send_failed':
        if str(payload.get('reason') or '') == 'unconfirmed':
            # W3: no chat.db row ever appeared for this send within ~10 minutes.
            line = '\u26a0\ufe0f Couldn\u2019t confirm your reply to ' + _label(payload) + ' was delivered.'
            return {'kind': 'send_failed', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
        err = str(payload.get('error') or '').strip()
        tail = (' (' + err + ')') if err else ''
        if payload.get('superseded'):
            # W3 nit: a soft "\u2713 sent" already went out, then the send failed -> say this corrects it.
            line = ('\u26a0\ufe0f Correction: your reply to ' + _label(payload)
                    + ' actually failed to deliver \u2014 disregard the earlier \u2713 sent' + tail + '.')
        else:
            line = '\u26a0\ufe0f Couldn\u2019t deliver your reply to ' + _label(payload) + ' \u2014 it may not have sent' + tail + '.'
        return {'kind': 'send_failed', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'needs_confirm':
        # W1/item 3: a label-only confirmation prompt. Never carries the message body. should_notify false.
        label = _label(payload)
        alt = str(payload.get('alt') or '').strip()
        if payload.get('reason') == 'new_contact':
            # v1.4.0: someone found in the Mac's Contacts (host-composed, name + last 4 digits, no body).
            detail = str(payload.get('detail') or '').strip()[:400]
            text = detail or ('\u2753 Text %s from your Contacts? Reply yes within 2 min.' % label)
            return {'kind': 'needs_confirm', 'lines': [{'id': base_id, 'text': text, 'body': False}]}
        if payload.get('reason') == 'directive':
            core = ('Send to %s instead of %s?' % (label, alt)) if alt else ('Send to %s?' % label)
        else:
            core = 'Reply to %s?' % label
        detail = str(payload.get('detail') or '').strip()[:400]      # v1.4.1: host-composed, body-free
        text = detail or ('\u2753 ' + core + ' Reply yes within 2 min.')
        return {'kind': 'needs_confirm', 'lines': [{'id': base_id, 'text': text, 'body': False}]}
    if t == 'needs_target':
        detail = str(payload.get('detail') or 'Who\u2019s this for? Start with a name, like "Alex: \u2026" or "text Alex \u2026".').strip()
        return {'kind': 'needs_target', 'lines': [{'id': base_id, 'text': detail, 'body': False}]}
    if t == 'blocked':
        label = _label(payload)
        if payload.get('dryRun'):
            line = '\U0001f6ab (dry run) would mute ' + label + '. Say "/unblock ' + label + '" to undo once live.'
        else:
            line = '\U0001f6ab Muted ' + label + '. Say "/unblock ' + label + '" to undo.'
        return {'kind': 'blocked', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'unblocked':
        return {'kind': 'unblocked', 'lines': [{'id': base_id, 'text': '\u2705 Unmuted ' + _label(payload) + '.', 'body': False}]}
    if t in ('status', 'catching_up', 'resynced'):
        status = t if t in ('catching_up', 'resynced') else str(payload.get('status') or '').strip()
        detail = str(payload.get('detail') or '').strip()
        if status == 'catching_up':
            line = '\u23f3 Catching up \u2014 relaying a burst in batches (nothing dropped).'
        elif status == 'resynced':
            line = '\U0001f504 Resynced after a long gap; older messages were skipped.'
        elif status == 'messages_not_running':
            line = '\u26a0\ufe0f Messages isn\u2019t running on the Mac, so replies can\u2019t be sent right now.'
        elif status == 'spam_summary' and detail:
            line = detail[:300]               # v1.4.6: counts only ("\U0001f6ab 3 texts went to Messages \u203a Spam ...")
        elif status == 'fda_missing':
            line = '\u26a0\ufe0f The watcher can\u2019t read Messages yet (Full Disk Access). New texts aren\u2019t coming through.'
        else:
            line = '\u2139\ufe0f ' + (detail or ('Status: ' + (status or 'unknown')))
        return {'kind': 'status:' + (status or 'unknown'), 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'wrong_account':
        line = '\u26a0\ufe0f The relay tab is signed in to the wrong X account \u2014 replies are paused until you switch back to the owner account.'
        return {'kind': 'wrong_account', 'lines': [{'id': base_id or 'wrong_account', 'text': line, 'body': False}]}
    if t == 'guess_expired':
        label = _label(payload, '')
        tail = (' to ' + label) if label else ''
        return {'kind': 'guess_expired', 'lines': [{'id': base_id, 'text': '\u231b Not sent%s.' % tail, 'body': False}]}
    if t == 'refused':
        reason = str(payload.get('reason') or '').strip()
        label = _label(payload, '')
        if reason == 'echo':
            line = '\u26a0\ufe0f Didn\u2019t send that to ' + (label or 'them') + ' \u2014 it looked like a relayed message bounced straight back.'
        elif reason == 'not_sent' and str(payload.get('detail') or '').strip():
            # v1.4.6: the Mac's fixed, body-free reason ("There was nothing after the name to send …")
            detail = re.sub(r'\s+', ' ', str(payload.get('detail')))[:200].strip()
            line = '\u26a0\ufe0f Not sent: ' + detail
        elif reason == 'attachment':
            # v1.4.6: a photo/file he sent in X Chat couldn't be read or was over the limit (counts only)
            try:
                n = max(1, min(10, int(payload.get('count') or 1)))
            except (TypeError, ValueError):
                n = 1
            what = 'An attachment' if n == 1 else '%d attachments' % n
            line = '\u26a0\ufe0f %s couldn\u2019t be sent%s (too large, over 25 MB, or not loaded in X). ' % (
                what, (' to ' + label) if label else '') + ('The text went through.' if payload.get('sentText') else 'Nothing was sent.')
        else:
            line = '\u26a0\ufe0f A reply wasn\u2019t sent' + ((' to ' + label) if label else '') + '.'
        return {'kind': 'refused', 'lines': [{'id': base_id, 'text': line, 'body': False}]}
    if t == 'relay_test':
        return _relay_test(payload, base_id)
    raise ValueError('unknown webhook type: ' + (t or '(empty)'))


_TEST_DETAIL_MAX = 100


def _test_detail(v):
    d = re.sub(r'\s+', ' ', str(v or '')).strip()
    return (d[:_TEST_DETAIL_MAX - 1] + '\u2026') if len(d) > _TEST_DETAIL_MAX else (d or 'no detail')


def _relay_test(payload, base_id):
    """v1.3.8 /test report: ONE silent X message, a header then one short line per step. The host sends
    the steps it could check itself; the "webhook accepted" and "routine posted" lines prove themselves
    by appearing at all. The header starts with the test sigil and carries the test tag so the extension
    recognizes it (and the echo guard treats it as bot output, so it is never relayed back out)."""
    steps = [s for s in (payload.get('steps') if isinstance(payload.get('steps'), list) else []) if isinstance(s, dict)]
    tag = re.sub(r'[^0-9a-f]', '', str(payload.get('tag') or payload.get('testId') or '').lower())[:6]
    host = [s for s in steps if s.get('name') not in ('send', 'webhook', 'routine')]
    send = [s for s in steps if s.get('name') == 'send']
    rows = [(s.get('ok'), _test_detail(s.get('detail'))) for s in host]
    rows += [(True, 'webhook POST accepted'), (True, 'routine posted this report')]
    rows += [(s.get('ok'), _test_detail(s.get('detail'))) for s in send]
    failed = sum(1 for ok, _ in rows if ok is False)
    header = '\U0001f9ea relay test' + ((' #' + tag) if tag else '') + (
        ' \u2014 all checks passed' if not failed else ' \u2014 %d failed' % failed)
    body = ['%s %d %s' % ('\u2705' if ok is True else '\u274c' if ok is False else '\u2796', i, d)
            for i, (ok, d) in enumerate(rows, 1)]
    return {'kind': 'relay_test',
            'lines': [{'id': base_id or ('relay_test:' + tag), 'text': '\n'.join([header] + body), 'body': False}]}


# ---------- dedupe (ids only, TTL; no bodies on disk; concurrency-safe) ----------
class Dedupe:
    """Payload-id dedupe store. Reads are lock-free; commits take an exclusive flock on a sidecar lock
    file, re-read the store, merge, prune by TTL, and write atomically (mkstemp + rename), so many
    relay_post processes running at once never lose each other's ids or corrupt the file (item 10)."""

    def __init__(self, path, ttl_seconds=86400, now=time.time):
        self.path = os.path.expanduser(path)
        self.lock_path = self.path + '.lock'
        self.ttl = int(ttl_seconds)
        self.now = now
        self._d = self._read()

    def _read(self):
        try:
            with open(self.path) as fh:
                d = {str(k): float(v) for k, v in (json.load(fh) or {}).items()}
        except (FileNotFoundError, ValueError):
            d = {}
        cut = self.now() - self.ttl
        return {k: v for k, v in d.items() if v >= cut}

    def seen(self, msg_id):
        return bool(msg_id) and msg_id in self._d

    def _ensure_dir(self):
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)

    def commit(self, ids):
        """Record ids under an exclusive lock, merging with whatever other processes wrote meanwhile."""
        ids = [i for i in ids if i]
        if not ids:
            return
        self._ensure_dir()
        with open(self.lock_path, 'a') as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            d = self._read()                         # re-read inside the lock to capture concurrent writers
            t = self.now()
            for i in ids:
                d[i] = t
            cut = t - self.ttl
            d = {k: v for k, v in d.items() if v >= cut}
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or '.', prefix='.seen-')
            try:
                with os.fdopen(fd, 'w') as fh:
                    json.dump(d, fh)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            self._d = d


# ---------- key cache (v1.4.5 speed; key material only, never events or bodies) ----------
class KeyCache:
    """The relay conversation's `conversation_key_events` (wrapped conversation keys) and the senders'
    public `signing_keys`, saved after a run that fetched them so later runs can skip both fetches.
    Message events are never stored. Mode 600, atomic writes, one conversation, TTL KEY_CACHE_TTL_S."""

    def __init__(self, path=None, ttl=KEY_CACHE_TTL_S, now=time.time):
        self.path = os.path.expanduser(path or KEY_CACHE_PATH)
        self.ttl = ttl
        self.now = now

    def load(self, conv):
        try:
            if os.path.getsize(self.path) > KEY_CACHE_MAX_BYTES:
                return None
            with open(self.path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get('conversation_id') != conv:
            return None
        if not (0 <= self.now() - float(d.get('savedAt') or 0) <= self.ttl):
            return None
        cke, sk = d.get('conversation_key_events'), d.get('signing_keys')
        if not cke or not sk or not isinstance(cke, list) or not isinstance(sk, list):
            return None
        return {'conversation_key_events': cke, 'signing_keys': sk}

    def save(self, conv, cke, sk):
        d = {'v': 1, 'conversation_id': conv, 'savedAt': self.now(), 'conversation_key_events': cke, 'signing_keys': sk}
        data = json.dumps(d)
        if len(data) > KEY_CACHE_MAX_BYTES:
            return False
        folder = os.path.dirname(self.path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.keys-')
        try:
            with os.fdopen(fd, 'w') as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return True

    def clear(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


# ---------- xchat_lite location + encryption ----------
def locate_helper(helper):
    """Return (python, script) for xchat_lite, honoring explicit paths then the skill's clone layout."""
    py = (helper or {}).get('python')
    script = (helper or {}).get('script')
    if py and script:                      # the routine is authoritative about where it cloned xchat_lite
        return os.path.expanduser(str(py)), os.path.expanduser(str(script))   # item N3: expand ~ paths
    home = os.path.expanduser('~')
    roots = (os.path.join(home, 'xchat-grokbot-helper'), os.path.join(os.getcwd(), 'xchat-grokbot-helper'),
             os.path.join(home, 'xchat-lite'), os.path.join(os.getcwd(), 'xchat-lite'))
    for root in roots:
        sp = os.path.join(root, 'xchat_lite.py')
        v = os.path.join(root, '.venv', 'bin', 'python')
        if os.path.exists(sp):
            return (v if os.path.exists(v) else sys.executable), sp
    return None, None


def _run(argv, stdin_obj, runner):
    """Run a helper command, feeding JSON on STDIN. Text never appears in argv, and helper stderr is
    never surfaced (it could echo message text) \u2014 only the exit code is reported (item 3)."""
    proc = runner(argv, input=json.dumps(stdin_obj).encode('utf-8'))
    out = proc.stdout.decode('utf-8') if isinstance(proc.stdout, (bytes, bytearray)) else (proc.stdout or '')
    if proc.returncode != 0:
        raise RuntimeError('xchat_lite exited %s' % proc.returncode)
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError('xchat_lite returned non-JSON output')


def session_encrypt_stdin(conversation_id, text, xchat, reply_to=None):
    """Build the session-encrypt stdin. The body goes here (stdin), never on argv. session-encrypt
    unwraps the conversation key from conversation_key_events itself each run (robust key resolution),
    so there is no separate key cache to go stale. `reply_to`, when set, is the X event id of the
    owner's own reply so the confirmation threads under their bubble (item 1); xchat_lite ignores it if
    it cannot thread, which is the graceful fallback to a plain line."""
    body = {'conversation_id': conversation_id, 'text': text,
            'events': (xchat or {}).get('events') or [],
            'conversation_key_events': (xchat or {}).get('conversation_key_events') or [],
            'signing_keys': (xchat or {}).get('signing_keys') or []}
    prep = (xchat or {}).get('prepare')
    if prep:
        body['prepare'] = prep
    if reply_to:
        body['reply_to'] = str(reply_to)
    return body


def encrypt_line(conversation_id, text, xchat, helper, runner, reply_to=None, should_notify=False, media=None):
    """Encrypt one line via xchat_lite session-encrypt. Returns the send dict for send_chat_message.
    session-encrypt warms keys and unwraps the conversation key from conversation_key_events in one
    process (robust key resolution). Raises on failure."""
    py, script = locate_media_helper(helper) if media else locate_helper(helper)
    if not py or not script:
        raise RuntimeError('xchat_lite not found (see x-api-mcp-guide skill)')
    base = [py, script]
    h = helper or {}
    for flag, key in (('--user-id', 'user_id'), ('--key-version', 'key_version'), ('--juicebox', 'juicebox')):
        if h.get(key):
            val = os.path.expanduser(str(h[key])) if key == 'juicebox' else str(h[key])   # item N3
            base += [flag, val]
    stdin_obj = session_encrypt_stdin(conversation_id, text, xchat, reply_to)
    if media:
        stdin_obj['media'] = media                     # v1.4.6: [{media_hash_key, input, conversation_key_version}]
    res = _run(base + ['session-encrypt'], stdin_obj, runner)
    return {
        'conversation_id': str(res.get('id') or conversation_id),
        'message_id': res.get('message_id') or str(_uuid.uuid4()),
        'encoded_message_create_event': res.get('encoded_message_create_event'),
        'encoded_message_event_signature': res.get('encoded_message_event_signature'),
        'add_conversation_keys': res.get('add_conversation_keys') if res.get('needs_add_conversation_keys_before_send') else None,
        # item 1: a relayed inbound message pings the owner; every status/confirmation line is silent.
        'should_notify': bool(should_notify),
        # item 1: thread a confirmation under the owner's reply when the webhook carried its X event id.
        'reply_to': str(reply_to) if reply_to else None,
    }


# ---------- main ----------
def _default_runner(helper):
    """subprocess runner that carries XDG_CONFIG_HOME (and any helper.env) so xchat_lite finds the
    sender's saved key at $XDG_CONFIG_HOME/xchat-grokbot-helper/saved-key.json (item 3). The call
    signature stays (argv, input=...) so a test runner can stand in unchanged."""
    env = dict(os.environ)
    h = helper or {}
    if h.get('xdg_config_home'):
        # expand ~ before exporting so xchat_lite finds $XDG_CONFIG_HOME/xchat-grokbot-helper/saved-key.json
        env['XDG_CONFIG_HOME'] = os.path.expanduser(str(h['xdg_config_home']))   # item N3
    for k, v in (h.get('env') or {}).items():
        val = str(v)
        if str(k).upper() == 'XDG_CONFIG_HOME' or '/' in val or val.startswith('~'):
            val = os.path.expanduser(val)
        env[str(k)] = val
    return lambda argv, input=b'': subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def process(req, runner=None):
    payload = req.get('payload') or {}
    conv = str(req.get('conversation_id') or payload.get('relayConversationId') or '').strip()
    ev = format_event(payload)                              # may raise ValueError -> error
    runner = runner or _default_runner(req.get('helper'))

    dedupe_path = req.get('dedupe_path') or '~/.imessage-relay-routine/seen_ids.json'
    dd = Dedupe(dedupe_path, req.get('ttl_seconds', 86400))
    lines = [ln for ln in ev['lines'] if not dd.seen(ln['id'])]
    if not lines:
        return {'action': 'skip', 'reason': 'duplicate id(s); nothing to post', 'posted_kind': ev['kind']}

    if not conv:
        return {'action': 'error', 'reason': 'no relay conversation_id'}

    xchat = dict(req.get('xchat') or {})
    # v1.4.5 speed: the keys come from the request (then cached) or from the key cache. With neither,
    # ask the routine to fetch them ({"action":"fetch"}) and run again; nothing was committed yet.
    cache = None if req.get('key_cache') is False else KeyCache(
        req.get('key_cache_path') or os.path.join(os.path.dirname(os.path.expanduser(dedupe_path)) or '.', 'xchat_keys.json'))
    supplied = bool(xchat.get('conversation_key_events') and xchat.get('signing_keys'))
    from_cache = False
    if not supplied and not xchat.get('prepare'):
        cached = cache.load(conv) if cache else None
        if not cached:
            return {'action': 'fetch', 'need': list(NEED_KEYS), 'posted_kind': ev['kind'],
                    'reason': 'no cached keys: fetch conversation_key_events + signing_keys and run again'}
        xchat.update(cached)
        from_cache = True

    # v1.4.6 photos: run 1 turns the copied photos into upload parts ({"action":"upload"}); run 2 (with
    # media_keys) builds the messages that carry them. Any photo that can't make it gets a clear notice.
    media_dir = req.get('media_dir') or os.path.join(os.path.dirname(os.path.expanduser(dedupe_path)) or '.', 'media')
    try:
        media_sweep(media_dir)
    except OSError:
        pass
    all_media = [it for ln in lines for it in ln.get('media') or []]
    media_keys = req.get('media_keys') if isinstance(req.get('media_keys'), dict) else {}
    line_media = {}
    if all_media and not req.get('media_upload'):
        # v1.4.7: only a routine prompt with the photo steps sets "media_upload": true. Any other prompt gets
        # the text with a notice (never an "upload" answer it doesn't know); the drop copy expires unfetched.
        req = dict(req, media_off=True)
    if all_media and req.get('media_off'):
        media_cleanup(all_media, media_dir)
        for ln in lines:
            if ln.get('media'):
                ln['text'] = media_failed_text(ln['text'], len(ln['media']))
    elif all_media and not media_keys:
        # v1.4.7: fetch each photo from the encrypted drop first (served once; the routine never touches the Mac)
        drop = load_drop(req, dedupe_path)
        if locate_media_helper(req.get('helper'))[0]:
            for it in all_media:
                if not os.path.exists(media_box_path(it, media_dir)):
                    try:
                        media_fetch(it, drop, media_dir, getter=req.get('_drop_getter') or drop_get)
                    except Exception:
                        _rm(media_box_path(it, media_dir))
        try:
            uploads, failed = media_prepare(conv, all_media, xchat, req.get('helper'), runner, media_dir)
        except Exception:
            media_cleanup(all_media, media_dir)
            uploads, failed = [], list(all_media)
        if uploads:
            return {'action': 'upload', 'uploads': uploads, 'posted_kind': ev['kind'],
                    'failed': len(failed),
                    'reason': 'upload each photo (chat_media_upload_initialize/append), then run again with the same '
                              'payload and media_keys {token: media_hash_key}'}
        media_keys = {}                              # nothing uploadable: text with notices
    if all_media and not req.get('media_off'):
        for ln in lines:
            if not ln.get('media'):
                continue
            entries, missing = [], 0
            for it in ln['media']:
                key = str(media_keys.get(it['token']) or '')
                st = _read_media_state(it['token'], media_dir)
                if key and st and os.path.exists(media_box_path(it, media_dir)):
                    e = {'media_hash_key': key, 'input': media_box_path(it, media_dir)}
                    if st.get('conversation_key_version'):
                        e['conversation_key_version'] = str(st['conversation_key_version'])
                    entries.append((it, e, st))
                else:
                    missing += 1
            if missing:
                ln['text'] = media_failed_text(ln['text'], missing)
            if entries:
                line_media[ln['id']] = entries

    reply_to = payload.get('reply_to')          # item 1: X event id of the owner's reply (optional)
    if not xchat.get('events'):
        reply_to = None                         # v1.4.5: threading needs the fetched events; post unthreaded
    sends = []
    try:
        for ln in lines:
            # body lines (relayed texts) notify and never thread; status/confirmation lines are silent
            # and thread under the owner's bubble when a reply_to was supplied.
            notify = bool(ln.get('body'))
            rt = None if notify else reply_to
            entries = line_media.get(ln['id'])
            snd = encrypt_line(conv, ln['text'], xchat, req.get('helper'), runner, reply_to=rt, should_notify=notify,
                               media=[e for _, e, _ in entries] if entries else None)
            if entries:
                # finalize each upload with this message_id BEFORE send_chat_message
                snd['media_finalize'] = [{'token': it['token'], 'media_hash_key': e['media_hash_key'],
                                          'num_parts': str(st.get('num_parts') or 1)} for it, e, st in entries]
            sends.append(snd)
    except Exception as e:                                  # body-free error for the routine
        media_cleanup(all_media, media_dir)
        if from_cache:
            cache.clear()                                   # stale or bad cache: fetch fresh keys once
            return {'action': 'fetch', 'need': list(NEED_KEYS), 'posted_kind': ev['kind'],
                    'reason': 'cached keys did not work: fetch conversation_key_events + signing_keys and run again'}
        return {'action': 'error', 'reason': str(e)[:200], 'posted_kind': ev['kind'], 'encrypted': False}
    media_cleanup(all_media, media_dir)                     # v1.4.6: photos are in flight only; gone now
    if supplied and cache:
        try:
            cache.save(conv, xchat['conversation_key_events'], xchat['signing_keys'])
        except OSError:
            pass                                            # cache is only a speed-up

    # The sends are prepared and encrypted; remember their ids now (concurrency-safe). The routine
    # then calls send_chat_message. For strict "remember only after the send landed", set
    # defer_commit:true and call `relay_post.py --ack {"dedupe_path","ids":[…]}` after the send
    # succeeds instead (see SKILL.md). The Mac retries each POST until it gets a 2xx, so a committed
    # id is what prevents a retried webhook from being re-sent.
    ids = [ln['id'] for ln in lines]
    if not req.get('defer_commit'):
        dd.commit(ids)
    return {'action': 'send', 'sends': sends, 'encrypted': True, 'posted_kind': ev['kind'], 'ack_ids': ids}


def ack(req):
    """Record ids as sent AFTER the routine's send_chat_message succeeded (strict dedupe path)."""
    dedupe_path = req.get('dedupe_path') or '~/.imessage-relay-routine/seen_ids.json'
    dd = Dedupe(dedupe_path, req.get('ttl_seconds', 86400))
    ids = [str(i) for i in (req.get('ids') or []) if i]
    dd.commit(ids)
    return {'action': 'ack', 'remembered': len(ids)}


def _read_request(infile):
    """Read the request JSON from a file or stdin. The --in file holds the (untrusted) message body, so
    it is DELETED right after it is read — even if parsing or anything downstream then fails (item N7)."""
    if infile:
        try:
            with open(infile, encoding='utf-8') as fh:
                raw = fh.read()
        finally:
            try:
                os.remove(infile)
            except OSError:
                pass
    else:
        raw = sys.stdin.read()
    return json.loads(raw or '{}')


def _read_payload_file(path):
    """Read the raw Mac webhook event JSON from a file and DELETE the file right after (even on error):
    it holds the untrusted body, so it never lingers and never reaches a command line (item B). The file
    must parse as exactly ONE JSON object; anything else (a bare value, a list, truncated/garbage, or two
    concatenated objects) is refused with a body-free error so a half-written or corrupt file is never
    forwarded. The raw text is never echoed."""
    try:
        with open(path, encoding='utf-8') as fh:
            raw = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    obj = json.loads(raw or 'null')          # JSONDecodeError (a ValueError) on garbage/truncation/two objects
    if not isinstance(obj, dict):
        raise ValueError('payload-file is not a single JSON object')
    return obj


def main(argv=None):
    ap = argparse.ArgumentParser(description='Format + encrypt one Mac relay webhook for X Chat.')
    ap.add_argument('--in', dest='infile', help='read the request JSON from this file instead of stdin '
                    '(keeps untrusted message text out of any command line); the file is deleted right '
                    'after it is read')
    ap.add_argument('--ack', action='store_true', help='record ids as sent AFTER send_chat_message '
                    'succeeded (post-send dedupe). Pass ids as arguments: --ack ID1 ID2 ... '
                    '[--dedupe-path P]; with no id arguments, reads {dedupe_path, ids:[...]} on stdin')
    ap.add_argument('--dedupe-path', dest='dedupe_path', help='dedupe store path for --ack')
    ap.add_argument('--payload-file', dest='payload_file', help='read the raw Mac webhook event (the '
                    'message body) from this file and use it as the request payload; the file is deleted '
                    'right after it is read, so the --in request can be assembled with no body (item B)')
    ap.add_argument('--forget-keys', dest='forget_keys', action='store_true',
                    help='delete the cached conversation keys (the next run fetches them again)')
    ap.add_argument('ids', nargs='*', help='message ids to record as sent (used with --ack)')
    args = ap.parse_args(argv)
    # main ALWAYS prints exactly one JSON object and never raises (item 10).
    if args.forget_keys:
        KeyCache().clear()
        print(json.dumps({'action': 'forgot_keys'}))
        return 0
    try:
        if args.ack and args.ids:
            req = {'ids': args.ids}
            if args.dedupe_path:
                req['dedupe_path'] = args.dedupe_path
        else:
            req = _read_request(args.infile)
            if args.payload_file:
                # the body lives only in the payload file; splice it in, then it is gone from disk.
                if not isinstance(req, dict):
                    req = {}
                try:
                    req['payload'] = _read_payload_file(args.payload_file)
                except (OSError, ValueError):
                    # body-free: never include the file's contents or the parser message text.
                    print(json.dumps({'action': 'error', 'reason': 'payload-file is not a single valid JSON object'}))
                    return 2
    except (OSError, ValueError) as e:
        print(json.dumps({'action': 'error', 'reason': 'could not read request JSON: %s' % type(e).__name__}))
        return 2
    try:
        if args.ack:
            if args.dedupe_path and isinstance(req, dict) and 'dedupe_path' not in req:
                req['dedupe_path'] = args.dedupe_path
            res = ack(req)
        else:
            res = process(req)
    except ValueError as e:
        res = {'action': 'error', 'reason': str(e)[:200]}
    except Exception as e:                                  # never crash without a JSON line
        res = {'action': 'error', 'reason': '%s' % type(e).__name__}
    print(json.dumps(res))
    return 0 if res.get('action') != 'error' else 2


if __name__ == '__main__':
    sys.exit(main())
