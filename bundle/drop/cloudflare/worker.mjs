// UNTESTED, community-only: the same drop on Cloudflare Workers + KV. Phillip's install uses Vercel.
// Bind a KV namespace as DROP_KV and set the secrets DROP_PUT_KEY_SHA256 / DROP_GET_KEY_SHA256
// (wrangler secret put ...). KV's expirationTtl deletes unfetched files after 10 minutes.
import { handle, kvStore } from '../core.mjs';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/api/sweep') return new Response('{"ok":true,"deleted":0}', { headers: { 'content-type': 'application/json' } });
    return handle(request, { store: kvStore(env.DROP_KV), env });
  },
};
