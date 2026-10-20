"""iMessage Relay v1.4.8 -- attachment byte paths on the Mac (both directions; 1:1 chats only).

Inbound (iMessage -> X Chat). The watcher holds Full Disk Access, so it is the only process that can
read ~/Library/Messages/Attachments. For a relayed message with photos it:
  1. reads each image (at most MAX_SOURCE_BYTES) and writes a private copy to
     ~/.imessage-relay/outbox/<random token>.<ext>  (dir 0700, file 0600; the token is 128 random bits
     and is the ONLY name: the original filename never leaves chat.db),
  2. converts HEIC/HEIF/TIFF/etc. to JPEG with /usr/bin/sips (on the copy, never the original) and
     downsizes anything over the X Chat 5 MiB image limit,
  3. v1.4.7: hands the copy to relay_drop, which reads it, deletes it at once, encrypts it with a fresh
     AES-256-GCM key and PUTs only the ciphertext to the drop (served once, gone after 10 minutes). The
     webhook carries {token, kind, mime, ext, size, sha256, url, key}; the bot's relay_post.py fetches,
     checks and decrypts it. The routine never touches the Mac. Without a drop (dropUrl/dropKey unset)
     photos are not staged at all and arrive as "📷 Photo". The outbox is still swept after OUTBOX_TTL_S.
Videos, voice messages and other files are not uploaded (the X Chat helper encrypts images only):
they become one-line notices with type and size only ("🎥 Video, 12 MB (not relayed)").

Outbound (X Chat -> iMessage). The extension reads a photo/file the owner attached in the relay chat
and streams it to the native host in chunks (attach_chunk). The host stages the chunks in
~/.imessage-relay/inflight/<uploadId>/ (0700/0600), checks total size + sha256 on send, moves the file
to ~/Pictures/.imessage-relay-out/<token>/ (a folder Messages' sandbox can read) and sends it with
AppleScript. The watcher deletes that copy as soon as chat.db shows the attachment row resolved (or
after the pending-send timeout); the host sweeps anything older than the TTLs.

Privacy: attachment bytes are never stored. Only in-flight temp files (mode 600, dirs 700), deleted right
after use (including on error) or swept after a short TTL. Never logged: filenames, paths, bytes, hashes.
Only counts, kinds and sizes.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time

import relay_common as rc

OUTBOX_DIR = os.path.join(rc.APP_DIR, 'outbox')
INFLIGHT_DIR = os.path.join(rc.APP_DIR, 'inflight')
SEND_STAGE_DIR = os.environ.get('IMESSAGE_RELAY_SEND_STAGE', os.path.join(rc.HOME, 'Pictures', '.imessage-relay-out'))
SIPS = '/usr/bin/sips'

OUTBOX_TTL_S = 10 * 60          # the Mac can't see when the box copied a file: short TTL, swept every tick
INFLIGHT_TTL_S = 10 * 60
SEND_STAGE_TTL_S = rc.PENDING_TTL_S + 5 * 60
MAX_SOURCE_BYTES = 64 * 1024 * 1024          # never read a bigger original
MAX_RELAY_IMAGE_BYTES = 5 * 1024 * 1024      # X Chat (xchat_lite encrypt-media) image limit
MAX_IMAGES_PER_MESSAGE = 10
MAX_OUTBOUND_BYTES = 25 * 1024 * 1024        # one attachment the owner sends from X Chat
MAX_OUTBOUND_FILES = 10
MAX_INFLIGHT_TOTAL = 100 * 1024 * 1024
MAX_CHUNK_BYTES = 640 * 1024                 # decoded bytes per attach_chunk (the extension sends 512 KiB)
ATTACH_GRACE_S = 30                          # a new photo whose file has not landed yet is retried this long

UPLOAD_ID_RE = re.compile(r'^[0-9a-f]{32}$')
TOKEN_RE = re.compile(r'^[0-9a-f]{32}$')
SHA_RE = re.compile(r'^[0-9a-f]{64}$')
EXT_RE = re.compile(r'^[a-z0-9]{1,5}$')

IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'heif', 'tif', 'tiff', 'bmp'}
VIDEO_EXTS = {'mov', 'mp4', 'm4v', 'avi', '3gp'}
AUDIO_EXTS = {'caf', 'm4a', 'mp3', 'aac', 'wav', 'amr', 'aiff'}
_MAGIC = ((b'\xff\xd8\xff', 'image/jpeg', 'jpg'), (b'\x89PNG\r\n\x1a\n', 'image/png', 'png'),
          (b'GIF87a', 'image/gif', 'gif'), (b'GIF89a', 'image/gif', 'gif'))
EXT_MIME = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
            'heic': 'image/heic', 'heif': 'image/heif', 'mov': 'video/quicktime', 'mp4': 'video/mp4',
            'm4v': 'video/x-m4v', 'pdf': 'application/pdf', 'm4a': 'audio/mp4', 'caf': 'audio/x-caf',
            'txt': 'text/plain', 'vcf': 'text/vcard', 'zip': 'application/zip'}


# ---------- small helpers ----------
def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def sniff(data):
    """(mime, ext) of an image the X Chat helper accepts (JPEG/PNG/GIF/WebP), else (None, None)."""
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', 'webp'
    return None, None


def fmt_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ''
    if n <= 0:
        return ''
    if n < 1024 * 1024:
        return '%d KB' % max(1, round(n / 1024.0))
    v = n / (1024.0 * 1024.0)
    return ('%.1f MB' % v) if v < 10 else ('%d MB' % round(v))


def _ext_of(*names):
    for n in names:
        e = os.path.splitext(str(n or ''))[1].lstrip('.').lower()
        if EXT_RE.match(e or ''):
            return e
    return ''


def kind_of(mime, uti, ext, sticker=False):
    m, u = str(mime or '').lower(), str(uti or '').lower()
    if sticker or m.startswith('image/') or ext in IMAGE_EXTS or u in ('public.jpeg', 'public.png', 'public.heic',
                                                                      'public.heif', 'com.compuserve.gif', 'public.tiff'):
        return 'image'
    if m.startswith('video/') or ext in VIDEO_EXTS or 'movie' in u or 'video' in u:
        return 'video'
    if m.startswith('audio/') or ext in AUDIO_EXTS or 'audio' in u:
        return 'audio'
    return 'file'


def sweep(dirpath, ttl_s, now=None):
    """Delete files (and then empty dirs) under dirpath older than ttl_s. Returns how many files."""
    now = time.time() if now is None else now
    n = 0
    if not os.path.isdir(dirpath):
        return 0
    for root, dirs, files in os.walk(dirpath, topdown=False):
        for f in files:
            p = os.path.join(root, f)
            try:
                if now - os.lstat(p).st_mtime > ttl_s:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))          # only succeeds when empty
            except OSError:
                pass
    return n


# ---------- inbound ----------
def attachment_rows(con, rowid):
    """Metadata of a message's attachments (memory only). Never logged. Each row:
    {kind, mime, ext, size, path, sticker}. `path` is the original file (FDA-only)."""
    cols = {r[1] for r in con.execute('PRAGMA table_info(attachment)')}
    sel = ', '.join(('a.' + c) if c in cols else 'NULL' for c in
                    ('filename', 'mime_type', 'uti', 'transfer_name', 'total_bytes', 'is_sticker'))
    try:
        rows = con.execute('SELECT %s FROM attachment a JOIN message_attachment_join j ON j.attachment_id = a.ROWID '
                           'WHERE j.message_id = ? ORDER BY a.ROWID' % sel, (rowid,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for filename, mime, uti, tname, total, sticker in rows:
        ext = _ext_of(tname, filename)
        path = os.path.expanduser(str(filename)) if filename else ''
        try:
            size = int(total or 0)
        except (TypeError, ValueError):
            size = 0
        if path and not size:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        out.append({'kind': kind_of(mime, uti, ext, bool(sticker)), 'mime': str(mime or '')[:60], 'ext': ext,
                    'size': size, 'path': path, 'sticker': bool(sticker)})
    return out


def missing_images(rows):
    """True when some image's file has not been written yet (still downloading)."""
    return any(r['kind'] == 'image' and (not r['path'] or not os.path.isfile(r['path'])) for r in rows)


