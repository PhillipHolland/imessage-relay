"""Shared test helpers: fresh module state per test, fakes, and a temp chat.db shaped like the real one."""
import importlib
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
MAC = os.path.join(ROOT, 'mac-helper')
if MAC not in sys.path:
    sys.path.insert(0, MAC)

APPLE_EPOCH = 978307200


class Mods:
    pass


def fresh(tmp):
    os.environ['IMESSAGE_RELAY_HOME'] = os.path.join(tmp, 'app')
    os.environ['IMESSAGE_RELAY_LOG'] = os.path.join(tmp, 'imessage-relay.log')
    os.environ['IMESSAGE_RELAY_CHAT_DB'] = os.path.join(tmp, 'chat.db')
    os.environ['IMESSAGE_RELAY_ADDRESSBOOK_GLOB'] = os.path.join(tmp, 'ab', '*.abcddb')
    m = Mods()
    import relay_common, typedstream, relay_test, relay_setup, imessage_relay_host, imessage_relay_watch, relayctl  # noqa: E401
    m.rc = importlib.reload(relay_common)
    m.ts = importlib.reload(typedstream)
    m.rtest = importlib.reload(relay_test)       # v1.3.8 /test
    m.setup = importlib.reload(relay_setup)      # v1.3.8 guided setup
    import relay_contacts
    m.contacts = importlib.reload(relay_contacts)  # v1.4.0 Contacts lookup
    import relay_ui
    m.ui = importlib.reload(relay_ui)             # v1.4.6 UI queue (paths follow IMESSAGE_RELAY_HOME)
    os.environ['IMESSAGE_RELAY_SEND_STAGE'] = os.path.join(tmp, 'send-stage')
    import relay_media
    m.media = importlib.reload(relay_media)       # v1.4.6 attachments (outbox / inflight / send stage)
    m.host = importlib.reload(imessage_relay_host)
    import relay_header, relay_keychain, relay_direct
    m.header = importlib.reload(relay_header)
    m.keychain = importlib.reload(relay_keychain)
    m.direct = importlib.reload(relay_direct)
    m.watch = importlib.reload(imessage_relay_watch)
    m.ctl = importlib.reload(relayctl)
    os.makedirs(os.environ['IMESSAGE_RELAY_HOME'], exist_ok=True)
    return m


def tmpdir():
    d = tempfile.mkdtemp(prefix='imr-test-')
    import atexit, shutil
    atexit.register(shutil.rmtree, d, True)      # v1.4.5: don't leave thousands of dirs in /tmp
    return d


def read_log():
    try:
        with open(os.environ['IMESSAGE_RELAY_LOG'], encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return ''


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeRunner:
    """Stands in for subprocess.run. osascript results are queued (returncode, stderr)."""

    def __init__(self, results=(), messages_running=True):
        self.results = list(results)
        self.calls = []
        self.messages_running = messages_running

    def __call__(self, cmd, **kw):
        assert 'shell' not in kw, 'never use a shell'
        self.calls.append(list(cmd))
        if cmd[0] == '/usr/bin/osascript':
            rc, err = self.results.pop(0)
            return subprocess.CompletedProcess(cmd, rc, '', err)
        if cmd[0] == '/usr/bin/pgrep':
            return subprocess.CompletedProcess(cmd, 0 if self.messages_running else 1, '', '')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    def count(self, prog):
        return len([c for c in self.calls if c[0] == prog])


def encode_len(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x10000:
        return b'\x81' + n.to_bytes(2, 'little')
    return b'\x82' + n.to_bytes(4, 'little')


def attributed_body(text):
    """Bytes shaped like a real chat.db attributedBody (NSAttributedString in typedstream)."""
    raw = text.encode('utf-8')
    return (b'\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00'
            b'\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+' + encode_len(len(raw)) + raw +
            b'\x86\x84\x02iI\x01' + encode_len(len(text)) +
            b'\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d__kIMMessagePartAttributeName'
            b'\x86\x92\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue\x00\x94\x84\x01*\x84\x99\x99\x00\x86\x86\x86')


SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, style INTEGER, chat_identifier TEXT, display_name TEXT);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, text TEXT, attributedBody BLOB,
  handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER, is_from_me INTEGER DEFAULT 0, is_delivered INTEGER DEFAULT 0,
  is_sent INTEGER DEFAULT 0, error INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,
  item_type INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0,
  associated_message_guid TEXT, associated_message_emoji TEXT, is_audio_message INTEGER DEFAULT 0,
  date_edited INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, transfer_name TEXT, mime_type TEXT, is_sticker INTEGER DEFAULT 0);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


