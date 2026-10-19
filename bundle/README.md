# iMessage Relay v1.4.5

Get your iMessages in X Chat, and reply to them from X Chat. Needs a Mac that stays on with
Messages signed in, Chrome (or Brave, Edge, Arc), and a Grok Bot with the X plugin connected
for two accounts (yours and a separate "sender" account).

## How it works
```
iMessage from an opted-in contact
  -> chat.db (read-only) -> watcher (LaunchAgent) -> webhook -> your bot
  -> bot posts "Alex: running late" into ONE relay chat (sender <-> you)
Your reply in the relay chat (x.com in Chrome)
  -> extension sees your own bubble -> native host picks the contact -> Messages.app -> iMessage
  -> bot posts "→ Alex ✓"
```
- Every contact shares the one relay chat. A reply goes to the contact of the message you
  replied to (X Chat reply-quote, best effort), else the one named at the start ("Alex: on my
  way", stripped before sending), else whoever wrote last within `replyWindowMinutes` (120).
  If that is unclear (nobody recent, two contacts within `ambiguousSeconds`, a name that fits two
  contacts), nothing is sent and the bot asks "Who's this for? Start with a name, like Alex: …".
- Full names work as the prefix ("Mary Ann Jones: hi"). A name that nobody on the relay list has
  ("Jordan Rivera: hey") is never guessed: the Mac looks it up in your Contacts and asks first
  ("Jordan isn't on your relay list yet. Reply yes within 2 min to text the Jordan Rivera in your
  Contacts (•••0188), or /add Jordan <number>."). `yes` sends "hey" and saves Jordan as a contact.
  Two matches list the names only; a card with several numbers lists them masked.
  v1.4.1: an **exact full name** ("Jordan Rivera: hey", any case) that matches exactly one Contacts
  card with exactly one number or email sends right away, saves the contact and reports
  "→ Jordan Rivera ✓ (new contact, •••0188)". `relayctl set confirmNewContacts always` asks every time
  (default `exact-skip`). `/add Jordan +1 555 555 0188` adds someone directly.
- v1.4.4: X Chat's real native Reply markup is read (the preview block before the message), so a
  quoted reply goes to the quoted person. A quote that still can't be placed never falls back to someone
  not in Contacts, and a bare reply right after a group message asks "Who's this for?" (never the group,
  never an older 1:1 guess). Replies never go to a group.
- v1.4.3: a bare reply is never guessed onto a business (Apple Business Chat), a short code or any
  other non-person handle; you get "Who's this for? Start with a name, like "Alex: …"" instead (quote
  or name them to reply). A "Reply to …?" prompt shows the Contacts name, or the masked number plus
  "⚠️ not in Contacts". Quote diagnostics: each reply sends a text-free outline of the page structure
  around it to the Mac's log (`quote_probe`), and the popup has **Copy page structure**.
- v1.4.2: X Chat's native Reply ("Replied to …" + preview) routes to the quoted person again; a quote
  that can't be placed, or a bare reply to a roster-only last texter (relay-all), asks "Reply to <label>?"
  instead of "Who's this for?". "✓✓ delivered" is checked every ~2 s while a reply is waiting.
- v1.4.2: names in any case ("jordan rivera: hey", "text jordan hey"). A prefix is a name only when your
  relay list or Contacts know it (a nickname or a close typo asks first; a typo is never sent unasked);
  anything else ("Breakfast plan: eggs") is ordinary text. A 👍 on the pending prompt confirms it, 👎 cancels.
- v1.4.1: while the relay is waiting for a confirmation (a reply prompt, `/block`, `/add`), a plain
  `yes` / `y` / `yep` / `ok` / `send` / `do it` confirms and `no` / `cancel` cancels (any case, with
  punctuation or emoji). Those words are never texted to anyone while a prompt is pending, nor within
  10 minutes of one ("Nothing is waiting for a yes …"). With nothing pending, "ok" is an ordinary reply.
- Contacts sharing a first name get distinct labels ("Alex R.", "Alex J."), or set your own
  with `relayctl label`.
- Only contacts you opt in are relayed, unless you turn on **relay-all**
  (`relayctl relay-all on`): then every new inbound text is relayed, labelled from your Mac's
  Contacts (else the formatted number or email), and group texts arrive as
  "Family · Sam: …" (read-only). **Relay-all forwards everything, including verification
  codes**; 5–6 digit short codes are skipped by default (`skipShortCodes`) and `relayctl mute`
  silences any sender. Reactions are always skipped.
- Relay-all keeps `~/.imessage-relay/roster.json` (handle, label, last relayed time; never
  text). Replies can go to anyone on it who texted within `rosterDays` (30), never to a number
  that has never texted in unless you add it as a contact.
- Message bodies are never written to disk by the relay (memory only; logs have counts).
- Dry run is on until you turn it off. Pause stops both directions. Quiet hours hold inbound
  messages and deliver a digest when they end; your replies always send.
- The Messages app must stay running (hidden is fine). Optional: `relayctl auto-reopen on`.

## Limits (v1.4.5)
- Replies are seen in Chrome on the Mac: keep a tab open on the relay chat (replies sent from a
  phone show up there too). Exclude x.com from Chrome Memory Saver.
- One relay chat for all contacts. Reply-quote detection depends on X Chat's markup (best
  effort); the name prefix always works.
- A reply goes to a group only when it starts with the group's exact name (`family: …`) or quotes a
  group message, and only after you answer yes to "Reply to the … group?" (relay-all only). A bare
  reply never goes to a group.
- 🗑️ on a relayed text asks "Block …?"; yes (or 👍, or `/block yes`) mutes that sender here only
  (Messages is not changed). Texts from numbers not in Contacts, or flagged by Messages, show
  "⚠️ Unknown · " first (`relayctl set spamMode off` to stop); nothing is hidden.
  No attachments are sent (incoming ones arrive as "[attachment]").

## Files
- `extension/`: Chrome MV3 extension (content script, service worker, status popup). Fixed
  key, so the ID is always `gpnjllicnleeogdmgpjekgflakdiifee`.
- `mac-helper/`: `imessage_relay_host.py` (native host `com.imessage_relay.host`),
  `imessage_relay_watch.py` (watcher), `watch_launcher.py` (run by the watcher app), `typedstream.py` (attributedBody decoder),
  `relayctl.py` (operations), `relay_common.py`, `send_imessage.applescript`, templates,
  `config.example.json`.
- `install.sh` / `uninstall.sh`: idempotent, per-user, no sudo. Install target `~/.imessage-relay/`
  plus `~/Applications/iMessage Relay Watcher.app`.
- `tests/`, `run_tests.sh`: Python unit tests (Linux-runnable) and jsdom tests.

## Install (on the Mac)
The bot runs the guided setup (SKILL.md, "Onboarding: one-minute guided setup"). By hand, one line:
```
./install.sh --brief && ~/.imessage-relay/relayctl setup --owner-x-handle <you> --sender-x-handle <sender> \
  --owner-x-user-id <id> --relay-conversation-id <id from https://x.com/i/chat/<id>> --relay-all off \
  --webhook-url '<url>' --webhook-key-prompt --open-windows
~/.imessage-relay/relayctl add-contact --name "Alex Rivera" --handle "+1 555 555 0123" [--label Alex]
```
`relayctl setup` never turns off dry run, never sets quiet hours and never adds contacts. Once the
watcher can read chat.db it finds your own iMessage numbers by itself (`relayctl owner-handles detect`).
Then follow the printed steps: drag `~/.imessage-relay/extension` onto chrome://extensions
(Developer mode on), approve the Automation prompt, drag **iMessage Relay Watcher**
(`~/Applications/iMessage Relay Watcher.app`) into Full Disk Access, press Cmd+R
on your x.com/i/chat tab, and type `/test` in the relay chat (one line per step, each with a timeout).

Why a wrapper app: macOS ignores Full Disk Access granted to Apple's Python (Python.app or
python3), so the watcher runs inside a small AppleScript applet. The LaunchAgent starts the
applet, the applet runs `bin/watch_launcher.py`, and that runs the current watcher, so updates
never touch the app or its permission. install.sh builds the app only when it is missing;
`--rebuild-watcher-app` rebuilds it, and then Full Disk Access must be granted again.