_KIND_WORD = {'image': 'Photo', 'video': 'Video', 'audio': 'Voice message', 'file': 'File'}


def notice(row, reason):
    """One body-free line for an attachment that is not relayed. Type and size only, never a name.
    reason: 'too_large' | 'unsupported' | 'unavailable' | 'off' | 'digest'. No decorative icon."""
    word = 'Sticker' if row.get('sticker') else _KIND_WORD.get(row.get('kind'), _KIND_WORD['file'])
    if not row.get('sticker') and row.get('kind') == 'file' and row.get('ext'):
        word = row['ext'].upper() + ' file'
    size = fmt_size(row.get('size'))
    sized = word + ((', ' + size) if size and row.get('kind') != 'audio' else '')
    if reason == 'too_large':
        return sized + '. Too large. Open Messages.'
    if reason == 'unsupported':
        return word + '. Open Messages to see it.'
    if reason == 'unavailable':
        return word + '. Couldn\u2019t relay it.'
    return word


def placeholder(rows):
    """Summary placeholder (no names) for photos that end up not uploaded: 'Photo' / '3 photos'."""
    n = len([r for r in rows if r.get('kind') == 'image'])
    if n <= 0:
        return ''
    return 'Photo' if n == 1 else '%d photos' % n


def _convert(src, dst, runner, max_side):
    cmd = [SIPS, '-s', 'format', 'jpeg', '-s', 'formatOptions', '82']
    if max_side:
        cmd += ['-Z', str(int(max_side))]
    cmd += [src, '--out', dst]
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return False
    return getattr(r, 'returncode', 1) == 0 and os.path.isfile(dst)


