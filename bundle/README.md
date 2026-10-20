# iMessage Relay v1.4.19

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
  -> ✅ appears on your own X message (v1.4.6; a text line only if something fails)
```
v1.4.7 brings their photos into X Chat through an **encrypted drop**, with no bot access to the Mac:
- The Mac encrypts each incoming 1:1 photo with a fresh random AES-256-GCM key and uploads only the
  ciphertext to a tiny drop service you own (`drop/`; Vercel by default, Cloudflare community-only). The
  webhook carries just `{token, size, sha256}` (v1.4.8: the photo's key is sealed inside the drop blob
  with a wrap key only the Mac and the bot have, so no key or URL passes through the routine; v1.4.7 sent
  `{token, url, size, sha256, key}`, still accepted); the bot downloads the ciphertext once, checks
  it, decrypts it into a mode-600 temp file, posts it to X Chat and deletes it. The drop holds ciphertext
  only, serves each file once and deletes it after 10 minutes. The Mac's temp copy is deleted before the
  upload starts. Without a drop (`relayctl set-drop` not run), photos arrive as "📷 Photo".
- v1.4.19 (Mac helper + routine relay_post.py; extension stays 1.4.16): a new text is noticed on the
  next 0.25 s step. v1.4.13 ignored the first second after each tick, and Messages rewrites chat.db
  often enough that the wait was ~1 s. The routine key cache now lasts 24 h, so a warm `relay_post.py`
  returns `send` on the first call instead of asking for a key fetch every 6 h. Direct post is unchanged
  and still off. The undo window was already `undoSeconds` (default 5, range 0–10). A shorter routine
  prompt is proposed in `docs/ROUTINE_PROMPT_FAST.md`; the live template is not changed. Replace
  `relay_post.py` on the bot for the longer cache, then re-run `./install.sh` on the Mac (the watcher
  app is not rebuilt).
- v1.4.18 (Mac helper + routine relay_post.py; extension stays 1.4.16): opt-in direct post. With
  `relayctl xpost on`, the Mac encrypts inbound text and posts it to the relay chat in about 1–2 seconds.
  The routine stays the fallback. Default off, so an existing install behaves as v1.4.17 until the owner
  opts in. Photos, reactions, and quiet-hours digests still use the webhook. Needs Python 3.12
  (`brew install python@3.12`, then re-run install.sh; the watcher app is not rebuilt) and relay_post.py
  v1.4.18 on the bot **before** `relayctl xpost on`. Setup: an X app (pay-per-use, user token) with
  redirect `http://127.0.0.1:53682/cb` and scopes `dm.read` `dm.write` `tweet.read` `users.read`
  `offline.access`, then `relayctl xpost setup` (browser login as the sender, then the hidden Chat PIN),
  then `relayctl xpost on`.
- v1.4.17 (Mac helper + routine relay_post.py; extension stays 1.4.16): a new conversation is easy to spot. Before a
  relayed text from a different person than the last one, or the same person after 30 min (`RC set headerGapMinutes N`,
  0 = off), a separate silent line "💬 Alex · 7:20 PM" (label + your local time, no text) is posted; groups use the
  group label. "Name: text" lines are unchanged. Quote-replying to a header asks "Send to Alex?" first; a typed
  header line is refused. The routine keeps only a key hash and a time (header_state.json).
- v1.4.16: the ✅ after a send lands again: v1.4.15 typed the picker search word into X Chat's chat-list
  Search box (it existed before the picker opened), so the full picker never showed ✅ ('no-emoji'). Only a search
  box that appears with the picker is used; quick row, full picker and search are tried in one 40 ms poll (no fixed
  1.2 s waits); twemoji images match by codepoint. Prompts show at once: during the undo window the extension asks
  the host (preflight, never sends) and a pick / Contacts / "reply to …?" prompt no longer waits out undoSeconds;
  a reply that would go out still waits the full window and a 🗑️ still cancels it.
