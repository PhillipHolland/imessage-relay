"""iMessage Relay v1.4.8 -- the encrypted photo drop (Mac side, inbound 1:1 photos only).

For each inbound 1:1 photo the watcher has staged (relay_media.stage_image: converted/downsized copy,
mode 600), this module
  1. reads the copy into memory and DELETES the file right away (before any network call),
  2. encrypts it with a fresh random AES-256-GCM key and nonce (macOS CommonCrypto via ctypes; the
     python `cryptography` package where CommonCrypto is absent, e.g. in tests on Linux),
     associated data = "imr-drop-v1:" + token, wire format = nonce(12) || ciphertext || tag(16),
  3. PUTs only that ciphertext to the drop (Authorization: Bearer <put key>), which keeps it at most
     10 minutes and serves it once,
  4. returns webhook metadata {token, kind, mime, ext, size, sha256, url, key} -- no filename, no path.
v1.4.8 (format v2, used whenever dropWrapKey is set): the fresh per-photo key is itself sealed with the
pre-shared wrap key (AES-256-GCM, associated data "imr-drop-v2-key:" + token) and travels INSIDE the drop
blob: wire = 0x02 || wrap nonce(12) || wrapped key(32) || tag(16) || nonce(12) || ciphertext || tag(16),
data associated data "imr-drop-v2:" + token. The webhook then carries only {token, kind, mime, ext, size,
sha256, v: 2}: no key and no URL, so nothing secret or fragile passes through the routine's hands.
The drop still holds ciphertext only (it never has the wrap key) and never sees the plaintext. Nothing about photos is logged except counts, sizes and kinds; never keys, URLs or hashes.
The drop key (dropKey) is only a PUT/DELETE credential: it can't read anything back.
"""
import base64
import ctypes
import hashlib
import os
import secrets
import urllib.error
import urllib.request

AAD_PREFIX = b'imr-drop-v1:'
AAD_V2 = b'imr-drop-v2:'
AAD_V2_KEY = b'imr-drop-v2-key:'
V2_MAGIC = b'\x02'
V2_HEADER = 1 + 12 + 32 + 16                 # magic + wrap nonce + wrapped key + tag
NONCE_LEN = 12
TAG_LEN = 16
KEY_LEN = 32
MAX_PLAIN = 4 * 1024 * 1024 - 64     # the drop's 4 MiB + 28 limit (Vercel's 4.5 MB body) minus the v2 header
PUT_TIMEOUT_S = 20
_CC = None


def _commoncrypto():
    global _CC
    if _CC is None:
        try:
            lib = ctypes.CDLL('/usr/lib/system/libcommonCrypto.dylib')
            for fn in (lib.CCCryptorGCMOneshotEncrypt, lib.CCCryptorGCMOneshotDecrypt):
                fn.restype = ctypes.c_int32
            _CC = lib
        except (OSError, AttributeError):
            _CC = False
    return _CC or None


def gcm_encrypt(key, nonce, plain, aad):
    """ciphertext || tag (AES-256-GCM)."""
    if len(key) != KEY_LEN or len(nonce) != NONCE_LEN:
        raise ValueError('bad key or nonce')
    lib = _commoncrypto()
    if lib:
        out = ctypes.create_string_buffer(max(1, len(plain)))
        tag = ctypes.create_string_buffer(TAG_LEN)
        rc = lib.CCCryptorGCMOneshotEncrypt(ctypes.c_uint32(0), key, ctypes.c_size_t(len(key)), nonce,
                                            ctypes.c_size_t(len(nonce)), aad, ctypes.c_size_t(len(aad)), plain,
                                            ctypes.c_size_t(len(plain)), out, tag, ctypes.c_size_t(TAG_LEN))
        if rc != 0:
            raise ValueError('encrypt failed')
        return out.raw[:len(plain)] + tag.raw
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # tests / non-macOS only
    return AESGCM(key).encrypt(nonce, plain, aad)


def gcm_decrypt(key, nonce, sealed, aad):
    """Inverse of gcm_encrypt (self-checks and tests). Raises ValueError on a bad tag."""
    if len(sealed) < TAG_LEN:
        raise ValueError('short')
    lib = _commoncrypto()
    if lib:
        ct, tag = sealed[:-TAG_LEN], sealed[-TAG_LEN:]
        out = ctypes.create_string_buffer(max(1, len(ct)))
        rc = lib.CCCryptorGCMOneshotDecrypt(ctypes.c_uint32(0), key, ctypes.c_size_t(len(key)), nonce,
                                            ctypes.c_size_t(len(nonce)), aad, ctypes.c_size_t(len(aad)), ct,
                                            ctypes.c_size_t(len(ct)), out, tag, ctypes.c_size_t(TAG_LEN))
        if rc != 0:
            raise ValueError('bad tag')
        return out.raw[:len(ct)]
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    try:
        return AESGCM(key).decrypt(nonce, sealed, aad)
    except InvalidTag:
        raise ValueError('bad tag')


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


