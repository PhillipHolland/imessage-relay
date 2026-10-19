// iMessage Relay v1.4.7 -- encrypted photo drop (shared core for every deploy target).
//
// The Mac encrypts each inbound 1:1 photo with a fresh random AES-256-GCM key and PUTs only the
// ciphertext here; the bot's relay_post.py GETs it once and decrypts it. This service therefore only
// ever holds ciphertext it cannot read: no plaintext, no filenames, no sender, no metadata beyond the
// size and the expiry. Each file is served once (deleted before the GET returns), and deleted after
// TTL_S (10 min) if nobody fetches it. The storage key is a hash of the token, so even the stored
// name does not reveal the URL.
//
// Routes (token = 32 lowercase hex):
//   PUT    /d/<token>   body = ciphertext   Authorization: Bearer <put key>   -> 201 {"ok":true}
//   GET    /d/<token>                       Authorization: Bearer <get key>   -> 200 ciphertext, then gone
//   DELETE /d/<token>                       Authorization: Bearer <put key>   -> 200 {"ok":true}
//   GET    /health                          (no auth, reveals nothing)        -> 200 {"ok":true,"v":"1.4.7"}
// Keys: env DROP_PUT_KEY_SHA256 and DROP_GET_KEY_SHA256 hold the hex sha256 of each per-install key,
// so the deployment never stores the keys themselves. The Mac has only the put key, the bot only the
// get key.
//
// A store adapter implements: put(id, bytes, expiresAtMs) -> true | false (false: already exists);
// take(id, nowMs) -> {bytes, expiresAt} | null, which must DELETE before it returns the bytes (throw if
// the delete fails); del(id); sweep(nowMs) -> number deleted.

export const VERSION = '1.4.7';
export const TTL_S = 600;
export const GCM_OVERHEAD = 12 + 16;                       // nonce + tag
export const MAX_PLAIN = 4 * 1024 * 1024;                  // fits Vercel's 4.5 MB function body limit
export const MAX_BODY = MAX_PLAIN + GCM_OVERHEAD;
const TOKEN_RE = /^[0-9a-f]{32}$/;
const HEX64_RE = /^[0-9a-f]{64}$/;

const enc = new TextEncoder();

export async function sha256hex(s) {
  const d = await crypto.subtle.digest('SHA-256', typeof s === 'string' ? enc.encode(s) : s);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function sameHex(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let x = 0;
  for (let i = 0; i < a.length; i++) x |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return x === 0;
}

export async function storageId(token) {
  return sha256hex('imr-drop-v1:' + token);
}

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' },
  });
}

async function authorized(request, wantHex) {
  if (!HEX64_RE.test(String(wantHex || ''))) return false;   // not configured: refuse everything
  const h = request.headers.get('authorization') || '';
  const m = /^Bearer ([\x21-\x7e]{16,256})$/.exec(h);
  if (!m) return false;
  return sameHex(await sha256hex(m[1]), String(wantHex));
}

export function tokenOf(url) {
  const u = new URL(url);
  const q = u.searchParams.get('token');
  if (q !== null) return TOKEN_RE.test(q) ? q : '';
  const m = /^\/d\/([^/]+)\/?$/.exec(u.pathname);
  return m && TOKEN_RE.test(m[1]) ? m[1] : '';
}

async function readBody(request) {
  const len = request.headers.get('content-length');
  if (len !== null && (!/^\d+$/.test(len) || Number(len) > MAX_BODY)) return null;
  const buf = new Uint8Array(await request.arrayBuffer());
  if (buf.length > MAX_BODY || buf.length <= GCM_OVERHEAD) return null;
  return buf;
}

