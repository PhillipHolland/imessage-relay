#!/usr/bin/python3
"""
iMessage Relay v1.4.3: inbound watcher (LaunchAgent com.imessage-relay.watch).

Every `pollSeconds` (default 10) it reads ~/Library/Messages/chat.db READ-ONLY for new rows
with is_from_me = 0 and POSTs
    {"type":"message","contactName","label","handle","text","receivedAt","group"?,"untrusted":true}
to config "webhookUrl" (the bot's webhook routine trigger). The bot posts it into the one
relay chat as "<label>: <text>" (groups: "<group label> · <label>: <text>").
  - relayAll false: only opted-in contacts (config "contacts") in 1:1 chats.
  - relayAll true: every inbound 1:1 iMessage/SMS and group chat messages. Senders who are not
    contacts get a unique label from the macOS Contacts name, else a formatted number or email,
    kept in roster.json (handle, label, lastInboundAt; never text).
  - Never: the owner's own handle, "mute" handles, 5-6 digit short codes (skipShortCodes).
After each successful post the sender's (or group's) roster time is updated; the host uses
the roster to route a bare reply and to allow replies to recent non-contact senders.

  - Needs Full Disk Access. macOS ignores that permission for Apple's Python, so launchd starts
    the small "iMessage Relay Watcher" applet (~/Applications), which runs watch_launcher.py,
    which runs this file. The owner grants Full Disk Access to that app.
  - Text: `message.text`, else decoded from `attributedBody` (typedstream.py), else
    "[attachment]" for attachment-only messages. Reactions/tapbacks are skipped; group chats too unless relayAll.
  - The text of skipped messages is never even selected: the scan reads ROWID, handle and flags
    only, and fetches text by ROWID for rows that will be relayed.
  - On disk: only the last committed ROWID (watch_state.json), roster.json (no text) and a
    counts-only status file.
    Message bodies live in memory only. The log (~/Library/Logs/imessage-relay.log) has counts.
  - paused: nothing is forwarded; new rows are skipped (not queued) until resumed.
  - quietHours: inbound messages are held IN MEMORY and sent as one "digest" POST when quiet
    hours end. The committed ROWID stays behind held rows, so a restart re-reads them from
    chat.db instead of losing them (nothing is written to disk).
  - Messages.app must stay running. If it is not: with "autoReopenMessages": true the watcher
    starts it hidden (open -g -j -a Messages), at most once every 5 minutes, and logs it;
    otherwise it logs (at most every 5 minutes) and posts one body-free status heads-up.
  - Jobs from relayctl (files in ~/.imessage-relay/jobs/) that need chat.db access run here,
    because this process is the one holding Full Disk Access: recent_senders, confirm_delivery.
"""
import glob
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay_common as rc
import relay_setup  # noqa: E402
import relay_contacts  # noqa: E402  (v1.4.0: replies to someone new)
from typedstream import message_text  # noqa: E402

STATE_PATH = os.path.join(rc.APP_DIR, 'watch_state.json')
STATUS_PATH = os.path.join(rc.APP_DIR, 'status.json')
JOBS_DIR = os.path.join(rc.APP_DIR, 'jobs')
NOTIFY_RETRY_DIR = os.path.join(rc.APP_DIR, 'notify_retry')   # v1.4.0: host notifications that failed
NOTIFY_RETRY_MAX_AGE_S = 600
NOTIFY_RETRY_PER_TICK = 10
CONTACTS_RESULT_MAX_AGE_S = 60
ADDRESSBOOK_GLOBS = os.environ.get('IMESSAGE_RELAY_ADDRESSBOOK_GLOB', os.pathsep.join([
    os.path.join(rc.HOME, 'Library', 'Application Support', 'AddressBook', 'Sources', '*', 'AddressBook-v22.abcddb'),
    os.path.join(rc.HOME, 'Library', 'Application Support', 'AddressBook', 'AddressBook-v22.abcddb'),
])).split(os.pathsep)

BATCH = 500
MAX_QUEUE = 200
MAX_POSTS_PER_TICK = 25     # rate-limit: never fire more than this many webhook POSTs in one tick
OLD_MESSAGE_S = 86400       # never relay a message first seen more than 24h after it arrived (big catch-ups)
EMPTY_GRACE_S = 45          # an empty-body row is left uncommitted this long (attributedBody may still be landing)
REBASELINE_GAP = 5000       # a jump larger than this (chat.db rebuilt / long outage) rebaselines with one status
DIGEST_CHUNK = 10          # digest posts are chunked to at most 10 lines (item 10)
SEND_CONFIRM_FALLBACK_S = 20  # item 6: if delivery is still unconfirmed after ~20s, post a plain ✓ sent
REOPEN_EVERY_S = 300
NOT_RUNNING_LOG_EVERY_S = 300
FDA_LOG_EVERY_S = 3600
JOB_MAX_AGE_S = 600
GROUP_STYLE = 43          # chat.style: 43 = group, 45 = 1:1
MESSAGES_NOT_RUNNING_DETAIL = ('Messages is not running on the Mac. New iMessages and replies may stall '
                               'until it is opened again.')
FDA_DETAIL = ("The watcher cannot read chat.db. Full Disk Access is probably missing for the 'iMessage Relay Watcher' "
              "app (~/Applications). Drag it into System Settings > Privacy & Security > Full Disk Access.")
RESYNC_DETAIL = ('The watcher was offline for a while or Messages rebuilt its database, so a large backlog was '
                 'skipped to avoid replaying old messages. New messages from here on relay normally.')
CATCHING_UP_DETAIL = ('A burst of messages is being relayed in batches to stay within rate limits. Nothing is '
                      'dropped; the rest will arrive over the next few minutes.')


post_json = rc.post_json

# A genuine one-time-code context: OTP words, or the classic "(is) your code" / "code is" / "code:"
# shapes. Bare "code" alone is NOT enough (so "zip code", "dress code", "area code" never trigger).
OTP_CONTEXT_RE = re.compile(
    r'(one[\s-]?time|verification|verify|passcode|\botp\b|\b2fa\b|two[\s-]?factor|'
    r'authenticat|security code|access code|confirmation code|login code|log[\s-]?in code|'
    r'sign[\s-]?in code|your code|code is|code:|is your code|use code|enter code|to (?:sign[\s-]?in|log[\s-]?in|verify|confirm|authenticate)|do not share|don\u2019t share|don.t share)', re.I)