- v1.4.15 (Mac helper only; extension stays 1.4.14): conversational guardrails. Within 10 minutes of any prompt,
  a short reply (up to 4 characters, digits, last 4, emoji only, an email start) is only ever a prompt answer: one
  that matches no choice gets "That isn't one of the choices" and nothing is sent. Text that came from a prompt
  or masked preview row is refused. A last-texter (recent) reply within 10 minutes of a prompt always asks first.
- v1.4.14 (Mac helper + extension): safety fix. X now wraps a bubble's text and its hidden + visible time in one
  span, so a bare "2" was read as "26:31 PM6:31 PM" and could be sent to the last texter. The text is now read
  as typed; a bubble that reads back differently from what was typed is refused (⚠️, nothing sent); and a bare
  number or last 4 digits within 10 minutes of any prompt never goes to the last texter. Install on the Mac,
  then reload the extension and press Cmd+R.
- v1.4.13 (Mac helper + extension): speed + a pick fix. A reply to a numbered pick prompt ("2", the last 4
  digits, an email start) is checked against the pick first and never turns into "Reply to <last texter>?";
  a pick stays answerable 10 minutes. New texts reach X Chat in ~1 s (the watcher wakes on a chat.db change
  instead of waiting up to pollSeconds), Contacts lookups for pick/confirm prompts answer ~1 s sooner, and the
  ✅ sent reaction is polled every 1 s right after a send. Install on the Mac, then reload the extension and
  press Cmd+R.
- v1.4.12 (Mac helper + extension): the Contacts pick prompt is one choice per line ("❓ Text Jordan Rivera at
  which?" / "1 · •••0188 mobile" / … / "Reply 1–4, or no."), phones first (most recently relayed, then
  mobile/iPhone/pref), work/school emails dropped when a better option exists, at most 4. Answer with the number,
  the last 4 digits, or the start of an email. Confirm prompts are typed as 🤖 text right away (no ❓ reaction
  waiting on the emoji picker). Install on the Mac, then reload the extension and press Cmd+R.
- v1.4.11 (Mac helper + extension): several Contacts cards with the same full name are one person (their
  numbers merged and deduped). One number: an exact full name sends and saves, a first name asks "yes". Several:
  a numbered pick prompt (masked numbers, the one used most recently first); reply 1/2/3, or yes / 👍 for 1.
  A full name is never told to "start with the full name". Install on the Mac (no watcher rebuild), then reload
  the extension and press Cmd+R on the X Chat tab.
- v1.4.10 (Chrome extension only; Mac helper stays v1.4.8, bot side v1.4.9): a native Reply to a relayed photo
  (the image bubble or its "Name: 📷 Photo" caption) now reaches the Mac. Emoji-only replies ("❤️❤️❤️", drawn by X as
  emoji images) count as text, the quoted thumbnail is never sent as your photo, a caption quote routes to that name,
  an image-only quote asks ❓, any reply the extension sees but skips gets a ⚠️ on it (never silent), and the
  ✅ "sent" reaction opens X's full emoji picker when the quick bar lacks ✅ (no more "→ Name ✓✓ delivered" text line).
  Reload the extension in chrome://extensions and press Cmd+R on the X Chat tab.
- The Chrome extension did not change in v1.4.7, v1.4.8 or v1.4.9.
- v1.4.9 (bot side only; the Mac code is v1.4.8): a relayed photo always carries the sender's label, even
  right after another text from them, and the routine's upload failures are logged with a short code.
  Photos need the sender's X connection to have the `media.write` permission.
- v1.4.8: every photo run reports body-free `media_status` codes and appends a counts-only line to the
  bot's `media.log`, so a "couldn't be relayed" notice always says why (e.g. `no_key`, `fetch_failed`).

v1.4.6 makes the relay feel native in X Chat:
- **✅ on your reply** as soon as Messages accepts it, placed by the extension in your X tab (seconds, not
  a routine round trip). The "→ Alex ✓" text line is only posted when the reaction can't be placed;
  failures are always posted (⚠️). It becomes **👀** when they read it (1:1 chats with read receipts).