def stage_image(row, runner=subprocess.run, outbox=None, max_bytes=MAX_RELAY_IMAGE_BYTES):
    """Copy (and if needed convert/downsize) one inbound image into the outbox. Returns
    (public_meta, None) or (None, reason). public_meta has NO filename: {token, kind, mime, ext, size,
    sha256, macPath}. Every intermediate file is removed; only the final token file remains."""
    outbox = _private_dir(outbox or OUTBOX_DIR)
    src = row.get('path') or ''
    try:
        st = os.stat(src)
    except OSError:
        return None, 'unavailable'
    if st.st_size > MAX_SOURCE_BYTES:
        return None, 'too_large'
    token = secrets.token_hex(16)
    work = os.path.join(outbox, '.%s.src' % token)
    conv = os.path.join(outbox, '.%s.jpg' % token)
    try:
        fd = os.open(work, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as inp:
            shutil.copyfileobj(inp, out, 1024 * 1024)
        with open(work, 'rb') as f:
            data = f.read()
        mime, ext = sniff(data)
        if mime and len(data) <= max_bytes:
            final_src = work
        elif mime == 'image/gif':
            return None, 'too_large'             # an animated GIF can't be re-encoded without losing it
        else:
            ok = False
            for side in ((None if not mime else 2048), 2048, 1600, 1200):
                if os.path.exists(conv):
                    os.remove(conv)
                if _convert(work, conv, runner, side) and os.path.getsize(conv) <= max_bytes:
                    ok = True
                    break
            if not ok:
                return None, ('too_large' if mime else 'unsupported')
            with open(conv, 'rb') as f:
                data = f.read()
            mime, ext = sniff(data)
            if not mime:
                return None, 'unsupported'
            final_src = conv
        final = os.path.join(outbox, '%s.%s' % (token, ext))
        os.replace(final_src, final)
        os.chmod(final, 0o600)
        return {'token': token, 'kind': 'image', 'mime': mime, 'ext': ext, 'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'macPath': final}, None
    except OSError:
        return None, 'unavailable'
    finally:
        for p in (work, conv):
            try:
                os.remove(p)
            except OSError:
                pass


def materialize(rows, stage, runner=subprocess.run, outbox=None, max_bytes=MAX_RELAY_IMAGE_BYTES):
    """Turn a message's attachment rows into (staged_images, notices, unstaged_photo_rows).
    stage=False (attachments off, quiet-hours digest, no webhook) gives notices/placeholders only."""
    staged, notices, unstaged = [], [], []
    for r in rows:
        if r['kind'] == 'image' and stage and len(staged) < MAX_IMAGES_PER_MESSAGE:
            meta, why = stage_image(r, runner, outbox, max_bytes)
            if meta:
                staged.append(meta)
            else:
                notices.append(notice(r, why))
        elif r['kind'] == 'image':
            unstaged.append(r)
        else:
            notices.append(notice(r, 'unsupported' if stage else 'off'))
    return staged, notices, unstaged


# ---------- outbound (host side) ----------
def valid_upload_id(u):
    return isinstance(u, str) and bool(UPLOAD_ID_RE.match(u))


def _inflight_total():
    total = 0
    for root, _dirs, files in os.walk(INFLIGHT_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def stage_chunk(upload_id, seq, data_b64, now=None):
    """Append one chunk of an outbound attachment. Chunks must arrive in order (seq 0, 1, ...).
    Returns {'ok':True,'received':bytes} or {'ok':False,'error'}. Nothing about the bytes is logged."""
    now = time.time() if now is None else now
    if not valid_upload_id(upload_id):
        return {'ok': False, 'error': 'bad upload id'}
    try:
        seq = int(seq)
        raw = base64.b64decode(str(data_b64 or ''), validate=True)
    except (TypeError, ValueError, binascii.Error):
        return {'ok': False, 'error': 'bad chunk'}
    if not raw or len(raw) > MAX_CHUNK_BYTES:
        return {'ok': False, 'error': 'bad chunk size'}
    d = _private_dir(os.path.join(_private_dir(INFLIGHT_DIR), upload_id))
    meta_p, data_p = os.path.join(d, 'meta.json'), os.path.join(d, 'data')
    meta = rc.read_json(meta_p, {}) or {}
    nxt = int(meta.get('next') or 0)
    if seq != nxt:
        return {'ok': False, 'error': 'chunk out of order'}
    size = int(meta.get('size') or 0) + len(raw)
    if size > MAX_OUTBOUND_BYTES:
        discard_upload(upload_id)
        return {'ok': False, 'error': 'attachment too large (limit %s)' % fmt_size(MAX_OUTBOUND_BYTES)}
    if seq == 0 and _inflight_total() + len(raw) > MAX_INFLIGHT_TOTAL:
        return {'ok': False, 'error': 'too many attachments in flight'}
    fd = os.open(data_p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'ab') as f:
        f.write(raw)
    rc.write_json_atomic(meta_p, {'next': seq + 1, 'size': size, 'at': now}, mode=0o600)
    return {'ok': True, 'received': size}


def discard_upload(upload_id):
    if valid_upload_id(upload_id):
        shutil.rmtree(os.path.join(INFLIGHT_DIR, upload_id), ignore_errors=True)


_NAME_BAD_RE = re.compile(r'[\x00-\x1f\x7f/\\:"*?<>|]+')


def safe_name(name, mime):
    """A filename for the recipient: the basename without path/control characters, at most 80 chars,
    else 'photo.<ext>' / 'attachment.<ext>'. Never logged."""
    base = os.path.basename(str(name or '').replace('\\', '/'))
    base = _NAME_BAD_RE.sub('_', base).strip(' ._')[:80]
    ext = _ext_of(base)
    if not ext:
        ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp',
               'video/mp4': 'mp4', 'video/quicktime': 'mov', 'application/pdf': 'pdf'}.get(str(mime or '').lower(), '')
    if not base or base.startswith('.'):
        base = 'photo' if str(mime or '').startswith('image/') else 'attachment'
    if ext and not base.lower().endswith('.' + ext):
        base = base + '.' + ext
    return base


def take_upload(upload_id, size, sha256):
    """Verify a fully received upload (size + sha256) and return its data path, else raise ValueError."""
    if not valid_upload_id(upload_id):
        raise ValueError('bad upload id')
    d = os.path.join(INFLIGHT_DIR, upload_id)
    meta = rc.read_json(os.path.join(d, 'meta.json'), {}) or {}
    p = os.path.join(d, 'data')
    try:
        real = os.path.getsize(p)
    except OSError:
        raise ValueError('attachment not received (expired?)')
    if int(meta.get('size') or -1) != real or int(size or -1) != real:
        raise ValueError('attachment size mismatch')
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    if not SHA_RE.match(str(sha256 or '')) or h.hexdigest() != sha256:
        raise ValueError('attachment checksum mismatch')
    return p


def stage_for_send(data_path, name):
    """Move a verified upload to a Messages-readable folder under its recipient-facing name. Returns
    the staged path; the upload's inflight dir is removed."""
    d = os.path.join(_private_dir(SEND_STAGE_DIR), secrets.token_hex(8))
    os.makedirs(d, mode=0o700)
    dst = os.path.join(d, name)
    shutil.move(data_path, dst)
    os.chmod(dst, 0o600)
    shutil.rmtree(os.path.dirname(data_path), ignore_errors=True)
    return dst


def remove_staged(path):
    """Delete a staged outbound file and its token folder (only ever inside SEND_STAGE_DIR)."""
    if not path:
        return False
    root = os.path.realpath(SEND_STAGE_DIR)
    p = os.path.realpath(path)
    if not p.startswith(root + os.sep):
        return False
    try:
        os.remove(p)
    except OSError:
        pass
    try:
        os.rmdir(os.path.dirname(p))
    except OSError:
        pass
    return True


def drop_staged(media):
    """Delete inbound outbox files of a message that will not be posted (e.g. held spam)."""
    root = os.path.realpath(OUTBOX_DIR)
    for x in media or []:
        p = os.path.realpath(str((x or {}).get('macPath') or ''))
        if p.startswith(root + os.sep):
            try:
                os.remove(p)
            except OSError:
                pass


def sweep_all(now=None):
    """Housekeeping for every in-flight area (safe to call often)."""
    return {'outbox': sweep(OUTBOX_DIR, OUTBOX_TTL_S, now), 'inflight': sweep(INFLIGHT_DIR, INFLIGHT_TTL_S, now),
            'sendStage': sweep(SEND_STAGE_DIR, SEND_STAGE_TTL_S, now)}