# "<word> code" phrases that are never OTPs; used to veto a match even inside an OTP message.
NON_OTP_CODE_RE = re.compile(r'(zip|postal|post|area|dress|promo|coupon|discount|bar|qr|colou?r|dial|country|error|'
                             r'source|dress)\s+code', re.I)
# A code token: a 4-12 digit run (space/dash grouped) OR a 5-8 char alphanumeric token with both a
# letter and a digit (e.g. "AB12CD", "G4X9Q2") so alphanumeric OTPs don't leak.
CODE_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9])([0-9][0-9\s-]{2,}[0-9]|[A-Za-z0-9]{5,8})(?![A-Za-z0-9])')
# A phone number is never an OTP, even inside an OTP-worded message. Veto the usual grouped shapes
# (optionally with a country code) and any digit run right after "call / dial / phone / text" (item 14).
PHONE_TOKEN_RE = re.compile(r'^(?:\+?\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}$')
CALL_CONTEXT_RE = re.compile(r'(?:call|dial|phone|text|fax|tel)\s*$', re.I)


def redact_codes(text):
    """Under relay-all, hide a one-time code so it is not forwarded into X Chat. Redacts only in a real
    OTP context (item 14): 4-12 digit runs and letter+digit alphanumeric codes near an OTP keyword or a
    'your code / code is / code:' shape; never 'zip code 90210', 'dress code 2', a plain 'deposit', or a
    phone number like 'call 800-555-1234'."""
    if not text or not OTP_CONTEXT_RE.search(text):
        return text

    def sub(m):
        tok = m.group(1)
        start = m.start(1)
        # veto when the token is the tail of a non-OTP "<word> code <token>" phrase just before it
        pre = text[max(0, start - 24):start]
        if NON_OTP_CODE_RE.search(pre):
            return tok
        # veto phone numbers (grouped shape, or a run right after "call/dial/phone/...")
        if PHONE_TOKEN_RE.match(tok.strip()) or CALL_CONTEXT_RE.search(pre):
            return tok
        digits = re.sub(r'[^0-9]', '', tok)
        has_alpha = any(c.isalpha() for c in tok)
        if not has_alpha:
            return '[code hidden]' if 4 <= len(digits) <= 12 else tok
        # alphanumeric: redact only a mixed letter+digit token (leaves ordinary words alone)
        if 5 <= len(tok) <= 8 and digits and has_alpha:
            return '[code hidden]'
        return tok
    return CODE_TOKEN_RE.sub(sub, text)


def flatten_inbound(text):
    """Indent every continuation line so an inbound body cannot forge a new "Name:" prefix line in
    the relay chat (the first line still carries the real sender's label)."""
    return re.sub(r'[\r\n]+', '\n\u2002', str(text or ''))


def contacts_names_map(globs=None):
    """Best effort: normalized handle -> display name from the macOS Contacts database
    (needs the same Full Disk Access). Empty dict when unavailable."""
    out = {}
    for pattern in globs or ADDRESSBOOK_GLOBS:
        for path in glob.glob(pattern):
            try:
                con = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=2)
            except sqlite3.Error:
                continue
            try:
                for table, col in (('ZABCDPHONENUMBER', 'ZFULLNUMBER'), ('ZABCDEMAILADDRESS', 'ZADDRESS')):
                    try:
                        rows = con.execute('SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, x.%s FROM %s x '
                                           'JOIN ZABCDRECORD r ON r.Z_PK = x.ZOWNER' % (col, table)).fetchall()
                    except sqlite3.Error:
                        continue
                    for first, last, org, value in rows:
                        name = ' '.join(p for p in (first, last) if p) or (org or '')
                        h = rc.normalize_handle(value)
                        if name and h and h not in out:
                            out[h] = name
            finally:
                con.close()
    return out


_TAPBACK_EMOJI = {0: '\u2764\ufe0f', 1: '\U0001f44d', 2: '\U0001f44e', 3: '\U0001f602',
                  4: '\u203c\ufe0f', 5: '\u2753'}   # love, like, dislike, laugh, emphasis, question


def classify_tapback(assoc, emoji):
    """Map a chat.db associated_message_type to (emoji, removed) for a text-line tapback, or None when it
    is not a tapback. 2000-2999 add, 3000-3999 remove; type ...6 is a custom emoji/sticker reaction and
    takes associated_message_emoji (item 5). The reacted message's own text is never read."""
    try:
        assoc = int(assoc)
    except (TypeError, ValueError):
        return None
    if 2000 <= assoc <= 2999:
        removed, base = False, assoc - 2000
    elif 3000 <= assoc <= 3999:
        removed, base = True, assoc - 3000
    else:
        return None
    if base == 6:
        e = str(emoji or '').strip() or '\U0001f3f7\ufe0f'
    else:
        e = _TAPBACK_EMOJI.get(base)
        if not e:
            return None
    return (e, removed)


def tapback_target(con, assoc_guid):
    """'your message' / 'their message' for the message a tapback points at (via associated_message_guid),
    or 'a message' when it cannot be resolved. Only the is_from_me flag is read, never any text."""
    g = str(assoc_guid or '')
    if '/' in g:
        g = g.rsplit('/', 1)[-1]
    if ':' in g:
        g = g.split(':')[-1]
    if not g:
        return 'a message'
    try:
        row = con.execute('SELECT is_from_me FROM message WHERE guid = ? LIMIT 1', (g,)).fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return 'a message'
    return 'your message' if row[0] else 'their message'


