// v1.4.7: the encrypted photo drop (shared core + both store adapters with fakes). No network.
const assert = require('assert');
const path = require('path');
const crypto = require('crypto');

let passed = 0;
async function t(name, fn) {
  try { await fn(); passed++; } catch (e) { console.log('FAIL ' + name + ': ' + (e && e.stack || e)); process.exitCode = 1; }
}
const sha = (s) => crypto.createHash('sha256').update(s).digest('hex');
const PUT_KEY = 'p'.repeat(40), GET_KEY = 'g'.repeat(40);
const env = { DROP_PUT_KEY_SHA256: sha(PUT_KEY), DROP_GET_KEY_SHA256: sha(GET_KEY), CRON_SECRET: 'cron-secret-123' };
const TOK = 'ab'.repeat(16);
const base = 'https://drop.example.invalid';

function memStore() {
  const m = new Map();
  return {
    m,
    async put(id, b, e) { if (m.has(id)) return false; m.set(id, { b: new Uint8Array(b), e }); return true; },
    async take(id) { const v = m.get(id); if (!v) return null; m.delete(id); return { bytes: v.b, expiresAt: v.e }; },
    async del(id) { m.delete(id); },
    async sweep(now) { let n = 0; for (const [k, v] of m) if (v.e <= now) { m.delete(k); n++; } return n; },
  };
}
const req = (method, p, key, body, extra = {}) => new Request(base + p, {
  method, body, headers: Object.assign(key ? { authorization: 'Bearer ' + key } : {}, extra) });