## Upgrade
Run the new `./install.sh` (no flags needed). It keeps config.json, roster.json and the existing
watcher app (never rebuilt or re-signed, wherever it lives, so Full Disk Access stays), keeps
the LaunchAgent pointing at that app, and turns an old `~/.imessage-relay/fdatest.py` into a
shim to `bin/watch_launcher.py`. It prints which version it found and upgrades in place. Then
reload the extension card in chrome://extensions (v1.4.5 changes the extension: 🗑️ to block;
v1.4.4, v1.4.3, v1.4.2, v1.4.1, v1.4.0 and v1.3.9 changed it too) and press Cmd+R
on the x.com chat tab.

## Operations
```
relayctl status | pause | resume | dry-run on|off | relay-all on|off
relayctl mute "Bank" | unmute "Bank" | roster [--days 7]
relayctl add-contact … | label … | remove-contact … | contacts | recent-senders | selftest
relayctl setup … | test-result | owner-handles detect [--apply]
```
In the relay chat: `/test` (end-to-end check), `/test send` (also texts your own number), `/help`.

## Config: ~/.imessage-relay/config.json
```json
{
  "paused": false,
  "dryRun": true,
  "quietHours": {"start": "22:00", "end": "07:00", "tz": "local"},
  "relayAll": false,
  "relayConversationId": "<x-conversation-id>",
  "contacts": [
    {"name": "Example Contact", "handle": "+15555550123", "label": "Example"}
  ],
  "replyWindowMinutes": 120,
  "ambiguousSeconds": 60,
  "confirmSends": true,
  "rosterDays": 30,
  "mute": [],
  "skipShortCodes": true,
  "footer": "",
  "deliveryCheck": true,
  "maxSendsPerMinute": 6,
  "webhookUrl": "",
  "webhookToken": "",
  "ownerHandle": "",
  "ownerXHandle": "",
  "senderXHandle": "",
  "autoReopenMessages": false,
  "pollSeconds": 10,
  "defaultCountryCode": "1"
}
```
Edit with `~/.imessage-relay/relayctl` (see `relayctl --help`); every component re-reads it.

## Webhook payloads (Mac -> bot)
- `{"type":"message","contactName","label","handle","text","receivedAt","untrusted":true}`
- `{"type":"digest","count","quietHours","messages":[...],"untrusted":true}`
- `{"type":"sent","label","contactName","dryRun","delivered","routedBy","at"}` (host, after a reply; no body)
- `{"type":"needs_target","detail","reason","at"}` (host, reply NOT sent; no body)
- `{"type":"status","status":"messages_not_running"|"fda_missing","detail","at"}` (no message body)