def attachment_placeholder(con, rowid, is_audio):
    """A metadata-only placeholder for an attachment/audio message (item 4): photo / N photos / video /
    sticker / a file's transfer_name / voice message. Reads only names and mime types, never bytes."""
    if is_audio:
        return '\U0001f3a4 Voice message'
    try:
        rows = con.execute('SELECT a.transfer_name, a.mime_type, a.is_sticker FROM attachment a '
                           'JOIN message_attachment_join j ON j.attachment_id = a.ROWID WHERE j.message_id = ?',
                           (rowid,)).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        return '\U0001f4ce Attachment'
    if any(r[2] for r in rows):
        return '\U0001f9f7 Sticker'
    kinds = []
    for name, mime, _ in rows:
        m = str(mime or '').lower()
        if m.startswith('image/'):
            kinds.append(('photo', None))
        elif m.startswith('video/'):
            kinds.append(('video', None))
        elif m.startswith('audio/'):
            kinds.append(('audio', None))
        else:
            kinds.append(('file', name))
    if len(kinds) == 1:
        k, name = kinds[0]
        if k == 'photo':
            return '\U0001f4f7 Photo'
        if k == 'video':
            return '\U0001f3a5 Video'
        if k == 'audio':
            return '\U0001f3a4 Voice message'
        return '\U0001f4ce ' + (str(name)[:60] if name else 'File')
    if all(k[0] == 'photo' for k in kinds):
        return '\U0001f4f7 %d photos' % len(kinds)
    if all(k[0] == 'video' for k in kinds):
        return '\U0001f3a5 %d videos' % len(kinds)
    return '\U0001f4ce %d attachments' % len(kinds)