(async () => {
  const core = await import(path.join(__dirname, '..', '..', 'drop', 'core.mjs'));
  const ct = new Uint8Array(64).fill(7);

  await t('put once, get once, then gone', async () => {
    const store = memStore();
    let r = await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, ct), { store, env });
    assert.strictEqual(r.status, 201);
    assert.strictEqual(store.m.size, 1);
    const [id] = store.m.keys();
    assert.strictEqual(id, await core.storageId(TOK));
    assert.ok(!id.includes(TOK), 'the stored name does not reveal the token');
    r = await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, ct), { store, env });
    assert.strictEqual(r.status, 409, 'no overwrite');
    r = await core.handle(req('GET', '/d/' + TOK, GET_KEY), { store, env });
    assert.strictEqual(r.status, 200);
    assert.deepStrictEqual(new Uint8Array(await r.arrayBuffer()), ct);
    assert.strictEqual(r.headers.get('cache-control'), 'no-store');
    assert.strictEqual(store.m.size, 0, 'deleted by the first GET');
    r = await core.handle(req('GET', '/d/' + TOK, GET_KEY), { store, env });
    assert.strictEqual(r.status, 404);
  });
  await t('rewritten ?token= form works too', async () => {
    const store = memStore();
    assert.strictEqual((await core.handle(req('PUT', '/api/drop?token=' + TOK, PUT_KEY, ct), { store, env })).status, 201);
    assert.strictEqual((await core.handle(req('GET', '/api/drop?token=' + TOK, GET_KEY), { store, env })).status, 200);
  });
  await t('keys are separate: get key cannot put, put key cannot get, none and wrong refused', async () => {
    const store = memStore();
    assert.strictEqual((await core.handle(req('PUT', '/d/' + TOK, GET_KEY, ct), { store, env })).status, 401);
    assert.strictEqual((await core.handle(req('PUT', '/d/' + TOK, null, ct), { store, env })).status, 401);
    await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, ct), { store, env });
    assert.strictEqual((await core.handle(req('GET', '/d/' + TOK, PUT_KEY), { store, env })).status, 401);
    assert.strictEqual((await core.handle(req('GET', '/d/' + TOK, 'x'.repeat(40)), { store, env })).status, 401);
    assert.strictEqual(store.m.size, 1, 'a refused GET does not consume it');
  });
  await t('unconfigured deployment refuses everything', async () => {
    const store = memStore();
    assert.strictEqual((await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, ct), { store, env: {} })).status, 401);
  });
  await t('size limits and bad tokens', async () => {
    const store = memStore();
    assert.strictEqual((await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, new Uint8Array(core.MAX_BODY + 1)), { store, env })).status, 413);
    assert.strictEqual((await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, new Uint8Array(28)), { store, env })).status, 413);
    assert.strictEqual((await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, new Uint8Array(core.MAX_BODY)), { store, env })).status, 201);
    for (const bad of ['/d/' + TOK.toUpperCase(), '/d/../x', '/d/abc', '/x/' + TOK, '/api/drop?token=zz'])
      assert.strictEqual((await core.handle(req('GET', bad, GET_KEY), { store, env })).status, 404, bad);
  });
  await t('expired files are not served and are swept', async () => {
    const store = memStore();
    let now = 1000000;
    await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, ct), { store, env, now: () => now });
    now += core.TTL_S * 1000;
    assert.strictEqual((await core.handle(req('GET', '/d/' + TOK, GET_KEY), { store, env, now: () => now })).status, 404);
    assert.strictEqual(store.m.size, 0);
    await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, ct), { store, env, now: () => now });
    now += core.TTL_S * 1000 + 1;
    let r = await core.handleSweep(req('GET', '/api/sweep', null), { store, env, now: () => now });
    assert.strictEqual(r.status, 401, 'sweep needs the cron secret');
    r = await core.handleSweep(req('GET', '/api/sweep', 'cron-secret-123'), { store, env, now: () => now });
    assert.deepStrictEqual(await r.json(), { ok: true, deleted: 1 });
  });
  await t('delete with the put key; storage errors are body-free', async () => {
    const store = memStore();
    await core.handle(req('PUT', '/d/' + TOK, PUT_KEY, ct), { store, env });
    assert.strictEqual((await core.handle(req('DELETE', '/d/' + TOK, PUT_KEY), { store, env })).status, 200);
    assert.strictEqual(store.m.size, 0);
    const bad = { take: async () => { throw new Error('secret detail'); } };
    const r = await core.handle(req('GET', '/d/' + TOK, GET_KEY), { store: bad, env });
    assert.strictEqual(r.status, 503);
    assert.ok(!(await r.text()).includes('secret detail'));
  });
  await t('health reveals nothing', async () => {
    const r = await core.handle(req('GET', '/health', null), { store: memStore(), env });
    assert.deepStrictEqual(await r.json(), { ok: true, v: core.VERSION });
  });
  await t('blob adapter: private, no suffix, no overwrite, delete before return, sweep by age', async () => {
    const files = new Map(), calls = [];
    const blob = {
      async put(p, body, o) { calls.push(['put', p, o]); if (files.has(p) && !o.allowOverwrite) throw new Error('This blob already exists'); files.set(p, { b: new Uint8Array(body), at: new Date(5000) }); return {}; },
      async get(p, o) { calls.push(['get', p, o]); const f = files.get(p); return f ? { statusCode: 200, stream: new Blob([f.b]).stream(), blob: { uploadedAt: f.at } } : null; },
      async del(p) { calls.push(['del', p]); for (const x of [].concat(p)) files.delete(x); },
      async list(o) { return { blobs: [...files].map(([k, v]) => ({ pathname: k, url: k, uploadedAt: v.at })), hasMore: false }; },
    };
    const s = core.blobStore(blob);
    assert.strictEqual(await s.put('i1', new Uint8Array([1, 2, 3])), true);
    assert.strictEqual(await s.put('i1', new Uint8Array([1])), false);
    const putOpts = calls[0][2];
    assert.deepStrictEqual([putOpts.access, putOpts.addRandomSuffix, putOpts.allowOverwrite], ['private', false, false]);
    assert.strictEqual(calls[0][1], 'd/i1.bin');
    const got = await s.take('i1', 6000);
    assert.deepStrictEqual(got.bytes, new Uint8Array([1, 2, 3]));
    assert.strictEqual(got.expiresAt, 5000 + core.TTL_S * 1000);
    assert.strictEqual(files.size, 0);
    assert.strictEqual(await s.take('i1', 6000), null);
    await s.put('i2', new Uint8Array([9]));
    assert.strictEqual(await s.sweep(5000 + core.TTL_S * 1000 - 1), 0);
    assert.strictEqual(await s.sweep(5000 + core.TTL_S * 1000), 1);
  });
  await t('blob adapter: a failed delete means the bytes are not served', async () => {
    const blob = { get: async () => ({ statusCode: 200, stream: new Blob([new Uint8Array(40)]).stream(), blob: {} }),
                   del: async () => { throw new Error('nope'); } };
    const r = await core.handle(req('GET', '/d/' + TOK, GET_KEY), { store: core.blobStore(blob), env });
    assert.strictEqual(r.status, 503);
  });
  await t('kv adapter: native expiry, only the expiry as metadata', async () => {
    const kv = new Map(), opts = [];
    const ns = { async get(k) { return kv.has(k) ? kv.get(k).v : null; }, async put(k, v, o) { opts.push(o); kv.set(k, { v, o }); },
                 async getWithMetadata(k) { const x = kv.get(k); return x ? { value: x.v, metadata: x.o.metadata } : { value: null, metadata: null }; },
                 async delete(k) { kv.delete(k); } };
    const s = core.kvStore(ns);
    assert.strictEqual(await s.put('k', new Uint8Array([4]), 123), true);
    assert.strictEqual(await s.put('k', new Uint8Array([4]), 123), false);
    assert.deepStrictEqual(opts[0], { expirationTtl: core.TTL_S, metadata: { e: 123 } });
    const g = await s.take('k');
    assert.strictEqual(g.expiresAt, 123);
    assert.strictEqual(kv.size, 0);
  });
  if (!process.exitCode) console.log('drop: ' + passed + ' passed');
})();
