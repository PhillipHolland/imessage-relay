"""iMessage Relay v1.4.8 -- delivery-proof queries and Messages.app sending.
Split out of relay_common.py so each file stays under 1000 lines. Always reached through
relay_common (rc.send_imessage, rc.check_delivery_once, rc.add_pending_send, ...); relay_common
re-exports these names lazily. Do not import relay_send before relay_common."""
import os
import re
import subprocess
import sqlite3
import time
import uuid
import relay_common as rc


# Outgoing messages in the ONE-TO-ONE chat with `handle`, earliest first. The participant-count subquery
# keeps group chats out, and date-ascending order lets the watcher claim one distinct ROWID per send.
DELIVERY_SQL = """
SELECT m.ROWID, m.is_delivered, m.error, m.date, LENGTH(COALESCE(m.text, '')),
       COALESCE((SELECT COUNT(*) FROM message_attachment_join j WHERE j.message_id = m.ROWID), 0)
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat_handle_join chj ON chj.chat_id = cmj.chat_id
JOIN handle h ON h.ROWID = chj.handle_id
WHERE m.is_from_me = 1 AND m.date >= ? AND h.id = ?
  AND (SELECT COUNT(*) FROM chat_handle_join x WHERE x.chat_id = cmj.chat_id) = 1
ORDER BY m.date ASC, m.ROWID ASC
"""

# v1.4.0: outgoing messages in ONE group chat, found by its Messages chat GUID (the id the reply was
# sent to). Same columns as DELIVERY_SQL; delivery flags and lengths only, never text.
DELIVERY_SQL_GROUP = """
SELECT m.ROWID, m.is_delivered, m.error, m.date, LENGTH(COALESCE(m.text, '')),
       COALESCE((SELECT COUNT(*) FROM message_attachment_join j WHERE j.message_id = m.ROWID), 0)
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.is_from_me = 1 AND m.date >= ? AND c.guid = ?
ORDER BY m.date ASC, m.ROWID ASC
"""


# When a pending send's text length does NOT match any outgoing row (e.g. an attributedBody-only row,
# where LENGTH(text)=0), we only trust a row that landed within this many seconds of the send. Past it,
# an outgoing row is almost certainly unrelated (the owner texting the same person from their iPhone),
# so we stay pending instead of falsely reporting "delivered" (item 8).
DELIVERY_FALLBACK_WINDOW_S = 4.0


# ---------- delivery proof (watcher only; it holds Full Disk Access) ----------
# One file per pending send, so a concurrent host append and a watcher resolve never lose each other's
# record. Records are body-free: handle, send time, text length and label only -- never the body or a hash.
def add_pending_send(handle, at, text_length, label='', path=None, note='', chat_guid='', x_uuid='', ui=False,
                     part='text', staged_path=''):
    """Host (no chat.db access) records a send for the watcher to verify. NEVER stores the body.
    v1.4.1: `note` is a short body-free tail for the confirmation ("new contact, •••0188").
    v1.4.2: `chat_guid` routes the check to a group chat (handle is then its group:<id> key)."""
    d = path or rc.PENDING_DIR
    h = handle if str(handle).startswith('group:') else rc.normalize_handle(handle)
    rec = {'id': uuid.uuid4().hex, 'handle': h, 'at': float(at),
           'textLength': int(text_length), 'label': rc.clean_label(label)}
    if note:
        rec['note'] = str(note)[:60]
    if chat_guid:
        rec['chatGuid'] = str(chat_guid)
    if x_uuid and re.match(r'^[0-9A-Za-z-]{8,64}$', str(x_uuid)):
        rec['xUuid'] = str(x_uuid)        # v1.4.6: the owner's X message id (for \u2705/\U0001f440/tapback mirrors)
    if part == 'file':
        rec['part'] = 'file'              # v1.4.6: an attachment; proven by an outgoing attachment row
        if staged_path:
            rec['stagedPath'] = str(staged_path)   # the watcher deletes this staged copy once resolved
    if ui:
        rec['ui'] = True                  # the extension will try a \u2705 reaction; the watcher waits for its ack
    rc.write_json_atomic(os.path.join(d, rec['id'] + '.json'), rec, mode=0o600)
    return rec['id']