- **Their tapbacks** (❤️ 👍 😂 …) on a reply you sent through the relay show up as real reactions on your
  X message (an id map only: iMessage guid ↔ X message id, 7 days, no text). Others stay text lines.
- **Prompts are reactions**: "Reply to Jordan?" is a ❓ on your own message; answer 👍/👎 or yes/no. Only
  "Who's this for?" is still text, typed by the extension and tagged 🤖. The relay ignores its own ✅ / 👀 /
  ❓ / 🤖 output; only your 👍/👎/🗑️ on relay prompts and your yes/no count.
- **Undo**: 🗑️ on your own message within `undoSeconds` (5, 0–10) cancels it before it goes out.
- **Quiet names**: "Name:" appears only when the sender changes (`quietNamesSeconds`, 600). Replying to an
  unprefixed bubble with X's native Reply still routes to its sender (keyed hashes of the text, no text).
- **Spam follows Messages** (`spamMode follow-messages`, the default): texts Messages files under Junk
  are held (counts only, an hourly "🚫 N spam texts held" line); Unknown Senders show "⚠️ Unknown · ",
  Promotions "🏷️ Promo · ", Transactions "🧾 ". If you turned screening or the spam filter off in Messages,
  the relay does the same; if this Mac can't tell, it falls back to `tag` (v1.4.5). "Reply STOP to opt out"
  footers are dropped from tagged texts. Read-only: Messages settings are never changed.
- **Photos both ways (1:1)**: their photos arrive as images in X Chat; your photos and files in the relay
  chat go out as iMessage attachments, with the same routing and confirmations (a group only after yes).
  Limits: 4 MB per photo into X Chat (v1.4.7 drop; bigger ones are downsized), 25 MB per attachment out, 10 per message;
  anything skipped gets a clear notice. Attachment bytes are never stored. Only in-flight temp files
  (mode 600), deleted right after use, including on error. Nothing is logged except counts, sizes and types.
- **Overnight recap**: quiet hours end with ONE tidy post grouped by person.
- **Shared posts and links**: sharing an X post (or a link card) into the relay chat sends its URL
  (`https://x.com/<user>/status/<id>`), after anything you typed: "Jordan:" + a post texts Jordan the link.
  A card is never mistaken for a reply quote, and its thumbnail is never sent as a photo.
- **Every refusal says why**: a ⚠️ on your message right away, plus a short "⚠️ Not sent: …" line.
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

## Limits (v1.4.19)
- Direct post is opt-in and text-only. Photos, reactions, and quiet-hours digests stay on the webhook.
  It is off until `relayctl xpost on`.
- Replies are seen in Chrome on the Mac: keep a tab open on the relay chat (replies sent from a
  phone show up there too). Exclude x.com from Chrome Memory Saver.
- One relay chat for all contacts. Reply-quote detection depends on X Chat's markup (best
  effort); the name prefix always works.
- A reply goes to a group only when it starts with the group's exact name (`family: …`) or quotes a
  group message, and only after you answer yes to "Reply to the … group?" (relay-all only). A bare
  reply never goes to a group.
- 🗑️ on a relayed text asks "Block …?"; yes (or 👍, or `/block yes`) mutes that sender here only
  (Messages is not changed). Texts from numbers not in Contacts, or flagged by Messages, show
  "⚠️ Unknown · " first (`relayctl set spamMode tag|off`; default `follow-messages`, see above).
- Photos: 1:1 chats only for incoming (group photos stay "📷 Photo"), and only with the photo drop set up
  (`relayctl set-drop`); a photo the drop no longer has (over 10 minutes) gets a "couldn't be relayed" notice; videos, voice notes and files that
  arrive show a type/size notice ("open Messages to see it"). Typing indicators are not relayed (they are
  not in chat.db). Your own tapbacks in X are not sent to iMessage (no supported way without UI scripting).