class ChatDb:
    def __init__(self, path):
        self.path = path
        con = sqlite3.connect(path)
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        self.handles = {}
        self.chats = {}

    def _con(self):
        return sqlite3.connect(self.path)

    def handle_id(self, con, h):
        if h not in self.handles:
            cur = con.execute('INSERT INTO handle (id, service) VALUES (?, ?)', (h, 'iMessage'))
            self.handles[h] = cur.lastrowid
        return self.handles[h]

    def chat_id(self, con, h, group=False, display_name=None, members=(), style=None):
        """group: False (1:1 chat with h), True (one anonymous group per sender), or a group key
        string shared by several senders. style=45 with members makes a group known only by its
        participant count."""
        key = ('g', group) if isinstance(group, str) else (h, bool(group))
        if key not in self.chats:
            n = len(self.chats) + 1
            ident = ('chat%04d' % n) if group else h
            cur = con.execute('INSERT INTO chat (guid, style, chat_identifier, display_name) VALUES (?, ?, ?, ?)',
                              (('iMessage;+;' if group else 'iMessage;-;') + ident,
                               style if style is not None else (43 if group else 45), ident, display_name))
            self.chats[key] = cur.lastrowid
            for mh in [h] + [x for x in members if x != h]:
                con.execute('INSERT INTO chat_handle_join VALUES (?, ?)', (cur.lastrowid, self.handle_id(con, mh)))
        return self.chats[key]

    def add(self, handle, text=None, ab=None, from_me=0, when=None, group=False, assoc=0, item_type=0,
            attachments=0, delivered=0, error=0, display_name=None, members=(), style=None,
            guid=None, assoc_guid=None, assoc_emoji=None, is_audio=0, date_edited=0, date_retracted=0,
            attach=None):
        """attach: optional dict/list of dicts {transfer_name, mime, is_sticker} to add rows in the
        attachment table joined to this message (sets cache_has_attachments)."""
        import time
        con = self._con()
        hid = self.handle_id(con, handle)
        ns = int(((when or time.time()) - APPLE_EPOCH) * 1e9)
        if attach:
            attachments = 1
        g = guid if guid is not None else ('g%d' % (self._next_guid()))
        cur = con.execute('INSERT INTO message (guid, text, attributedBody, handle_id, service, date, is_from_me, is_delivered, '
                          'is_sent, error, associated_message_type, item_type, cache_has_attachments, '
                          'associated_message_guid, associated_message_emoji, is_audio_message, date_edited, date_retracted) '
                          'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (g, text, ab, hid, 'iMessage', ns, from_me, delivered, from_me, error, assoc, item_type, attachments,
                           assoc_guid, assoc_emoji, is_audio, date_edited, date_retracted))
        mid = cur.lastrowid
        con.execute('INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)', (self.chat_id(con, handle, group, display_name, members, style), mid))
        for a in (attach if isinstance(attach, (list, tuple)) else ([attach] if attach else [])):
            ac = con.execute('INSERT INTO attachment (transfer_name, mime_type, is_sticker) VALUES (?,?,?)',
                             (a.get('transfer_name'), a.get('mime'), 1 if a.get('is_sticker') else 0))
            con.execute('INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)', (mid, ac.lastrowid))
        con.commit()
        con.close()
        return mid

    def _next_guid(self):
        self._guid_n = getattr(self, '_guid_n', 0) + 1
        return self._guid_n
