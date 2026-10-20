#!/usr/bin/python3
"""v1.4.18: Keychain items for the direct poster. Stdlib only.

The `security` CLI has to receive some password on `-w` (it will not read stdin), so the item is
created with a non-secret placeholder and an ACL. The real secret is written afterwards with
SecItemUpdate and never appears in argv. Logs must not include it either.

Service: com.imessage-relay.xpost
Accounts: refresh-token, client-secret, private-key
"""
import os
import subprocess
import sys

SERVICE = 'com.imessage-relay.xpost'
ACCOUNTS = ('refresh-token', 'client-secret', 'private-key')
PLACEHOLDER = 'imr-placeholder'


class KeychainError(Exception):
    pass


def _run(runner, argv, **kw):
    return runner(argv, **kw)


def _check_secret(secret):
    if not isinstance(secret, str) or not secret or '\n' in secret or '\r' in secret:
        raise ValueError('secret must be one line')


def trusted_bins(extra=()):
    """Executables allowed to read the item. `security` itself stays trusted so find -w works."""
    paths = [sys.executable, '/usr/bin/security']
    paths.extend(list(extra or ()))
    out = []
    for p in paths:
        if not p or not isinstance(p, str):
            continue
        for cand in (p,):
            if cand and cand not in out:
                out.append(cand)
        try:
            real = os.path.realpath(p)
        except OSError:
            real = ''
        if real and real not in out:
            out.append(real)
    return out


def store(account, secret, trusted=(), runner=subprocess.run, write_secret=None):
    """Create or replace one item. `secret` is passed only to write_secret, never to argv."""
    if account not in ACCOUNTS:
        raise ValueError('unknown account')
    _check_secret(secret)
    argv = ['security', 'add-generic-password', '-a', account, '-s', SERVICE, '-U']
    for p in trusted_bins(trusted):
        argv.extend(['-T', p])
    argv.extend(['-w', PLACEHOLDER])
    proc = _run(runner, argv)
    if getattr(proc, 'returncode', 1) != 0:
        raise KeychainError('keychain_add_failed')
    writer = write_secret or secitem_update
    try:
        writer(account, secret)
    except Exception:
        delete(account, runner=runner)
        raise
    return True


def exists(account, runner=subprocess.run):
    """True when the item exists. Does not print or return the secret."""
    proc = _run(runner, ['security', 'find-generic-password', '-a', account, '-s', SERVICE])
    return getattr(proc, 'returncode', 1) == 0


def load(account, runner=subprocess.run):
    """Return the secret, or None. Caller must not log the result. Strips one trailing newline."""
    proc = _run(runner, ['security', 'find-generic-password', '-a', account, '-s', SERVICE, '-w'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if getattr(proc, 'returncode', 1) != 0:
        return None
    out = proc.stdout or ''
    if isinstance(out, bytes):
        out = out.decode('utf-8', 'replace')
    if out.endswith('\n'):
        out = out[:-1]
    return out or None


def delete(account, runner=subprocess.run):
    _run(runner, ['security', 'delete-generic-password', '-a', account, '-s', SERVICE],
         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def delete_all(runner=subprocess.run):
    for account in ACCOUNTS:
        try:
            delete(account, runner=runner)
        except Exception:
            pass


def secitem_update(account, secret):
    """Write the real secret into an item the security CLI just created. Darwin only."""
    _check_secret(secret)
    if sys.platform != 'darwin':
        raise KeychainError('keychain_unavailable')
    try:
        import ctypes
        cf = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        sec = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/Security.framework/Security')
    except OSError:
        raise KeychainError('keychain_unavailable')
    utf8 = 0x08000100
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    cf.CFDataCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    sec.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    sec.SecItemUpdate.restype = ctypes.c_int32
    try:
        key_cb = ctypes.c_void_p.in_dll(cf, 'kCFTypeDictionaryKeyCallBacks')
        val_cb = ctypes.c_void_p.in_dll(cf, 'kCFTypeDictionaryValueCallBacks')
    except ValueError:
        raise KeychainError('keychain_unavailable')

    def cstr(s):
        ref = cf.CFStringCreateWithCString(None, s.encode('utf-8'), utf8)
        if not ref:
            raise KeychainError('keychain_unavailable')
        return ref

    owned = []
    buf = None
    try:
        qk = [cstr('class'), cstr('svce'), cstr('acct')]
        qv = [cstr('genp'), cstr(SERVICE), cstr(account)]
        owned.extend(qk + qv)
        raw = secret.encode('utf-8')
        buf = ctypes.create_string_buffer(raw)
        data = cf.CFDataCreate(None, ctypes.cast(buf, ctypes.c_void_p), len(raw))
        if not data:
            raise KeychainError('keychain_unavailable')
        owned.append(data)
        ak = [cstr('v_Data')]
        owned.extend(ak)

        def cfdict(keys, vals):
            n = len(keys)
            ka = (ctypes.c_void_p * n)(*keys)
            va = (ctypes.c_void_p * n)(*vals)
            ref = cf.CFDictionaryCreate(None, ka, va, n, ctypes.cast(key_cb, ctypes.c_void_p), ctypes.cast(val_cb, ctypes.c_void_p))
            if not ref:
                raise KeychainError('keychain_unavailable')
            owned.append(ref)
            return ref

        query = cfdict(qk, qv)
        attrs = cfdict(ak, [data])
        status = sec.SecItemUpdate(query, attrs)
        if status != 0:
            raise KeychainError('keychain_update_failed')
    finally:
        if buf is not None:
            for i in range(len(buf)):
                buf[i] = b'\x00'
        for ref in owned:
            try:
                cf.CFRelease(ref)
            except Exception:
                pass