def load_pending(path=None):
    """All pending-send records, each carrying its own 'id' so a caller removes exactly one."""
    d = path or rc.PENDING_DIR
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith('.json'):
            continue
        r = rc.read_json(os.path.join(d, n))
        if isinstance(r, dict) and r.get('handle'):
            r.setdefault('id', n[:-5])
            out.append(r)
    return out


def update_pending(entry, path=None, **fields):
    """Merge `fields` into one pending record on disk (e.g. mark softSent=True). Body-free, like add."""
    i = entry.get('id') if isinstance(entry, dict) else entry
    if not i:
        return
    fp = os.path.join(path or rc.PENDING_DIR, str(i) + '.json')
    rec = rc.read_json(fp)
    if not isinstance(rec, dict):
        return
    rec.update(fields)
    rc.write_json_atomic(fp, rec, mode=0o600)
    if isinstance(entry, dict):
        entry.update(fields)


def remove_pending(entry, path=None):
    """Delete one pending record by its id (no blanket rewrite, so nothing else is lost)."""
    i = entry.get('id') if isinstance(entry, dict) else entry
    if not i:
        return
    try:
        os.remove(os.path.join(path or rc.PENDING_DIR, str(i) + '.json'))
    except OSError:
        pass


def check_delivery_once(handle, since_unix, db_path=None, exclude_rowids=(), text_length=None, chat_guid=None, part='text'):
    """One non-blocking delivery check for the watcher's tick. Reads only delivery flags, never text.
    Matches the earliest outgoing message in the 1:1 chat with `handle` after `since_unix`, skipping
    ROWIDs already claimed this tick and preferring an exact text-length match on ties. Returns
    (status, error, rowid): status is 'delivered', 'failed', 'pending' (sent, not yet confirmed /
    not found yet) or 'unavailable' (no chat.db / query failed).
    v1.4.1: chat_guid set = a group send, matched in that chat (by its Messages GUID)."""
    try:
        con = rc.open_chat_db(db_path)
    except rc.ChatDbUnavailable:
        return 'unavailable', None, None
    try:
        if chat_guid:
            rows = con.execute(DELIVERY_SQL_GROUP, (rc.unix_to_apple_ns(since_unix), str(chat_guid))).fetchall()
        else:
            rows = con.execute(DELIVERY_SQL, (rc.unix_to_apple_ns(since_unix), rc.normalize_handle(handle))).fetchall()
    except sqlite3.Error:
        return 'unavailable', None, None
    finally:
        con.close()
    rows = [r for r in rows if r[0] not in set(exclude_rowids or ())]
    if part == 'file':
        # v1.4.27: an attachment send is proven only by an attachment row (the join), and only
        # is_delivered deletes the staged file. cache_has_attachments alone is not enough.
        rows = [r for r in rows if len(r) > 5 and r[5]]
        text_length = None
    if not rows:
        return 'pending', None, None
    if text_length is not None:
        exact = [r for r in rows if r[4] == text_length]
        if exact:
            row = exact[0]
        else:
            # No outgoing row whose length matches the relayed text. Do NOT claim delivery from an
            # unrelated row (e.g. the owner's own later iPhone message to the same person). Only trust a
            # row that landed within a short window of the send -- a real failure or the actual relay row
            # shows up right away; anything later stays pending (item 8).
            near = [r for r in rows if 0 <= rc.apple_date_to_unix(r[3]) - since_unix <= DELIVERY_FALLBACK_WINDOW_S]
            if not near:
                return 'pending', None, None
            row = near[0]
    else:
        row = rows[0]
    rowid, is_delivered, error = row[0], row[1], row[2]
    if is_delivered == 1:
        return 'delivered', None, rowid
    if error and int(error) != 0:
        return 'failed', int(error), rowid
    return 'pending', None, rowid