## Files
- `extension/`: Chrome MV3 extension (content script, service worker, status popup). Fixed
  key, so the ID is always `gpnjllicnleeogdmgpjekgflakdiifee`.
- `mac-helper/`: `imessage_relay_host.py` (native host `com.imessage_relay.host`),
  `imessage_relay_watch.py` (watcher), `watch_launcher.py` (run by the watcher app), `typedstream.py` (attributedBody decoder),
  `relayctl.py` (operations), `relay_common.py`, `relay_header.py`, `relay_direct.py`, `relay_keychain.py`,
  `relay_xpost.py` (v1.4.18 direct poster; runs under `~/.imessage-relay/xpost-venv`, Python 3.12),
  `relay_ui.py` (v1.4.6 reaction queue + id map),
  `relay_media.py` (v1.4.6 in-flight attachments), `relay_drop.py` (v1.4.7 photo encryption + drop upload; v1.4.8 key wrap), `relay_echo.py`, `send_imessage.applescript`, templates,
  `config.example.json`.
- `drop/`: the v1.4.7 encrypted photo drop (`core.mjs` shared; `api/` + `vercel.json` for Vercel,
  `cloudflare/` untested, community-only). Deployed by the bot, never installed on the Mac. See `drop/README.md`.
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
reload the extension card in chrome://extensions **only when coming from v1.4.5 or older** (v1.4.7 and v1.4.8 did not
change it; v1.4.6 changed the extension: ✅/❓/👀 reactions,
🤖 prompts, undo, photos; v1.4.5 changed it too (🗑️ to block); v1.4.4, v1.4.3, v1.4.2, v1.4.1, v1.4.0 and v1.3.9 changed it too) and press Cmd+R
on the x.com chat tab.

## Operations
```
relayctl status | pause | resume | dry-run on|off | relay-all on|off
relayctl mute "Bank" | unmute "Bank" | roster [--days 7]
relayctl add-contact … | label … | remove-contact … | contacts | recent-senders | selftest
relayctl setup … | test-result | owner-handles detect [--apply]
relayctl set-drop --url https://… --key-stdin | set-drop --off      # v1.4.7 photo drop (key on stdin only)
relayctl set-drop --wrap-key-stdin                                  # v1.4.8 photo-key wrap key (stdin only)
relayctl xpost setup | on | off | status | forget                   # v1.4.18 direct post (default off)
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
  "defaultCountryCode": "1",
  "spamMode": "follow-messages",
  "uiReactions": true, "uiPrompts": true, "readReceipts": true, "mirrorTapbacks": true,
  "stripOptOut": true, "relayAttachments": true, "quietNamesSeconds": 600, "undoSeconds": 5,
  "dropUrl": "", "dropKey": "", "dropWrapKey": ""
}
```
Edit with `~/.imessage-relay/relayctl` (see `relayctl --help`); every component re-reads it.

## Webhook payloads (Mac -> bot)
- `{"type":"message","contactName","label","handle","text","receivedAt","untrusted":true}`, plus optional
  `tag`, `unknown`, `quiet`, `reaction` and `media:[{token,kind,mime,ext,size,sha256,v:2}]` (v1.4.8, with a
  wrap key: the ciphertext and its sealed key are on the drop at `<drop url>/d/<token>`) or
  `media:[{token,kind,mime,ext,size,sha256,url,key}]` (v1.4.7 / no wrap key). No name, no path; relay_post.py
  fetches each photo once, only from its own drop.json url
- `{"type":"batch","messages":[...]}` (several texts from one tick)
- `{"type":"digest","count","quietHours","messages":[...],"untrusted":true}`
- `{"type":"sent","label","contactName","dryRun","delivered","routedBy","at"}` (host, after a reply; no body)
- `{"type":"needs_target","detail","reason","at"}` (host, reply NOT sent; no body)
- `{"type":"status","status":"messages_not_running"|"fda_missing","detail","at"}` (no message body)
