# iMessage Relay photo drop (v1.4.7)

A tiny service that lets inbound 1:1 photos reach X Chat **without the bot ever touching the Mac**.

- The Mac encrypts each photo with a **fresh random AES-256-GCM key** and PUTs only the ciphertext here.
  The key travels only inside that one webhook body to the bot; this service never sees it.
- It stores **ciphertext only**: no plaintext, no filenames, no sender, no metadata beyond size and expiry.
  The stored name is a hash of the URL token.
- Each file is **served once** (deleted before the response is returned) and **deleted after 10 minutes**
  if nobody fetches it.
- PUT/DELETE need the per-install **put key** (only the Mac has it); GET needs the **get key** (only the
  bot has it). The deployment stores only the sha256 of each key (`DROP_PUT_KEY_SHA256`,
  `DROP_GET_KEY_SHA256`).
- Limit: 4 MiB per photo (Vercel's function body limit is 4.5 MB); the Mac downsizes bigger photos.

Routes: `PUT /d/<token>`, `GET /d/<token>`, `DELETE /d/<token>`, `GET /health`. All the logic is in
`core.mjs`, shared by both targets.

## Vercel (default; what the iMessage Relay Grok Bot deploys)
- Storage: a **private** Vercel Blob store. Deleted on the first GET; a Vercel Cron (`/api/sweep`, every
  minute, authorized with `CRON_SECRET`) deletes anything older than 10 minutes, so an unfetched file
  lives at most ~11 minutes.
- Deploy (from this folder, CLI logged in):
  ```
  vercel project add imessage-relay-drop && vercel link --yes --project imessage-relay-drop
  vercel blob create-store imessage-relay-drop --access private -e production --yes
  vercel env add DROP_PUT_KEY_SHA256 production < put_key.sha256      # values on stdin, never argv
  vercel env add DROP_GET_KEY_SHA256 production < get_key.sha256
  vercel env add CRON_SECRET production --sensitive < cron_secret
  vercel deploy --prod --yes
  ```
  Cron jobs every minute need a Vercel **Pro** team; on Hobby (daily cron only) an unfetched file can
  stay until the daily sweep, so prefer Pro or the Cloudflare target.

## Cloudflare Workers + KV (UNTESTED, community-only)
`cloudflare/worker.mjs` + `cloudflare/wrangler.toml`. KV's own `expirationTtl` (600 s) deletes unfetched
files with no cron. Checked only with `wrangler deploy --dry-run` and `wrangler dev --local`; never deployed
by the maintainers. See the comments in `wrangler.toml` for the three commands.

## Wiring
- Mac: `printf %s "$PUT_KEY" | ~/.imessage-relay/relayctl set-drop --url https://<your-drop> --key-stdin`
  (or pipe it from a file; never argv). `relayctl set-drop --off` turns photos back into "📷 Photo".
- Bot: `~/.imessage-relay-routine/drop.json` (mode 600): `{"url":"https://<your-drop>","get_key":"…"}`.
  relay_post.py only ever fetches from that url.
