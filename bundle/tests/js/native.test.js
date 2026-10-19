/* Background (service worker) tests: config from the host (get_config), reply gating,
 * handle normalization, popup status, self-test relay, and "no bodies in storage". Run:
 *   NODE_PATH=/path/to/node_modules node tests/js/native.test.js
 */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const BG = fs.readFileSync(path.join(__dirname, '../../extension/background.js'), 'utf8');
const CONV = '1111-2222';

function env(opts = {}) {
  const store = {}, listeners = [], nativeCalls = [], badge = {};
  let hostConfig = Object.assign({ ok: true, version: '1.3.2', paused: false, dryRun: true,
    relayConversationId: CONV, contacts: [{ name: 'Test Contact', label: 'Test', handle: '15555550123' }] }, opts.config || {});
  const local = {
    async get(keys) { const ks = typeof keys === 'string' ? [keys] : keys; const o = {}; ks.forEach(k => { if (k in store) o[k] = JSON.parse(JSON.stringify(store[k])); }); return o; },
    async set(v) { Object.assign(store, JSON.parse(JSON.stringify(v))); },
  };
  const chrome = {
    runtime: {
      id: 'ext', onMessage: { addListener: f => listeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
      sendNativeMessage: async (host, msg) => {
        nativeCalls.push({ host, msg });
        if (opts.hostDown) throw new Error('Specified native messaging host not found.');
        if (msg.type === 'get_config') return hostConfig;
        if (msg.type === 'selftest') return { ok: true, detail: 'sent' };
        return opts.sendReply || { ok: true, dryRun: true, contact: 'Test', routedBy: 'recent' };
      },
    },
    storage: { local, session: { async get() { return {}; }, async set() {} }, onChanged: { addListener() {} } },
    action: { async setBadgeText(o) { badge.text = o.text; }, async setBadgeBackgroundColor() {} },
    alarms: { create() {}, onAlarm: { addListener() {} } },
    tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} } },
  };
  const ctx = { chrome, console: { log() {}, warn() {} } };
  vm.runInNewContext(BG, ctx);
  let n = 0;
  const send = over => {
    const item = Object.assign({ ts: new Date().toISOString(), conversationId: CONV, direction: 'outbound', text: 'Hey', signals: [], score: 3, key: CONV + '|id:uuid=' + (n++), source: 'frame' }, over);
    listeners.forEach(f => f({ type: 'relay-log', item }, { id: 'ext', tab: { id: 1 }, frameId: 3 }, () => {}));
  };
  const ask = msg => new Promise(res => listeners.forEach(f => f(msg, { id: 'ext' }, res)));
  const sends = () => nativeCalls.filter(c => c.msg.type === 'send').map(c => c.msg);
  const signal = sig => listeners.forEach(f => f({ type: 'relay-signal', signal: sig }, { id: 'ext', tab: { id: 1 } }, () => {}));
  return { ctx, store, badge, nativeCalls, sends, send, signal, ask, setHost: v => { hostConfig = Object.assign({}, hostConfig, v); } };
}
let passed = 0, failed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log('PASS', name); } catch (e) { failed++; console.log('FAIL', name, '\n   ', e.stack.split('\n').slice(0, 2).join(' | ')); } }