# ---------- Messages.app ----------
# Raw AppleScript stderr can quote the message text, so it is never logged or returned. Every send
# failure is mapped to one of these short, fixed codes, each with one fixed human message.
OSASCRIPT_ERRORS = {
    'messages_busy': 'Messages was busy and did not respond in time.',
    'automation_denied': 'Automation permission for Messages is off (System Settings > Privacy & Security > Automation).',
    'no_imessage': 'That recipient is not reachable (SMS replies are not supported).',
    'timeout': 'Messages did not respond (timed out).',
    'send_failed': 'The reply could not be sent.',
    'attachment_unavailable': "The attachment didn't reach the Mac intact (expired or incomplete). Nothing was sent; attach it again.",
    'attachment_failed': 'Part of the reply was sent, but an attachment failed. Check Messages before resending.',
}


def classify_osascript_error(stderr):
    """Map AppleScript stderr to a fixed error code, without keeping any of its text."""
    s = str(stderr or '')
    if '-1743' in s:
        return 'automation_denied'
    if '-1712' in s:
        return 'messages_busy'
    if '-1728' in s:          # Messages couldn't resolve the participant on iMessage
        return 'no_imessage'
    low = s.lower()
    if 'sms replies not supported' in low or 'service' in low or 'participant' in low or 'buddy' in low:
        return 'no_imessage'
    return 'send_failed'


def error_message(code):
    return OSASCRIPT_ERRORS.get(code, OSASCRIPT_ERRORS['send_failed'])


def build_osascript_cmd(text, handle, service='auto', script_path=None, target='participant', mode='text'):
    """argv list (no shell). Text, handle and service are separate argv items, never spliced into
    source. service: 'iMessage', 'SMS', or 'auto' (iMessage, then SMS via Text Message Forwarding).
    v1.4.1: target 'chat' sends to the group chat whose Messages GUID is `handle` (an extra argv item,
    only added when used)."""
    cmd = [rc.OSASCRIPT, script_path or rc.SCRIPT_PATH, text, handle, service]
    if mode == 'file':
        cmd += ['chat' if target == 'chat' else 'participant', 'file']   # v1.4.6: argv 1 is a staged file path
    elif target == 'chat':
        cmd.append('chat')
    return cmd


def send_imessage(text, handle, runner=subprocess.run, sleeper=time.sleep, timeout=30, service='auto',
                  target='participant', mode='text'):
    """(ok, errcode). errcode is a fixed token from OSASCRIPT_ERRORS (never raw AppleScript text).
    Retries exactly once, and ONLY when a completed osascript actually reported -1712 on stderr
    (Messages busy / first-run Automation prompt). A local process timeout means osascript itself
    hung, so it is NOT retried (re-running would just hang again)."""
    cmd = build_osascript_cmd(text, handle, service, target=target, mode=mode)
    code = 'send_failed'
    for attempt in (1, 2):
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, 'timeout'
        if r.returncode == 0:
            return True, ''
        code = classify_osascript_error(r.stderr or r.stdout or '')
        if attempt == 1 and code == 'messages_busy':
            # -1712 is the first-run Automation prompt / Messages-not-ready case: the send did NOT go
            # through, so nudging Messages and retrying exactly once is safe. Use the patchable helper.
            rc.log_event('retry_after_busy')
            try:
                rc.open_messages_hidden(runner)
            except Exception:
                pass
            sleeper(2)
            continue
        return False, code
    return False, code


def messages_running(runner=subprocess.run):
    try:
        r = runner(['/usr/bin/pgrep', '-x', 'Messages'], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return None


def open_messages_hidden(runner=subprocess.run):
    try:
        r = runner([rc.OPEN, '-g', '-j', '-a', 'Messages'], capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False