class Watcher:
    def __init__(self, db_path=None, poster=post_json, runner=subprocess.run, now=time.time,
                 config_loader=rc.load_config, sleeper=time.sleep):
        self.db_path = db_path or rc.CHAT_DB
        self.poster = poster
        self.runner = runner
        self.now = now
        self.load_config = config_loader
        self.sleeper = sleeper
        self.held = []           # quiet-hours messages, memory only
        self.retry = []          # webhook failures, memory only (oldest first)
        self.cursor = None       # highest ROWID scanned (memory)
        self.delivered = set()   # ROWIDs posted this run (guards against re-posting)
        self.last_reopen = -1e18
        self.last_not_running_log = -1e18
        self.not_running_notified = False
        self.last_fda_log = -1e18
        self.fda_notified = False
        self.chat_db_ok_logged = False
        self.backoff_s = 0
        self.backoff_until = 0
        self.last_forward_at = None
        self.messages_running = None
        self._cols = None
        self._chats = {}         # chat ROWID -> {group, key, name}
        self._names = None       # handle -> name from macOS Contacts (memory only)
        self._names_at = -1e18
        self._empty_since = {}    # rowid -> first-seen ts for empty-body rows (grace before skipping)
        self._backpressure_notified = False

    # ----- state -----
    def committed(self):
        pending = [m['rowid'] for m in self.held + self.retry]
        return (min(pending) - 1) if pending else self.cursor

    def save_state(self):
        if self.cursor is not None:
            rc.write_json_atomic(STATE_PATH, {'lastRowid': self.committed(), 'version': rc.VERSION})

    def write_status(self, cfg, **extra):
        st = {'version': rc.VERSION, 'lastTickAt': rc.iso_utc(self.now()), 'paused': cfg['paused'],
              'quiet': rc.in_quiet_hours(cfg['quietHours'], self.now()), 'held': len(self.held),
              'retry': len(self.retry), 'messagesRunning': self.messages_running,
              'autoReopenMessages': cfg['autoReopenMessages'], 'lastForwardAt': self.last_forward_at,
              'webhookConfigured': bool(cfg['webhookUrl']), 'contacts': len(cfg['contacts']),
              'relayAll': cfg['relayAll'], 'muted': len(cfg['mute'])}
        st.update(extra)
        rc.write_json_atomic(STATUS_PATH, st)

    # ----- webhook -----
    def post(self, cfg, payload):
        if not cfg['webhookUrl']:
            return False
        rc.ensure_event_id(payload)   # unique id on every webhook payload so a retried delivery dedupes (item B)
        return bool(self.poster(cfg['webhookUrl'], payload, cfg.get('webhookToken', '')))

    def post_status(self, cfg, status, detail):
        ok = self.post(cfg, {'type': 'status', 'status': status, 'detail': detail, 'at': rc.iso_utc(self.now())})
        rc.log_event('status_posted', status=status, ok=ok)
        return ok

    def retry_notifications(self, cfg):
        """v1.4.0: re-post body-free host notifications (e.g. "Who's this for?") whose webhook POST
        failed or timed out in the host. Same event id, so a POST that actually landed is deduped by the
        routine. Dropped after 10 minutes, or at once while paused / with no webhook."""
        try:
            names = sorted(n for n in os.listdir(NOTIFY_RETRY_DIR) if n.endswith('.json'))
        except FileNotFoundError:
            return 0
        sent = 0
        for name in names[:NOTIFY_RETRY_PER_TICK]:
            path = os.path.join(NOTIFY_RETRY_DIR, name)
            item = rc.read_json(path, {}) or {}
            payload = item.get('payload') if isinstance(item.get('payload'), dict) else None
            stale = time.time() - float(item.get('queuedAt') or 0) > NOTIFY_RETRY_MAX_AGE_S   # host stamps wall time
            if payload is None or stale or cfg['paused'] or not cfg['webhookUrl']:
                rc.log_event('notify_retry_dropped', type=(payload or {}).get('type'), stale=stale)
            elif self.post(cfg, payload):
                rc.log_event('notify_retry', type=payload.get('type'), ok=True)
                sent += 1
            else:
                continue                      # keep it for the next tick
            try:
                os.unlink(path)
            except OSError:
                pass
        return sent

    # ----- Messages.app -----
    def check_messages_app(self, cfg):
        t = self.now()
        running = rc.messages_running(self.runner)
        self.messages_running = running
        if running is not False:
            if running:
                self.not_running_notified = False
            return
        if cfg['autoReopenMessages']:
            if t - self.last_reopen >= REOPEN_EVERY_S:
                self.last_reopen = t
                ok = rc.open_messages_hidden(self.runner)
                rc.log_event('messages_reopened', ok=ok)
            return
        if t - self.last_not_running_log >= NOT_RUNNING_LOG_EVERY_S:
            self.last_not_running_log = t
            rc.log_event('messages_not_running', autoReopen=False)
        if not self.not_running_notified and cfg['webhookUrl']:
            self.not_running_notified = self.post_status(cfg, 'messages_not_running', MESSAGES_NOT_RUNNING_DETAIL)

    # ----- chat.db scan -----
    def columns(self, con):
        if self._cols is None:
            msg = {r[1] for r in con.execute('PRAGMA table_info(message)')}
            chat = {r[1] for r in con.execute('PRAGMA table_info(chat)')}
            self._cols = (msg, chat)
        return self._cols

    def _inbound_backlog(self, con):
        """Count NEW inbound rows past the cursor (is_from_me = 0). Owner's own sends never count
        toward a rebaseline, so a burst of outgoing messages can't drop a real incoming one (P5)."""
        try:
            return con.execute('SELECT COUNT(*) FROM message WHERE ROWID > ? AND is_from_me = 0', (self.cursor,)).fetchone()[0]
        except sqlite3.Error:
            return 0

    def scan(self, con, cfg, counts, capacity):
        """Build the batch of new inbound messages locally and return (messages, new_cursor, capped).
        Durability rules (item: inbound durability):
          - self.cursor is NOT mutated here; the caller advances it to new_cursor only after this
            returns, so an exception can never commit past un-emitted rows.
          - each row is processed under its own try/except, so one unparseable row is skipped, not the
            whole batch.
          - a very recent empty-body row is deferred (left for a later tick): attributedBody may still
            be landing. new_cursor does not advance past it.
          - backpressure: when the in-memory queue is full we stop scanning and keep the cursor; chat.db
            is the durable queue and the rows are re-read next tick (never dropped)."""
        msg_cols, chat_cols = self.columns(con)
        col = lambda c, d='0': ('m.' + c) if c in msg_cols else d  # noqa: E731
        guid = 'm.guid' if 'guid' in msg_cols else "''"
        q = ('SELECT m.ROWID, m.is_from_me, h.id, m.date, %s, %s, %s, %s, %s, %s, %s, %s, %s, '
             '(SELECT cmj.chat_id FROM chat_message_join cmj WHERE cmj.message_id = m.ROWID LIMIT 1) FROM message m '
             'LEFT JOIN handle h ON h.ROWID = m.handle_id WHERE m.ROWID > ? ORDER BY m.ROWID LIMIT %d'
             % (col('associated_message_type'), col('item_type'), col('cache_has_attachments'), guid,
                col('associated_message_guid', 'NULL'), col('associated_message_emoji', 'NULL'),
                col('is_audio_message'), col('date_edited'), col('date_retracted'), BATCH))
        body_q = 'SELECT text, %s FROM message WHERE ROWID = ?' % ('attributedBody' if 'attributedBody' in msg_cols else 'NULL')
        ctx = {'cc': cfg['defaultCountryCode'], 'mute': set(cfg['mute']), 'roster': rc.load_roster(),
               'roster_changed': False, 'body_q': body_q, 'chat_cols': chat_cols, 'now': self.now()}
        cursor = self.cursor
        out, capped, stop = [], False, False
        for _ in range(40):
            rows = con.execute(q, (cursor,)).fetchall()
            if not rows:
                break
            for row in rows:
                rowid = row[0]
                if len(out) >= capacity:
                    capped = stop = True
                    break
                try:
                    decision = self._decide(con, cfg, counts, row, ctx)
                except sqlite3.OperationalError:
                    counts['dbBusy'] = counts.get('dbBusy', 0) + 1   # chat.db locked: leave this row and the rest for a later tick
                    stop = True
                    break
                except Exception:
                    counts['rowErrors'] += 1
                    decision = ('skip', None)
                if decision[0] == 'defer':       # fresh empty row: leave it and everything after for later
                    stop = True
                    break
                cursor = rowid
                if decision[0] == 'relay':
                    out.append(decision[1])
            if stop or len(rows) < BATCH:
                break
        if ctx['roster_changed']:
            try:
                rc.save_roster(ctx['roster'])
            except Exception:
                counts['rosterWriteErrors'] += 1
        return out, cursor, capped

    def _decide(self, con, cfg, counts, row, ctx):
        """Classify one scanned row. Returns ('skip', None), ('defer', None) or ('relay', message).
        The text of a skipped row is never selected."""
        rowid, from_me, hid, date, assoc, item_type, has_att, guid, assoc_guid, assoc_emoji, is_audio, date_edited, date_retracted, chat_id = row
        counts['scanned'] += 1
        if from_me:
            return ('skip', None)
        if not hid:
            counts['noHandle'] += 1
            return ('skip', None)
        h = rc.normalize_sender(hid, ctx['cc'])
        if h in rc.owner_handle_set(cfg):
            counts['own'] += 1
            return ('skip', None)
        if h in ctx['mute']:
            counts['muted'] += 1
            return ('skip', None)
        if cfg['skipShortCodes'] and rc.is_short_code(hid):
            counts['shortCode'] += 1
            return ('skip', None)
        contact = rc.contact_by_handle(cfg, h)
        if not contact and not cfg['relayAll']:
            counts['notAllowlisted'] += 1
            return ('skip', None)
        chat = self.chat_info(con, chat_id, ctx['chat_cols'])
        if chat['group'] and not cfg['relayAll']:
            counts['group'] += 1
            return ('skip', None)
        if chat['group'] and chat['key'] in ctx['mute']:   # a muted group chat (group:<id>) is silenced (item N4)
            counts['muted'] += 1
            return ('skip', None)
        reaction = None
        if assoc:
            tb = classify_tapback(assoc, assoc_emoji)   # item 5: incoming tapbacks become text lines
            if tb is None:
                counts['reactionsOrEvents'] += 1
                return ('skip', None)
            emoji, removed = tb
            target = tapback_target(con, assoc_guid)
            reaction = ('removed ' if removed else '') + emoji + ' ' + target   # never the reacted row's text
        elif item_type:
            counts['reactionsOrEvents'] += 1
            return ('skip', None)
        if rowid in self.delivered:
            return ('skip', None)
        received = rc.apple_date_to_unix(date)
        if ctx['now'] - received > OLD_MESSAGE_S:      # never relay an old message during a big catch-up
            counts['tooOld'] += 1
            return ('skip', None)
        if reaction is not None:
            body = reaction
        else:
            text, ab = con.execute(ctx['body_q'], (rowid,)).fetchone()
            body = message_text(text, ab, False)        # real text only here; placeholders are added below
            if not body:
                # item 4: metadata-only placeholders (no bytes) for audio / attachments / unsent notices
                if is_audio or has_att:
                    body = attachment_placeholder(con, rowid, bool(is_audio))
                elif date_retracted:
                    body = '\U0001f6ab Unsent a message'
                else:
                    first = self._empty_since.setdefault(rowid, ctx['now'])
                    if ctx['now'] - first < EMPTY_GRACE_S:
                        counts['deferredEmpty'] += 1
                        return ('defer', None)
                    counts['empty'] += 1
                    self._empty_since.pop(rowid, None)
                    return ('skip', None)
            elif date_edited:
                body = body + '  \u270f\ufe0f (edited)'   # new text only; the original is never stored
            self._empty_since.pop(rowid, None)
            if cfg['relayAll'] and cfg.get('redactCodes', True):
                body = redact_codes(body)
            body = flatten_inbound(body)                # a body can't fake a new "Name:" line
        name = contact['name'] if contact else self.name_for(h)
        label, ch = rc.roster_label(cfg, ctx['roster'], h, name)
        ctx['roster_changed'] |= ch
        m = {'rowid': rowid, 'id': str(guid or rowid), 'contactName': name or rc.format_handle(h),
             'label': label, 'handle': h, 'text': body,
             'receivedAt': rc.iso_utc(received), 'route': h}
        if reaction is not None:
            m['reaction'] = True
        if chat['group']:
            glabel, ch = rc.roster_label(cfg, ctx['roster'], chat['key'], chat['name'], group=True)
            ctx['roster_changed'] |= ch
            m.update(group={'label': glabel, 'name': chat['name']}, route=chat['key'])
            counts['groupMessages'] += 1
        return ('relay', m)

    def chat_info(self, con, chat_id, chat_cols):
        """{group, key, name} for a chat ROWID (cached). Group = chat.style 43 or more than one
        other participant."""
        if chat_id in self._chats:
            return self._chats[chat_id]
        info = {'group': False, 'key': '', 'name': ''}
        if chat_id is not None:
            sel = ', '.join(c if c in chat_cols else 'NULL' for c in ('style', 'display_name', 'chat_identifier', 'guid'))
            row = con.execute('SELECT %s FROM chat WHERE ROWID = ?' % sel, (chat_id,)).fetchone()
            members = con.execute('SELECT COUNT(*) FROM chat_handle_join WHERE chat_id = ?', (chat_id,)).fetchone()[0]
            if row:
                style, display, ident, guid = row
                if style == GROUP_STYLE or members > 1:
                    key = re.sub(r'[^A-Za-z0-9;:+@._-]', '', str(ident or guid or chat_id))[:120]
                    info = {'group': True, 'key': 'group:' + key, 'name': str(display or '').strip()[:60]}
        self._chats[chat_id] = info
        return info

    def name_for(self, handle):
        """Display name from the macOS Contacts database (cached; refreshed every 10 minutes, or
        after a minute when an unknown handle shows up)."""
        t = self.now()
        if self._names is None or t - self._names_at > 600 or (handle not in self._names and t - self._names_at > 60):
            try:
                self._names = contacts_names_map()
            except Exception:
                self._names = self._names or {}
            self._names_at = t
        return self._names.get(handle, '')

    # ----- forwarding -----
    @staticmethod
    def route_label(m):
        return m['group']['label'] if m.get('group') else m['label']

    @staticmethod
    def public(m):
        p = {'id': m['id'], 'contactName': m['contactName'], 'label': m['label'], 'handle': m['handle'],
             'text': m['text'], 'receivedAt': m['receivedAt']}
        if m.get('group'):
            p['group'] = dict(m['group'])
        if m.get('reaction'):
            p['reaction'] = True            # item 5: formats as "Alex ❤️ your message" (no colon)
        return p

    # Backpressure replaces the old drop-oldest cap: nothing queued is ever discarded. When the queue
    # is full, scan() stops and leaves the rest in chat.db (the durable queue), to be re-read next tick.

    def _bookkeep(self, m, t, counts):
        """After a message is confirmed delivered to the bot (2xx): routing + echo-guard state. Each
        step is isolated so a disk hiccup never forces a re-POST of a message already delivered."""
        self.delivered.add(m['rowid'])
        # Nit: an incoming tapback (reaction) is relayed as a line but must NOT update last-sender state,
        # so a lone "❤️" can't hijack who a bare reply routes to. Skip roster + echo bookkeeping for it.
        if not m.get('reaction'):
            try:
                rc.record_inbound(m['route'], t, label=self.route_label(m))   # routing: handle (or group) + time only
            except Exception:
                counts['rosterWriteErrors'] += 1
            try:
                rc.record_relayed_text(m['text'], t, handle=m.get('handle', ''))   # echo guard: HMAC + sender only, never the body
            except Exception:
                pass

    def flush(self, cfg, counts):
        t = self.now()
        if t < self.backoff_until:
            return
        failed = False
        posts = 0
        if self.held:                    # quiet hours just ended: one digest (chunked if huge)
            while self.held and posts < MAX_POSTS_PER_TICK:
                chunk = self.held[:DIGEST_CHUNK]
                payload = {'type': 'digest', 'count': len(chunk), 'quietHours': cfg['quietHours'],
                           'messages': [self.public(m) for m in chunk], 'untrusted': True}
                if not self.post(cfg, payload):
                    failed = True
                    break
                self.held = self.held[len(chunk):]      # advance BEFORE bookkeeping: a 2xx is never re-sent
                posts += 1
                counts['digested'] += len(chunk)
                rc.write_digest_marker(t)               # item 7: owner's first bare reply afterward must name a person
                for m in chunk:
                    self._bookkeep(m, t, counts)
        while not failed and self.retry and posts < MAX_POSTS_PER_TICK:
            m = self.retry[0]
            if not self.post(cfg, dict(type='message', untrusted=True, **self.public(m))):
                failed = True
                break
            self.retry.pop(0)                           # remove immediately after 2xx, before bookkeeping
            posts += 1
            counts['forwarded'] += 1
            self._bookkeep(m, t, counts)
        if (self.retry or self.held) and posts >= MAX_POSTS_PER_TICK:
            counts['rateLimited'] = 1
        if failed:
            counts['webhookFailures'] += 1
            self.backoff_s = min(300, max(10, self.backoff_s * 2))
            self.backoff_until = t + self.backoff_s
            if not cfg['webhookUrl']:
                counts['noWebhookUrl'] = 1
        else:
            self.backoff_s = 0
            if counts['forwarded'] or counts['digested']:
                self.last_forward_at = rc.iso_utc(t)

    # ----- one cycle -----
    def tick(self):
        cfg = self.load_config()
        counts = dict.fromkeys(('scanned', 'notAllowlisted', 'group', 'groupMessages', 'own', 'muted', 'shortCode', 'noHandle',
                                'reactionsOrEvents', 'empty', 'deferredEmpty', 'tooOld', 'rowErrors', 'rosterWriteErrors',
                                'new', 'held', 'forwarded', 'digested', 'webhookFailures', 'droppedPaused',
                                'backpressure', 'rateLimited', 'rebaselined', 'dbBusy'), 0)
        self.process_jobs(cfg)
        self.retry_notifications(cfg)
        t = self.now()
        try:
            con = rc.open_chat_db(self.db_path)
        except rc.ChatDbBusy as e:
            # chat.db is readable but momentarily locked/busy: skip this tick, retry next time. Never
            # treat this as missing Full Disk Access, and never post fda_missing (item 11).
            counts['dbBusy'] = counts.get('dbBusy', 0) + 1
            rc.log_event('chat_db_busy', error=str(e)[:200])
            self.write_status(cfg, chatDb='busy', chatDbError=str(e)[:200])
            return counts
        except rc.ChatDbUnavailable as e:
            if t - self.last_fda_log >= FDA_LOG_EVERY_S:
                self.last_fda_log = t
                rc.log_event('chat_db_unavailable', error=str(e)[:200])
            self.chat_db_ok_logged = False
            if not self.fda_notified and not cfg['paused'] and cfg['webhookUrl']:
                self.fda_notified = self.post_status(cfg, 'fda_missing', FDA_DETAIL)
            self.write_status(cfg, chatDb='unavailable', chatDbError=str(e)[:200])
            return counts
        self.fda_notified = False
        if not self.chat_db_ok_logged:
            self.chat_db_ok_logged = True        # once per start / recovery: relayctl status reads it
            self.last_fda_log = -1e18            # so a later failure is logged at once, too
            rc.log_event('chat_db_ok')
            self.auto_detect_owner(con, cfg)
        self.process_pending(cfg)            # verify delivery of host sends (host has no FDA; the watcher does)
        try:
            max_rowid = con.execute('SELECT COALESCE(MAX(ROWID), 0) FROM message').fetchone()[0]
            if self.cursor is None:
                last = (rc.read_json(STATE_PATH, {}) or {}).get('lastRowid')
                if not isinstance(last, int) or last > max_rowid:
                    last = max_rowid            # first run (or a rebuilt chat.db): never replay history
                    rc.log_event('watch_baseline', lastRowid=last)
                self.cursor = last
            new = []
            capped = False
            if cfg['paused']:
                counts['droppedPaused'] = len(self.held) + len(self.retry)
                self.held, self.retry = [], []
                self.cursor = max_rowid
            elif not self.held and not self.retry and self._inbound_backlog(con) > REBASELINE_GAP:
                self.cursor = max_rowid                # chat.db rebuilt / long outage: rebaseline, don't replay
                counts['rebaselined'] = 1
                rc.log_event('watch_rebaseline', lastRowid=max_rowid)
                if cfg['webhookUrl']:
                    self.post_status(cfg, 'resynced', RESYNC_DETAIL)
            else:
                self.check_messages_app(cfg)
                capacity = max(0, MAX_QUEUE - len(self.held) - len(self.retry))
                new, new_cursor, capped = self.scan(con, cfg, counts, capacity)
                self.cursor = new_cursor               # advance only after the whole scan returns
                counts['new'] = len(new)
                counts['backpressure'] = 1 if capped else 0
        finally:
            con.close()
        if not cfg['paused']:
            if rc.in_quiet_hours(cfg['quietHours'], t):
                self.held = sorted(self.held + self.retry + new, key=lambda m: m['rowid'])
                self.retry = []
                counts['held'] = len(new)
            else:
                self.retry.extend(new)
                self.flush(cfg, counts)
            if capped and not self._backpressure_notified and cfg['webhookUrl'] and not rc.in_quiet_hours(cfg['quietHours'], t):
                self._backpressure_notified = self.post_status(cfg, 'catching_up', CATCHING_UP_DETAIL)
            if not self.retry and not self.held:
                self._backpressure_notified = False
        self.save_state()
        self.write_status(cfg, chatDb='ok')
        if any(v for k, v in counts.items() if k not in ('scanned', 'notAllowlisted', 'own', 'noHandle')):
            rc.log_event('tick', queuedHeld=len(self.held), queuedRetry=len(self.retry), **counts)
        return counts

    # ----- delivery proof for host sends (host has no FDA; the watcher does) -----
    def _claimed_path(self):
        return os.path.join(rc.APP_DIR, 'claimed_sends.json')

    def _load_claimed(self, t):
        d = rc.read_json(self._claimed_path(), {}) or {}
        return {int(k): float(v) for k, v in d.items() if t - float(v) <= rc.PENDING_TTL_S * 3}

    def _save_claimed(self, claimed):
        try:
            rc.write_json_atomic(self._claimed_path(), {str(k): v for k, v in claimed.items()}, mode=0o600)
        except Exception:
            pass

    def has_pending(self):
        try:
            return any(n.endswith('.json') for n in os.listdir(rc.PENDING_DIR))
        except OSError:
            return False

    def process_pending(self, cfg):
        """The host (run under Chrome, no chat.db access) left body-free pending records of its sends;
        the watcher checks chat.db for is_delivered / error, logs a counts-only 'delivery' event, and
        notifies the webhook only when a send failed. Unconfirmed records expire after ~10 min."""
        try:
            pending = rc.load_pending()
        except Exception:
            return
        if not pending:
            return
        t = self.now()
        resolved = self._load_claimed(t)         # rowids of ALREADY-RESOLVED sends (persisted, TTL)
        tick_claimed = set(resolved)             # within this tick, also avoid two records grabbing one row
        changed = False

        def confirm(e, delivered):
            if cfg.get('confirmSends', True) and not cfg['paused'] and cfg['webhookUrl']:
                p = {'type': 'sent', 'label': e.get('label', ''), 'delivered': delivered, 'dryRun': False, 'at': rc.iso_utc(t)}
                if e.get('note'):
                    p['note'] = e['note']             # v1.4.1: "(new contact, •••0188)"
                self.post(cfg, p)

        def fail(e, error=None, reason=None, superseded=False):
            if not cfg['paused'] and cfg['webhookUrl']:
                p = {'type': 'send_failed', 'label': e.get('label', ''), 'at': rc.iso_utc(t)}
                if error is not None:
                    p['error'] = error
                if reason:
                    p['reason'] = reason
                if superseded:
                    p['superseded'] = True   # a soft "✓ sent" already went out; this line corrects it
                self.post(cfg, p)

        for e in sorted(pending, key=lambda e: float(e.get('at') or 0)):   # oldest send first
            age = t - float(e.get('at') or 0)
            # item 8: match only chat.db rows at/after the send time (no 30s lookback, so a message the
            # owner sent from another device just before never counts), excluding rows already matched.
            status, error, rowid = rc.check_delivery_once(
                e['handle'], float(e['at']), db_path=self.db_path,
                exclude_rowids=tick_claimed, text_length=e.get('textLength'))
            if rowid is not None:
                tick_claimed.add(rowid)            # same-tick collision guard (even for a still-pending match)
            if status == 'delivered':
                rc.log_event('delivery', status='delivered')
                if not e.get('softSent'):          # W3: the ✓ sent soft-confirmation already went out; don't double-post
                    confirm(e, True)
                rc.remove_pending(e)
            elif status == 'failed':
                # W3: a failure that arrives later still corrects an earlier ✓ sent with a send_failed line.
                rc.log_event('delivery', status='failed', error=error)
                fail(e, error=error, superseded=bool(e.get('softSent')))
                rc.remove_pending(e)
            elif age > rc.PENDING_TTL_S:
                # W3: the 10-minute timeout. If a matching row was ever seen we already soft-confirmed it;
                # if NO row ever appeared, the send likely never happened -> say so, do not claim ✓ sent.
                if e.get('softSent'):
                    rc.log_event('delivery', status='expired')
                else:
                    rc.log_event('delivery', status='unconfirmed')
                    fail(e, reason='unconfirmed')
                rc.remove_pending(e)
            elif (status == 'pending' and rowid is not None and age >= SEND_CONFIRM_FALLBACK_S
                  and not e.get('softSent')):
                # W3: a matching chat.db row exists but delivery is still unconfirmed ~20s on -> post the
                # soft "✓ sent" ONCE and KEEP the record, so a later delivered/failed can still correct it.
                rc.log_event('delivery', status='soft_sent')
                confirm(e, False)
                rc.update_pending(e, softSent=True)
                continue                           # keep waiting; nothing resolved yet
            else:
                continue                           # no matching row yet, or within the 20s window: wait silently
            if rowid is not None:                  # only a RESOLVED row is remembered, so it is never
                resolved[rowid] = t                # rematched to a different send on a later tick
                changed = True
        if changed:
            self._save_claimed(resolved)

    # ----- v1.3.8 guided setup: the owner's own handles, detected once Full Disk Access works -----
    def auto_detect_owner(self, con, cfg):
        """Only when no owner handle is set yet (an explicit choice is never overridden). Handles only."""
        if cfg.get('ownerHandles') or cfg.get('ownerHandle'):
            return None
        try:
            found = relay_setup.detect_owner_handles(con, cfg['defaultCountryCode'])
            res = relay_setup.merge_owner_handles(found, only_if_empty=True)
        except Exception as e:
            rc.log_event('owner_detect_error', error=type(e).__name__)
            return None
        rc.log_event('owner_handles_detected', added=len(res.get('added') or []),
                     skippedContacts=len(res.get('skippedContacts') or []), relayAllEnabled=bool(res.get('relayAllEnabled')))
        return res

    # ----- jobs (relayctl -> watcher, results written next to the job) -----
    def process_jobs(self, cfg):
        try:
            names = sorted(os.listdir(JOBS_DIR))
        except FileNotFoundError:
            return 0
        n = 0
        for name in names:
            if name.startswith('contacts') and name.endswith('.result.json'):
                p = os.path.join(JOBS_DIR, name)   # an unread Contacts answer (host timed out): never keep it
                try:
                    if self.now() - os.path.getmtime(p) > CONTACTS_RESULT_MAX_AGE_S:
                        os.unlink(p)
                except OSError:
                    pass
            if not name.endswith('.job.json'):
                continue
            path = os.path.join(JOBS_DIR, name)
            job = rc.read_json(path, {}) or {}
            os.unlink(path)
            jid = str(job.get('id') or name[:-9])
            if self.now() - float(job.get('createdAt') or 0) > JOB_MAX_AGE_S:
                continue
            try:
                res = self.run_job(job, cfg)
            except Exception as e:
                res = {'ok': False, 'error': str(e)[:200]}
            res['id'] = jid
            rc.write_json_atomic(os.path.join(JOBS_DIR, jid + '.result.json'), res)
            rc.log_event('job_done', type=job.get('type'), ok=res.get('ok'))
            n += 1
        return n

    def run_job(self, job, cfg):
        typ = job.get('type')
        if typ == 'confirm_delivery':                 # poll check_delivery_once (blocking helper removed in v1.3)
            handle, since = job.get('handle', ''), float(job.get('since') or 0)
            attempts = max(1, int(job.get('attempts') or 10))
            d, note = None, 'pending'
            for i in range(attempts):
                status, error, _ = rc.check_delivery_once(handle, since, db_path=self.db_path)
                if status == 'delivered':
                    d, note = True, 'delivered'
                    break
                if status == 'failed':
                    d, note = False, rc.error_message(error)
                    break
                if status == 'unavailable':
                    note = 'chat.db unavailable'
                    break
                if i < attempts - 1:
                    self.sleeper(1)
            return {'ok': d is not None, 'delivered': d, 'note': note}
        if typ == 'recent_senders':
            return self.recent_senders(cfg, int(job.get('days') or 30), int(job.get('limit') or 20))
        if typ == 'test_probe':                       # v1.3.8 /test: am I alive, can I read chat.db?
            return self.test_probe()
        if typ == 'contacts_lookup':                  # v1.4.0: host asks who "Jordan Rivera" is (no FDA there)
            if isinstance(job.get('names'), list):     # v1.4.2: several candidate names, typo matches allowed per name
                names = [str(n or '')[:80] for n in job['names'][:4]]
                return relay_contacts.lookup_many(names, cfg, globs=ADDRESSBOOK_GLOBS, fuzzy=job.get('fuzzy'))
            return relay_contacts.lookup(str(job.get('name') or '')[:80], cfg, globs=ADDRESSBOOK_GLOBS)
        if typ == 'detect_owner_handles':             # v1.3.8 guided setup (relayctl owner-handles detect)
            return self.detect_owner_job(cfg, bool(job.get('apply')))
        return {'ok': False, 'error': 'unknown job type'}

    def test_probe(self):
        state = 'ok'
        try:
            rc.open_chat_db(self.db_path).close()
        except rc.ChatDbBusy:
            state = 'busy'
        except rc.ChatDbUnavailable:
            state = 'unavailable'
        running = rc.messages_running(self.runner)
        self.messages_running = running
        return {'ok': True, 'alive': True, 'version': rc.VERSION, 'chatDb': state, 'messagesRunning': running}

    def detect_owner_job(self, cfg, apply):
        try:
            con = rc.open_chat_db(self.db_path)
        except rc.ChatDbBusy:
            return {'ok': False, 'error': 'chat.db is busy, try again in a moment'}
        except rc.ChatDbUnavailable as e:
            return {'ok': False, 'error': 'chat.db unavailable (Full Disk Access for the watcher?): ' + str(e)[:120]}
        try:
            found = relay_setup.detect_owner_handles(con, cfg['defaultCountryCode'])
        finally:
            con.close()
        if not apply:
            return {'ok': True, 'detected': found}
        res = relay_setup.merge_owner_handles(found)
        res['detected'] = found
        return res

    def recent_senders(self, cfg, days, limit):
        """Handles that sent you 1:1 iMessages recently, with counts. Never reads message text."""
        try:
            con = rc.open_chat_db(self.db_path)
        except rc.ChatDbBusy as e:
            # chat.db is momentarily locked/busy (readable): ask the caller to retry. Not an FDA problem.
            # (v1.3.8: this branch used an undefined `counts` and surfaced as a NameError.)
            rc.log_event('chat_db_busy', error=str(e)[:200])
            return {'ok': False, 'error': 'chat.db is busy, try again in a moment'}
        except rc.ChatDbUnavailable as e:
            return {'ok': False, 'error': 'chat.db unavailable: ' + str(e)[:150]}
        try:
            _, chat_cols = self.columns(con)
            style_filter = ('AND COALESCE((SELECT c.style FROM chat_message_join cmj JOIN chat c ON c.ROWID = cmj.chat_id '
                            'WHERE cmj.message_id = m.ROWID LIMIT 1), 45) != %d' % GROUP_STYLE) if 'style' in chat_cols else ''
            since = rc.unix_to_apple_ns(self.now() - days * 86400)
            rows = con.execute('SELECT h.id, COUNT(*), MAX(m.date) FROM message m JOIN handle h ON h.ROWID = m.handle_id '
                               'WHERE m.is_from_me = 0 AND m.date > ? %s GROUP BY h.id ORDER BY MAX(m.date) DESC LIMIT ?'
                               % style_filter, (since, limit * 3)).fetchall()
        finally:
            con.close()
        names = contacts_names_map()
        merged = {}
        for hid, count, last in rows:
            h = rc.normalize_handle(hid, cfg['defaultCountryCode'])
            if not rc.valid_handle(h) or h in rc.owner_handle_set(cfg):
                continue
            e = merged.setdefault(h, {'handle': h, 'name': names.get(h, ''), 'count': 0, 'lastAt': 0,
                                      'alreadyContact': bool(rc.contact_by_handle(cfg, h))})
            e['count'] += count
            e['lastAt'] = max(e['lastAt'], rc.apple_date_to_unix(last))
        out = sorted(merged.values(), key=lambda e: -e['lastAt'])[:limit]
        for e in out:
            e['lastAt'] = rc.iso_utc(e['lastAt'])
        return {'ok': True, 'senders': out, 'contactsDb': bool(names)}

    # ----- loop -----
    def run_forever(self):
        stop = {'flag': False}
        signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
        rc.log_event('watch_start', version=rc.VERSION, pid=os.getpid())
        while not stop['flag']:
            cfg = None
            try:
                self.tick()
                cfg = self.load_config()
            except Exception as e:
                rc.log_event('watch_error', error=type(e).__name__)
            for i in range((cfg or rc.DEFAULT_CONFIG)['pollSeconds']):
                if stop['flag']:
                    break
                self.sleeper(1)
                # v1.4.2 latency: while a reply is waiting for its delivery proof, check chat.db every ~2 s
                # instead of once per pollSeconds (was up to 10 s before "\u2713\u2713 delivered" could post).
                if i % 2 == 1 and self.has_pending():
                    try:
                        self.process_pending(self.load_config())
                    except Exception as e:
                        rc.log_event('watch_error', error=type(e).__name__)
                if os.path.isdir(JOBS_DIR) and any(n.endswith('.job.json') for n in os.listdir(JOBS_DIR)):
                    try:
                        self.process_jobs(self.load_config())
                    except Exception as e:
                        rc.log_event('job_error', error=type(e).__name__)
        rc.log_event('watch_stop')


if __name__ == '__main__':
    if '--once' in sys.argv:
        print(json.dumps(Watcher().tick()))
    else:
        Watcher().run_forever()