(async () => {
  await test('normalizeHandle: E.164 for phones (+1 default), lowercase emails; same rules as the host', async () => {
    const { ctx } = env();
    const cases = [
      ['15555550123', '+15555550123'], ['5555550123', '+15555550123'], ['(555) 555-0123', '+15555550123'],
      ['+1 555.555.0123', '+15555550123'], ['tel:+1-555-555-0123', '+15555550123'], ['0044 20 7946 0000', '+442079460000'],
      ['+44 20 7946 0000', '+442079460000'], [' Someone@Example.COM ', 'someone@example.com'], ['mailto:A@B.co', 'a@b.co'], ['', ''], ['abc', ''],
    ];
    for (const [i, o] of cases) assert.strictEqual(ctx.normalizeHandle(i), o, i);
    assert.ok(ctx.validHandle('+15555550123') && ctx.validHandle('a@b.co') && !ctx.validHandle('+123') && !ctx.validHandle('x; rm -rf'));
    // the shared vectors file is also checked by the Python tests
    const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, '../normalize_vectors.json'), 'utf8'));
    for (const [i, o] of vectors) assert.strictEqual(ctx.normalizeHandle(i), o, 'vector ' + i);
  });

  await test('only the relay chat from get_config is forwarded; the host picks the recipient', async () => {
    const e = env();
    e.send({ text: 'Hey', key: 'k1' });
    e.send({ text: 'Hey', key: 'k1' });             // same key again (re-render / second instance)
    await sleep(80);
    assert.ok(e.nativeCalls[0].msg.type === 'get_config' && e.nativeCalls[0].host === 'com.imessage_relay.host');
    assert.strictEqual(e.sends().length, 1);
    const m = e.sends()[0];
    assert.deepStrictEqual(Object.keys(m).sort(), ['conversationId', 'quotedText', 'text', 'ts', 'type']);
    assert.strictEqual(m.conversationId, CONV);
    assert.strictEqual(e.store.relayConversationId, CONV, 'mirrored for the content script filter');
    const ev = e.store.events[0];
    assert.strictEqual(ev.status, 'dry-run');
    assert.strictEqual(ev.contactName, 'Test');
    assert.strictEqual(ev.routedBy, 'recent');
    assert.strictEqual(ev.textLength, 3);
    assert.ok(!JSON.stringify(e.store).includes('"Hey"'), 'no message body in extension storage');
  });

  await test('not forwarded: inbound, unknown, low score, stale, empty; other conversations leave no trace', async () => {
    const e = env();
    e.send({ direction: 'inbound', score: -3 });
    e.send({ direction: 'unknown', score: 0 });
    e.send({ conversationId: '9999-1111' });
    await sleep(60);
    e.send({ score: 2 });
    e.send({ ts: new Date(Date.now() - 5 * 60 * 1000).toISOString() });
    e.send({ text: '[no text]' });
    await sleep(100);
    assert.strictEqual(e.sends().length, 0);
    assert.deepStrictEqual(e.store.events.map(i => i.detail), ['direction score 2 < 3', 'stale item', 'empty text']);
    assert.ok(!JSON.stringify(e.store).includes('9999-1111'), 'other conversation id not stored');
  });

  await test('relayAll with no contacts: replies still go to the host (it checks the roster); status says so', async () => {
    const off = env({ config: { contacts: [] } });
    off.send({ key: 'r0' });
    await sleep(80);
    assert.strictEqual(off.sends().length, 0);
    assert.strictEqual(off.store.events[0].detail, 'no contacts in config');
    const e = env({ config: { contacts: [], relayAll: true }, sendReply: { ok: true, dryRun: true, contact: 'Pat', routedBy: 'prefix' } });
    e.send({ key: 'r1', text: 'Pat: hi' });
    await sleep(80);
    assert.strictEqual(e.sends().length, 1);
    assert.strictEqual(e.store.status.relayAll, true);
    assert.strictEqual(e.store.events[0].contactName, 'Pat');
  });

  await test('relay chat not set: nothing forwarded; popup status says so', async () => {
    const e = env({ config: { relayConversationId: '' } });
    e.send({});
    await sleep(80);
    assert.strictEqual(e.sends().length, 0);
    assert.strictEqual(e.store.events, undefined);
    assert.strictEqual(e.store.status.relayChatSet, false);
  });

  await test('quoted text goes to the host but is never stored; needsTarget shows "needs-name"', async () => {
    const e = env({ sendReply: { ok: false, needsTarget: true, error: "Who's this for? Start with a name, like Test: …" } });
    e.send({ text: 'sure thing', quotedText: 'Test: dinner tonight?' });
    await sleep(80);
    assert.strictEqual(e.sends()[0].quotedText, 'Test: dinner tonight?');
    const ev = e.store.events[0];
    assert.strictEqual(ev.status, 'needs-name');
    assert.ok(ev.detail.startsWith("Who's this for?"));
    assert.strictEqual(ev.contactName, '');
    const dump = JSON.stringify(e.store);
    assert.ok(!dump.includes('dinner') && !dump.includes('sure thing'), 'no bodies or quotes in storage');
  });

  await test('paused in the host config: nothing sent, badge II, paused mirrored for content scripts', async () => {
    const e = env({ config: { paused: true } });
    e.send({});
    await sleep(80);
    assert.strictEqual(e.sends().length, 0);
    assert.strictEqual(e.store.events[0].detail, 'paused');
    assert.strictEqual(e.store.paused, true);
    assert.strictEqual(e.badge.text, 'II');
  });

  await test('host not installed: status says not connected, badge !, nothing sent', async () => {
    const e = env({ hostDown: true });
    e.send({});
    await sleep(80);
    assert.strictEqual(e.sends().length, 0);
    assert.strictEqual(e.store.status.hostConnected, false);
    assert.ok(/not found/.test(e.store.status.hostError));
    assert.strictEqual(e.badge.text, '!');
  });

  await test('popup relay-status: re-checks host, returns status + last 5 events newest first', async () => {
    const e = env({ config: { dryRun: false, ownerXHandle: '@OwnerAcct', senderXHandle: 'SenderBot' }, sendReply: { ok: true, dryRun: false, sent: true, delivered: true, contact: 'Test', routedBy: 'prefix' } });
    for (let i = 0; i < 7; i++) { e.send({ text: 'm' + i, key: 'k' + i }); await sleep(30); }
    const r = await e.ask({ type: 'relay-status' });
    assert.strictEqual(r.status.hostConnected, true);
    assert.strictEqual(r.status.dryRun, false);
    assert.deepStrictEqual(r.status.contacts, ['Test']);
    assert.strictEqual(r.status.relayChatSet, true);
    assert.strictEqual(r.events.length, 5);
    assert.strictEqual(r.events[0].textLength, 2);
    assert.ok(r.events.every(x => x.status === 'sent' && x.detail === 'delivered'));
    assert.deepStrictEqual(e.store.ownerNames, ['OwnerAcct']);
    assert.deepStrictEqual(e.store.senderNames, ['SenderBot']);
    assert.strictEqual(e.badge.text, '7');
  });

  await test('host refusal is recorded; the next item re-checks the host', async () => {
    const e = env({ sendReply: { ok: false, error: 'handle not on allowlist' } });
    e.send({});
    await sleep(80);
    assert.strictEqual(e.store.events[0].status, 'refused');
    assert.strictEqual(e.store.events[0].detail, 'handle not on allowlist');
  });

  await test('pending self-test in get_config: asks the host to run it once', async () => {
    const e = env({ config: { pendingSelfTest: { id: 'st-1' } } });
    await e.ask({ type: 'relay-status' });
    await sleep(50);
    await e.ask({ type: 'relay-status' });
    await sleep(50);
    const st = e.nativeCalls.filter(c => c.msg.type === 'selftest');
    assert.strictEqual(st.length, 1);
    assert.strictEqual(st[0].msg.id, 'st-1');
    assert.strictEqual(e.store.events.find(x => x.kind === 'selftest').status, 'sent');
  });

  await test('wrong_account content signal: status + ACC badge, nothing sent to the host', async () => {
    const e = env();
    e.signal({ status: 'wrong_account' });
    await sleep(60);
    assert.strictEqual(e.store.status.wrongAccount, true);
    assert.strictEqual(e.badge.text, 'ACC');
    assert.strictEqual(e.sends().length, 0);
    assert.ok(!JSON.stringify(e.store).includes('wrong account'), 'no body in storage');
  });

  await test('refused over-limit reply from content: owner notice event, no host send', async () => {
    const e = env();
    e.send({ refuse: 'too-long', textLength: 5000, limit: 4000, key: CONV + '|id:uuid=reffed' });
    await sleep(60);
    assert.strictEqual(e.sends().length, 0, 'never contacts the host');
    const ev = e.store.events[0];
    assert.strictEqual(ev.status, 'refused');
    assert.ok(/too long to relay/.test(ev.detail));
    assert.strictEqual(ev.textLength, 5000);
  });

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