export async function handle(request, { store, env, now = Date.now }) {
  const url = new URL(request.url);
  const method = request.method.toUpperCase();
  if (method === 'GET' && (url.pathname === '/health' || url.pathname === '/api/health')) {
    return json(200, { ok: true, v: VERSION });
  }
  const token = tokenOf(request.url);
  if (!token) return json(404, { ok: false, error: 'not found' });
  const id = await storageId(token);
  try {
    if (method === 'PUT') {
      if (!(await authorized(request, env.DROP_PUT_KEY_SHA256))) return json(401, { ok: false, error: 'unauthorized' });
      const body = await readBody(request);
      if (!body) return json(413, { ok: false, error: 'size' });
      const created = await store.put(id, body, now() + TTL_S * 1000);
      return created ? json(201, { ok: true, ttl: TTL_S }) : json(409, { ok: false, error: 'exists' });
    }
    if (method === 'GET') {
      if (!(await authorized(request, env.DROP_GET_KEY_SHA256))) return json(401, { ok: false, error: 'unauthorized' });
      const got = await store.take(id, now());
      if (!got) return json(404, { ok: false, error: 'not found' });
      if (got.expiresAt && got.expiresAt <= now()) return json(404, { ok: false, error: 'not found' });  // already deleted by take
      return new Response(got.bytes, {
        status: 200,
        headers: { 'content-type': 'application/octet-stream', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' },
      });
    }
    if (method === 'DELETE') {
      if (!(await authorized(request, env.DROP_PUT_KEY_SHA256))) return json(401, { ok: false, error: 'unauthorized' });
      await store.del(id);
      return json(200, { ok: true });
    }
    return json(405, { ok: false, error: 'method' });
  } catch (e) {
    return json(503, { ok: false, error: 'storage' });                // never echo details
  }
}

export async function handleSweep(request, { store, env, now = Date.now }) {
  const want = String(env.CRON_SECRET || '');
  if (want && (request.headers.get('authorization') || '') !== 'Bearer ' + want) {
    return json(401, { ok: false, error: 'unauthorized' });
  }
  try {
    return json(200, { ok: true, deleted: await store.sweep(now()) });
  } catch (e) {
    return json(503, { ok: false, error: 'storage' });
  }
}

// ---------- adapters ----------
// Vercel Blob (private store). blob = {put, get, del, list} from '@vercel/blob' (injected for tests).
// Expiry = uploadedAt + TTL; nothing else is stored with the bytes.
export function blobStore(blob, ttlS = TTL_S) {
  const path = (id) => 'd/' + id + '.bin';
  return {
    async put(id, bytes) {
      try {
        const body = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        await blob.put(path(id), body, { access: 'private', addRandomSuffix: false, allowOverwrite: false,
                                          contentType: 'application/octet-stream' });
        return true;
      } catch (e) {
        if (/already exists/i.test(String(e && e.message))) return false;
        throw e;
      }
    },
    async take(id, nowMs) {
      const r = await blob.get(path(id), { access: 'private', useCache: false });
      if (!r || r.statusCode !== 200 || !r.stream) return null;
      const bytes = new Uint8Array(await new Response(r.stream).arrayBuffer());
      await blob.del(path(id));                            // gone BEFORE the bytes are returned
      const up = r.blob && r.blob.uploadedAt ? new Date(r.blob.uploadedAt).getTime() : nowMs;
      return { bytes, expiresAt: up + ttlS * 1000 };
    },
    async del(id) {
      await blob.del(path(id));
    },
    async sweep(nowMs) {
      let cursor, n = 0;
      do {
        const page = await blob.list({ prefix: 'd/', limit: 1000, cursor });
        const old = (page.blobs || []).filter((b) => nowMs - new Date(b.uploadedAt).getTime() >= ttlS * 1000);
        if (old.length) {
          await blob.del(old.map((b) => b.url || b.pathname));
          n += old.length;
        }
        cursor = page.hasMore ? page.cursor : undefined;
      } while (cursor);
      return n;
    },
  };
}

// Cloudflare Workers KV (UNTESTED, community-only). KV's own expirationTtl deletes every file after
// TTL_S even if nothing ever runs again; the only metadata is the expiry.
export function kvStore(kv, ttlS = TTL_S) {
  return {
    async put(id, bytes, expiresAt) {
      if ((await kv.get(id, 'arrayBuffer')) !== null) return false;
      await kv.put(id, bytes, { expirationTtl: ttlS, metadata: { e: expiresAt } });
      return true;
    },
    async take(id) {
      const r = await kv.getWithMetadata(id, 'arrayBuffer');
      if (!r || r.value === null) return null;
      await kv.delete(id);
      return { bytes: new Uint8Array(r.value), expiresAt: (r.metadata && r.metadata.e) || 0 };
    },
    async del(id) {
      await kv.delete(id);
    },
    async sweep() {
      return 0;                                            // KV expires keys by itself
    },
  };
}
