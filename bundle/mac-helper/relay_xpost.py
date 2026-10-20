#!/usr/bin/env python3
"""v1.4.18 direct poster. Runs under ~/.imessage-relay/xpost-venv (Python 3.12, chatxdk<0.6).

The watcher (Python 3.9) spawns `relay_xpost.py post` and writes one JSON object on stdin:
  {"texts":[{"text","header","notify"}]}
This process prints exactly one JSON line on stdout and exits 0 even on failure:
  {"ok":true,"message_id":"...","header_sent":bool}
  {"ok":false,"retryable":bool,"code":"...","header_sent":bool}

chatxdk is imported lazily so this file still imports on Python 3.9 (unit tests).
Message text is never logged, never put in argv, and never written to disk.
Secrets (refresh token, client secret, Chat private key, PIN, auth code) stay in the Keychain
or in process memory. The access token is memory only.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_common as rc  # noqa: E402
import relay_keychain as rk  # noqa: E402

API = 'https://api.x.com'
AUTHORIZE = 'https://x.com/i/oauth2/authorize'
SCOPES = 'dm.write dm.read tweet.read users.read offline.access'
CONV_PATH_RE = __import__('re').compile(r'^([0-9]{1,19}|[0-9]{1,19}-[0-9]{1,19}|g[0-9]{1,19})$')
KEY_CACHE_TTL_S = 6 * 3600
KEY_CACHE_MAX_BYTES = 512 * 1024
HTTP_TIMEOUT_S = 5
SETUP_TIMEOUT_S = 180


class NetError(Exception):
    pass


def emit(obj):
    sys.stdout.write(json.dumps(_public_outcome(obj), separators=(',', ':')) + '\n')
    sys.stdout.flush()


def _public_outcome(obj):
    if not isinstance(obj, dict):
        return {'ok': False, 'retryable': True, 'code': 'internal'}
    keep = ('ok', 'retryable', 'code', 'message_id', 'header_sent', 'message_ids',
            'userIdSet', 'keySaved', 'redirect', 'guessesRemaining')
    out = {}
    for k in keep:
        if k in obj:
            out[k] = obj[k]
    if 'ok' not in out:
        out['ok'] = False
    return out


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return verifier, challenge


def authorize_url(client_id, redirect_uri, state, challenge):
    q = urllib.parse.urlencode({
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': SCOPES,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })
    return AUTHORIZE + '?' + q


def callback_error(expected_state, got):
    if not got:
        return 'timeout'
    if got.get('error'):
        return 'denied'
    if got.get('state') != expected_state:
        return 'state_mismatch'
    if not got.get('code'):
        return 'no_code'
    return ''


def conv_path(conv):
    c = str(conv or '').strip().replace(':', '-')
    if not CONV_PATH_RE.match(c):
        return ''
    if not rc.CONV_RE.match(c):
        return ''
    return c


def parse_token(data):
    if not isinstance(data, dict):
        return None
    access = data.get('access_token')
    if not access or not isinstance(access, str):
        return None
    refresh = data.get('refresh_token') if isinstance(data.get('refresh_token'), str) else ''
    return {'access_token': access, 'refresh_token': refresh}


def basic_auth(client_id, client_secret):
    raw = ('%s:%s' % (client_id, client_secret)).encode('utf-8')
    return 'Basic ' + base64.b64encode(raw).decode('ascii')


def _as_list(raw):
    if isinstance(raw, dict):
        data = raw.get('data', raw)
    else:
        data = raw
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return []


def map_signing(raw, user_id):
    """API public-key rows -> chatxdk SigningKeyEntry dicts. Drop rows with no identity key."""
    out = []
    for ent in _as_list(raw):
        if not isinstance(ent, dict):
            continue
        identity = ent.get('public_key') or ent.get('identity_public_key')
        signing = ent.get('signing_public_key')
        if not identity or not signing or ent.get('public_key_version') is None:
            continue
        item = {
            'public_key_version': str(ent.get('public_key_version')),
            'public_key': str(signing),
            'identity_public_key': str(identity),
            'user_id': str(ent.get('user_id') or user_id or ''),
        }
        sig = ent.get('identity_public_key_signature')
        if sig:
            item['identity_public_key_signature'] = str(sig)
        out.append(item)
    return out


def latest_public_key(raw):
    """The highest public_key_version row from a public_keys response, or None."""
    best, best_n = None, None
    for ent in _as_list(raw):
        if not isinstance(ent, dict):
            continue
        try:
            n = int(ent.get('public_key_version'))
        except (TypeError, ValueError):
            continue
        if best_n is None or n >= best_n:
            best, best_n = ent, n
    return best


def key_events_from(resp):
    if not isinstance(resp, dict):
        return []
    meta = resp.get('meta') if isinstance(resp.get('meta'), dict) else {}
    ev = meta.get('conversation_key_events') or []
    if not isinstance(ev, list):
        return []
    return [str(x) for x in ev if isinstance(x, str) and x]


def participant_ids(conv, user_id):
    c = conv_path(conv)
    ids = []
    if not c:
        ids.append(user_id)
    elif c.startswith('g'):
        ids.append(user_id)
    elif '-' in c:
        ids.extend(c.split('-'))
    else:
        ids.append(c)
        if user_id:
            ids.append(user_id)
    out = []
    for i in ids:
        i = __import__('re').sub(r'[^0-9]', '', str(i or ''))
        if i and i not in out:
            out.append(i)
    return out


def pick_conversation_key(extracted):
    if not isinstance(extracted, dict):
        return None, None
    keys = extracted.get('keys')
    if not isinstance(keys, dict) or not keys:
        return None, None
    latest = extracted.get('latest_version')
    if latest is not None and (latest in keys or str(latest) in keys):
        key = keys.get(latest)
        if key is None:
            key = keys.get(str(latest))
        if key:
            return key, str(latest)
    best_n, best = None, None
    for ver, key in keys.items():
        if not key:
            continue
        try:
            n = int(ver)
        except (TypeError, ValueError):
            n = -1
        if best_n is None or n >= best_n:
            best_n, best = n, (str(ver), key)
    if not best:
        return None, None
    return best[1], best[0]


def message_id_of(payload):
    if isinstance(payload, dict):
        return str(payload.get('message_id') or '')
    return str(getattr(payload, 'message_id', '') or '')


def send_body(payload):
    """Map a chatxdk SendPayload (object or dict) onto the X send-message JSON."""
    if isinstance(payload, dict):
        mid = payload.get('message_id')
        enc = payload.get('encrypted_content') or payload.get('encoded_message_create_event')
        sig = payload.get('encoded_event_signature') or payload.get('encoded_message_event_signature')
    else:
        mid = getattr(payload, 'message_id', None)
        enc = getattr(payload, 'encrypted_content', None)
        sig = getattr(payload, 'encoded_event_signature', None)
    return {
        'message_id': mid,
        'encoded_message_create_event': enc,
        'encoded_message_event_signature': sig,
    }


def post_payloads(items, http):
    """Send already-encrypted payloads in order. `http(body) -> (status, obj)` or raises NetError.

    A network error during send is ambiguous (the bytes may have left): retryable false, code timeout.
    200/201 is success. 408/429/5xx and other 4xx are retryable (X did not accept the message).
    """
    header_sent = False
    sent = []
    for meta, payload in items:
        body = send_body(payload)
        try:
            status, _resp = http(body)
        except NetError:
            return {'ok': False, 'retryable': False, 'code': 'timeout', 'header_sent': header_sent,
                    'message_id': sent[-1] if sent else ''}
        if status in (200, 201):
            mid = message_id_of(payload)
            if mid:
                sent.append(mid)
            if meta.get('header'):
                header_sent = True
            continue
        retryable = isinstance(status, int) and (status in (408, 429) or status >= 400)
        code = ('http_%s' % status) if status else 'timeout'
        return {'ok': False, 'retryable': bool(retryable), 'code': code, 'header_sent': header_sent,
                'message_id': sent[-1] if sent else ''}
    return {'ok': True, 'retryable': False, 'code': 'ok', 'header_sent': header_sent,
            'message_id': sent[-1] if sent else '', 'message_ids': sent}


def post_prepared(texts, http, encrypt):
    """Encrypt every line first (still before send), then POST. Encrypt failure is retryable."""
    try:
        items = []
        for t in texts:
            items.append((t, encrypt(t.get('text') or '', bool(t.get('notify')))))
    except Exception:
        return {'ok': False, 'retryable': True, 'code': 'encrypt_failed', 'header_sent': False}
    return post_payloads(items, http)


class KeyCache:
    """conversation_key_events + signing_keys only. Never message events, bodies, or juicebox tokens."""

    def __init__(self, path=None, ttl=KEY_CACHE_TTL_S, now=time.time):
        self.path = path or os.path.join(rc.APP_DIR, 'xchat_keys.json')
        self.ttl = ttl
        self.now = now

    def load(self, conv):
        try:
            if os.path.getsize(self.path) > KEY_CACHE_MAX_BYTES:
                return None
            with open(self.path, encoding='utf-8') as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get('conversation_id') != conv:
            return None
        try:
            age = self.now() - float(d.get('savedAt') or 0)
        except (TypeError, ValueError):
            return None
        if not (0 <= age <= self.ttl):
            return None
        cke, sk = d.get('conversation_key_events'), d.get('signing_keys')
        if not cke or not sk or not isinstance(cke, list) or not isinstance(sk, list):
            return None
        return {'conversation_key_events': cke, 'signing_keys': sk}

    def save(self, conv, cke, sk):
        d = {'v': 1, 'conversation_id': conv, 'savedAt': self.now(),
             'conversation_key_events': cke, 'signing_keys': sk}
        data = json.dumps(d)
        if len(data) > KEY_CACHE_MAX_BYTES:
            return False
        folder = os.path.dirname(self.path)
        try:
            if folder:
                os.makedirs(folder, mode=0o700, exist_ok=True)
            tmp = self.path + '.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            return False
        return True

    def clear(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def cache_age(path=None, now=None):
    path = path or os.path.join(rc.APP_DIR, 'xchat_keys.json')
    try:
        with open(path, encoding='utf-8') as fh:
            d = json.load(fh)
        saved = float(d.get('savedAt') or 0)
    except (OSError, ValueError, TypeError):
        return None
    if saved <= 0:
        return None
    t = time.time() if now is None else now
    return max(0, int(t - saved))


class _Auth:
    """One refresh for the whole post. A second 401 stays a failure."""

    def __init__(self, refresh):
        self.refresh = refresh
        self.used = 0

    def call(self, fn):
        status, body = fn()
        if status == 401 and not self.used:
            self.used += 1
            if self.refresh():
                status, body = fn()
        return status, body


def _fail(code, retryable=True):
    return {'ok': False, 'retryable': bool(retryable), 'code': code, 'header_sent': False}


def perform_post(texts, conv, user_id, key_version, private_key, chat, http, cache=None, _refetch=True):
    """Prepare keys, encrypt, POST. `http` has refresh/get_events/get_signing/post. `chat` is chatxdk-like.

    Does not create a conversation key. An empty thread returns no_conversation_key (retryable) so the
    routine's existing prepare path can run. Network errors before send are retryable; during send they
    are timeout (possibly delivered).
    """
    path = conv_path(conv)
    if not path:
        return _fail('bad_conversation')
    auth = _Auth(http.refresh)
    from_cache = False
    try:
        cached = cache.load(path) if cache is not None else None
        if cached:
            events = cached['conversation_key_events']
            signing = cached['signing_keys']
            from_cache = True
        else:
            status, ev = auth.call(lambda: http.get_events(path))
            if status != 200:
                return _fail('http_%s' % status if status else 'network')
            events = key_events_from(ev)
            status, sk = auth.call(lambda: http.get_signing(path))
            if status != 200:
                return _fail('http_%s' % status if status else 'network')
            signing = map_signing(sk, user_id)
            if cache is not None and events and signing:
                try:
                    cache.save(path, events, signing)
                except OSError:
                    pass
        try:
            chat.import_keys(bytes(private_key), version=str(key_version))
        except TypeError:
            chat.import_keys(bytes(private_key))
        chat.set_identity(str(user_id), str(key_version))
        try:
            chat.set_cache_keys(True)
        except Exception:
            pass
        if signing:
            chat.set_signing_keys(signing)
        extracted = chat.extract_conversation_keys(list(events or []))
        key, ver = pick_conversation_key(extracted)
        if not key:
            if from_cache and _refetch and cache is not None:
                cache.clear()
                return perform_post(texts, conv, user_id, key_version, private_key, chat, http, cache, _refetch=False)
            return _fail('no_conversation_key')

        def encrypt(text, notify):
            return chat.encrypt_message(path, text, conversation_key=key, conversation_key_version=ver, should_notify=bool(notify))

        prepared = post_prepared(texts, lambda body: auth.call(lambda: http.post(body)), encrypt)
    except NetError:
        return _fail('network')
    except Exception:
        prepared = _fail('encrypt_failed')
    if prepared.get('code') == 'encrypt_failed' and from_cache and _refetch and cache is not None:
        cache.clear()
        return perform_post(texts, conv, user_id, key_version, private_key, chat, http, cache, _refetch=False)
    return prepared


def request_json(method, url, token=None, json_body=None, form=None, basic=None, timeout=HTTP_TIMEOUT_S):
    headers = {'User-Agent': 'imessage-relay-xpost/' + rc.VERSION, 'Accept': 'application/json'}
    data = None
    if basic:
        headers['Authorization'] = basic
    elif token:
        headers['Authorization'] = 'Bearer ' + token
    if form is not None:
        data = urllib.parse.urlencode(form).encode('utf-8')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif json_body is not None:
        data = json.dumps(json_body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, 'status', 200)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b''
        status = e.code
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        raise NetError('network')
    obj = None
    if raw:
        try:
            obj = json.loads(raw.decode('utf-8'))
        except ValueError:
            obj = None
    return status, obj


def _open_chat(juicebox_json=None):
    from chat_xdk import Chat
    if juicebox_json:
        return Chat(juicebox_json if isinstance(juicebox_json, str) else json.dumps(juicebox_json))
    return Chat()


class _LiveHttp:
    def __init__(self, client_id, client_secret, refresh_token, user_id):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.user_id = str(user_id or '')
        self.token = ''

    def refresh(self):
        status, obj = request_json(
            'POST', API + '/2/oauth2/token',
            form={'grant_type': 'refresh_token', 'refresh_token': self.refresh_token, 'client_id': self.client_id},
            basic=basic_auth(self.client_id, self.client_secret))
        tok = parse_token(obj) if status == 200 else None
        if not tok:
            return False
        self.token = tok['access_token']
        rotated = tok.get('refresh_token') or ''
        if rotated and rotated != self.refresh_token:
            self.refresh_token = rotated
            try:
                rk.store('refresh-token', rotated, rk.trusted_bins())
            except Exception:
                pass
        return True

    def get_events(self, conv):
        return request_json('GET', API + '/2/chat/conversations/%s/events?max_results=100' % urllib.parse.quote(conv),
                            token=self.token)

    def get_signing(self, conv):
        rows = []
        for i in participant_ids(conv, self.user_id):
            status, obj = request_json('GET', API + '/2/users/%s/public_keys' % i, token=self.token)
            if status != 200:
                return status, obj
            for ent in _as_list(obj):
                if isinstance(ent, dict):
                    ent = dict(ent)
                    ent.setdefault('user_id', i)
                    rows.append(ent)
        return 200, {'data': rows}

    def post(self, body):
        return request_json('POST', API + '/2/chat/conversations/%s/messages' % urllib.parse.quote(conv_path_of_body(body)),
                            token=self.token, json_body=body)


def conv_path_of_body(body):
    """post() is called with the message body only. The live wrapper closes over the path."""
    return getattr(body, '_conv', '') or _LiveHttp._conv or ''


# The live POST URL needs the conversation id, which is not inside the message body.
_LiveHttp._conv = ''


def _bind_post(http, conv):
    path = conv

    def post(body):
        return request_json('POST', API + '/2/chat/conversations/%s/messages' % urllib.parse.quote(path),
                            token=http.token, json_body=body)

    http.post = post
    return http


def _decode_key(b64):
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    if not raw:
        return None
    return bytearray(raw)


def _wipe(buf):
    if isinstance(buf, bytearray):
        for i in range(len(buf)):
            buf[i] = 0


def _post_live(texts):
    cfg = rc.load_config()
    conv = conv_path(cfg.get('relayConversationId'))
    user_id = str(cfg.get('xpostUserId') or '')
    key_version = str(cfg.get('xpostKeyVersion') or '')
    client_id = str(cfg.get('xpostClientId') or '')
    if not conv or not user_id or not key_version or not client_id:
        return _fail('not_configured')
    refresh = rk.load('refresh-token')
    secret = rk.load('client-secret')
    key_b64 = rk.load('private-key')
    if not refresh or not secret or not key_b64:
        return _fail('keychain')
    private_key = _decode_key(key_b64)
    if not private_key:
        return _fail('keychain')
    try:
        try:
            chat = _open_chat()
        except ImportError:
            return _fail('no_chatxdk')
        http = _bind_post(_LiveHttp(client_id, secret, refresh, user_id), conv)
        if not http.refresh():
            return _fail('unauthorized')
        return perform_post(texts, conv, user_id, key_version, private_key, chat, http, KeyCache())
    finally:
        _wipe(private_key)


def _tty_in():
    return open('/dev/tty', 'r')


def _tty_out():
    return open('/dev/tty', 'w')


def prompt_line(label):
    inn, out = _tty_in(), _tty_out()
    try:
        out.write(label)
        out.flush()
        return inn.readline().strip()
    finally:
        inn.close()
        out.close()


def prompt_secret(label):
    import getpass
    out = _tty_out()
    old = sys.stdin
    inn = _tty_in()
    try:
        sys.stdin = inn
        return getpass.getpass(label, stream=out)
    finally:
        sys.stdin = old
        inn.close()
        out.close()


def _save_cfg(**kw):
    def _do():
        raw = rc.read_raw_config() or {}
        raw.update(kw)
        rc.write_json_atomic(rc.CONFIG_PATH, raw)
    rc.with_host_lock(_do)


def _listen_for_code(port, timeout_s):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != '/cb':
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            self.server.got = {
                'code': (qs.get('code') or [''])[0],
                'state': (qs.get('state') or [''])[0],
                'error': (qs.get('error') or [''])[0],
            }
            body = b'<html><body>iMessage Relay is connected. You can close this tab.</body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    try:
        server = HTTPServer(('127.0.0.1', int(port)), Handler)
    except OSError:
        return None, 'port_in_use'
    server.got = None
    server.timeout = 1
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline and not server.got:
            server.handle_request()
    finally:
        server.server_close()
    return server.got, ''


def cmd_setup():
    """Browser PKCE, then a hidden Chat PIN. Does not turn directPost on. Prints one JSON object."""
    cfg = rc.load_config()
    port = int(cfg.get('xpostRedirectPort') or 53682)
    redirect = 'http://127.0.0.1:%d/cb' % port
    existing = str(cfg.get('xpostClientId') or '')
    entered = prompt_line('X client id%s: ' % ((' [' + existing + ']') if existing else ''))
    client_id = entered or existing
    if not __import__('re').match(r'^[A-Za-z0-9_-]{1,200}$', client_id or ''):
        return {'ok': False, 'retryable': False, 'code': 'bad_client_id', 'redirect': redirect}
    client_secret = prompt_secret('X client secret (hidden): ')
    if not client_secret or '\n' in client_secret or '\r' in client_secret:
        return {'ok': False, 'retryable': False, 'code': 'client_secret_required', 'redirect': redirect}
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(client_id, redirect, state, challenge)
    err = _tty_out()
    try:
        err.write('Redirect URI (register it on the X app, then press Enter):\n%s\n' % redirect)
        err.flush()
    finally:
        err.close()
    prompt_line('Press Enter to open the browser. ')
    try:
        import subprocess
        subprocess.run(['/usr/bin/open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        err = _tty_out()
        try:
            err.write('Open this URL in a browser:\n%s\n' % url)
            err.flush()
        finally:
            err.close()
    got, err_code = _listen_for_code(port, SETUP_TIMEOUT_S)
    if err_code:
        return {'ok': False, 'retryable': False, 'code': err_code, 'redirect': redirect}
    bad = callback_error(state, got)
    if bad:
        return {'ok': False, 'retryable': False, 'code': bad, 'redirect': redirect}
    try:
        status, obj = request_json(
            'POST', API + '/2/oauth2/token',
            form={'grant_type': 'authorization_code', 'code': got['code'], 'redirect_uri': redirect,
                  'code_verifier': verifier, 'client_id': client_id},
            basic=basic_auth(client_id, client_secret), timeout=20)
    except NetError:
        return {'ok': False, 'retryable': False, 'code': 'token_exchange', 'redirect': redirect}
    tok = parse_token(obj) if status == 200 else None
    if not tok or not tok.get('refresh_token'):
        return {'ok': False, 'retryable': False, 'code': 'token_exchange', 'redirect': redirect}
    try:
        bins = rk.trusted_bins()
        rk.store('refresh-token', tok['refresh_token'], bins)
        rk.store('client-secret', client_secret, bins)
    except Exception:
        return {'ok': False, 'retryable': False, 'code': 'keychain', 'redirect': redirect}
    try:
        status, me = request_json('GET', API + '/2/users/me', token=tok['access_token'], timeout=20)
    except NetError:
        return {'ok': False, 'retryable': False, 'code': 'users_me', 'redirect': redirect}
    user_id = ''
    if status == 200 and isinstance(me, dict):
        data = me.get('data') if isinstance(me.get('data'), dict) else me
        user_id = __import__('re').sub(r'[^0-9]', '', str((data or {}).get('id') or ''))[:30]
    if not user_id:
        return {'ok': False, 'retryable': False, 'code': 'users_me', 'redirect': redirect}
    try:
        status, keys = request_json('GET', API + '/2/users/%s/public_keys' % user_id, token=tok['access_token'], timeout=20)
    except NetError:
        return {'ok': False, 'retryable': False, 'code': 'public_keys', 'redirect': redirect}
    row = latest_public_key(keys) if status == 200 else None
    if not row:
        return {'ok': False, 'retryable': False, 'code': 'public_keys', 'redirect': redirect}
    version = str(row.get('public_key_version') or '')
    if not __import__('re').match(r'^[A-Za-z0-9._:-]{1,40}$', version):
        return {'ok': False, 'retryable': False, 'code': 'public_keys', 'redirect': redirect}
    juice = row.get('juicebox_config')
    pin = prompt_secret('X Chat PIN (hidden): ')
    if not pin:
        return {'ok': False, 'retryable': False, 'code': 'pin_required', 'redirect': redirect}
    blob = None
    try:
        try:
            chat = _open_chat(juice)
        except ImportError:
            return {'ok': False, 'retryable': False, 'code': 'no_chatxdk', 'redirect': redirect}
        chat.unlock(pin)
        blob = chat.export_keys()
    except Exception as e:
        out = {'ok': False, 'retryable': False, 'code': 'unlock_failed', 'redirect': redirect}
        try:
            from chat_xdk import guesses_remaining
            n = guesses_remaining(e)
            if isinstance(n, int):
                out['guessesRemaining'] = n
        except Exception:
            pass
        return out
    finally:
        pin = ''
    if not blob:
        return {'ok': False, 'retryable': False, 'code': 'unlock_failed', 'redirect': redirect}
    try:
        b64 = base64.b64encode(bytes(blob)).decode('ascii')
        rk.store('private-key', b64, rk.trusted_bins())
    except Exception:
        return {'ok': False, 'retryable': False, 'code': 'keychain', 'redirect': redirect}
    finally:
        _wipe(blob)
    _save_cfg(xpostClientId=client_id, xpostUserId=user_id, xpostKeyVersion=version, xpostRedirectPort=port)
    return {'ok': True, 'retryable': False, 'code': 'ok', 'userIdSet': True, 'keySaved': True, 'redirect': redirect}


def cmd_post(req):
    if not isinstance(req, dict):
        return _fail('bad_request')
    texts = req.get('texts')
    if not isinstance(texts, list) or not texts:
        return _fail('bad_request')
    clean = []
    for t in texts:
        if not isinstance(t, dict) or 'text' not in t:
            return _fail('bad_request')
        clean.append({'text': str(t.get('text') if t.get('text') is not None else ''),
                      'header': bool(t.get('header')), 'notify': bool(t.get('notify'))})
    return _post_live(clean)


def main(argv=None, stdin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if not argv or argv[0] not in ('setup', 'post'):
            emit({'ok': False, 'retryable': False, 'code': 'usage'})
            return 0
        if argv[0] == 'setup':
            emit(cmd_setup())
            return 0
        src = sys.stdin if stdin is None else stdin
        raw = src.read()
        try:
            req = json.loads(raw or 'null')
        except ValueError:
            emit(_fail('bad_request'))
            return 0
        emit(cmd_post(req))
    except Exception:
        emit(_fail('internal'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
