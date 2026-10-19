import { VERSION } from '../core.mjs';
export const GET = () => new Response(JSON.stringify({ ok: true, v: VERSION }), {
  headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
});
