# iMessage Relay

Get your iMessages in X Chat, and reply to them from X Chat. iMessages from contacts you opt in
are posted into one X Chat conversation; your replies there are sent back as iMessages through
your own Mac.

## Requirements
- A Mac that stays on, signed in to **Messages** with iMessage (the Messages app kept open).
- **Google Chrome** on that Mac (Brave, Edge and Arc work too), signed in to your X account.
- **Two X accounts**: yours, and a separate "sender" account that posts the relayed texts.
- Apple's Command Line Tools (`xcode-select -p`), for `/usr/bin/python3`.

## Setup
You don't run anything from this repo by hand. The **iMessage Relay Grok Bot** sets it up: it asks
three short questions, downloads `build_bundle.py` from a pinned release, verifies its sha256, and
runs the installer on your Mac (or gives you one line to paste into Terminal).

`build_bundle.py` is the whole Mac-side package in one file (extension, Mac helper, installer,
tests). Running `python3 build_bundle.py OUT_DIR` writes every file and checks each one against an
embedded sha256. The same files are in [`bundle/`](bundle/); [`bundle/README.md`](bundle/README.md)
has the technical details. `tools/` holds the scripts that pack `bundle/` into `build_bundle.py`.

## Privacy model
- **Opt-in contacts.** Only contacts you choose are relayed (an optional "relay-all" mode must be
  turned on explicitly).
- **No message bodies stored on the bot computer.** The bot sees a message only while relaying it
  and keeps nothing of the text; the Mac side never writes message bodies to disk either (memory
  only; logs hold counts).
- **Attachments (v1.4.6).** Attachment bytes are never stored. Only in-flight temp files (mode 600),
  deleted right after use, including on error. Nothing is logged except counts, sizes and types.
- **Photos into X Chat (v1.4.7, v1.4.8).** Each photo is encrypted on your Mac with a fresh key and only the
  ciphertext goes to a small drop service you own (on your own Vercel by default), which serves it once
  and deletes it after 10 minutes. Since v1.4.8 the photo's key is sealed inside that ciphertext with a
  wrap key only your Mac and your bot hold, so the webhook carries no key. The bot never runs commands on your Mac.
- chat.db is opened read-only. Replies start in dry run (logged, not sent) until you go live.
- No telemetry. The relay makes no analytics or phone-home requests.

## Verify a download
Each release lists the sha256 of its assets in the release notes. Check before running:

```sh
curl -fsSL -o build_bundle.py https://github.com/PhillipHolland/imessage-relay/releases/download/v1.4.27/build_bundle.py
shasum -a 256 build_bundle.py     # macOS (on Linux: sha256sum build_bundle.py)
```

The digest must match the release notes exactly. If it doesn't, delete the file and don't run it.

## License
MIT, see [LICENSE](LICENSE).