def seal(token, plain, rand=secrets.token_bytes):
    """(wire_bytes, key_b64url) for one photo: a fresh key and nonce every time."""
    key, nonce = rand(KEY_LEN), rand(NONCE_LEN)
    return nonce + gcm_encrypt(key, nonce, plain, AAD_PREFIX + token.encode('ascii')), b64u(key)


def _b64u_decode(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def wrap_key(cfg):
    """The 32-byte wrap key (dropWrapKey, base64url) or None."""
    s = str(cfg.get('dropWrapKey') or '')
    if len(s) != 43:
        return None
    try:
        k = _b64u_decode(s)
    except Exception:
        return None
    return k if len(k) == KEY_LEN else None


def seal_v2(token, plain, wkey, rand=secrets.token_bytes):
    """v1.4.8 wire bytes for one photo: a fresh key and nonce every time, the key sealed with the wrap key."""
    key, nonce, wnonce = rand(KEY_LEN), rand(NONCE_LEN), rand(NONCE_LEN)
    tok = token.encode('ascii')
    wrapped = wnonce + gcm_encrypt(wkey, wnonce, key, AAD_V2_KEY + tok)
    return V2_MAGIC + wrapped + nonce + gcm_encrypt(key, nonce, plain, AAD_V2 + tok)


def open_v2(token, wire, wkey):
    """Inverse of seal_v2 (self-checks and tests). Raises ValueError."""
    if len(wire) < V2_HEADER + NONCE_LEN + TAG_LEN or wire[:1] != V2_MAGIC:
        raise ValueError('not v2')
    tok = token.encode('ascii')
    key = gcm_decrypt(wkey, wire[1:13], wire[13:V2_HEADER], AAD_V2_KEY + tok)
    body = wire[V2_HEADER:]
    return gcm_decrypt(key, body[:NONCE_LEN], body[NONCE_LEN:], AAD_V2 + tok)


def configured(cfg):
    url, key = str(cfg.get('dropUrl') or ''), str(cfg.get('dropKey') or '')
    return url.startswith('https://') and len(key) >= 16


def drop_url(cfg, token):
    return str(cfg['dropUrl']).rstrip('/') + '/d/' + token


def _request(method, url, key, body=None, timeout=PUT_TIMEOUT_S):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/octet-stream',
                                          'User-Agent': 'imessage-relay-drop/1.4.8'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def put(cfg, token, wire, requester=_request):
    return requester('PUT', drop_url(cfg, token), str(cfg['dropKey']), wire) == 201


def discard(media, cfg, requester=_request):
    """Best-effort DELETE of uploaded photos that will not be posted (e.g. held spam)."""
    if not configured(cfg):
        return
    for x in media or []:
        tok = str((x or {}).get('token') or '')
        if len(tok) == 32 and ((x or {}).get('url') or (x or {}).get('v') == 2):
            requester('DELETE', drop_url(cfg, tok), str(cfg['dropKey']))


def upload_staged(media, cfg, requester=_request, rand=secrets.token_bytes):
    """Encrypt + PUT each staged photo; its plaintext copy is deleted before the upload starts, on every
    path. Returns (webhook_media, failed_count). webhook_media items: {token, kind, mime, ext, size,
    sha256, url, key} (format v1), or {token, kind, mime, ext, size, sha256, v: 2} with a wrap key."""
    wkey = wrap_key(cfg)
    out, failed = [], 0
    for m in media or []:
        p = str(m.get('macPath') or '')
        try:
            with open(p, 'rb') as fh:
                data = fh.read(MAX_PLAIN + 1)
        except OSError:
            data = None
        finally:
            try:
                os.remove(p)                      # the temp copy is gone right away
            except OSError:
                pass
        if (data is None or len(data) > MAX_PLAIN or len(data) != m.get('size')
                or hashlib.sha256(data).hexdigest() != m.get('sha256')):
            failed += 1
            continue
        try:
            if wkey:
                wire, key = seal_v2(m['token'], data, wkey, rand), None
            else:
                wire, key = seal(m['token'], data, rand)
        except Exception:
            failed += 1
            continue
        finally:
            data = None
        if not put(cfg, m['token'], wire, requester):
            failed += 1
            continue
        item = {'token': m['token'], 'kind': 'image', 'mime': m.get('mime'), 'ext': m.get('ext'),
                'size': m.get('size'), 'sha256': m.get('sha256')}
        if key is None:
            item['v'] = 2                          # the key is inside the drop blob, sealed with the wrap key
        else:
            item.update(url=drop_url(cfg, m['token']), key=key)
        out.append(item)
    return out, failed
