/* v1.4.6: background.js UI flow — ✅ on a sent reply is acked to the host, ❓ / 🤖 prompt acks, the relay's
 * own 🤖 text is never sent, the undo window cancels a send, queued reactions are polled. Fictional names. */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vm = require('vm');
const sleep = ms => new Promise(r => setTimeout(r, ms));
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('PASS', name); }
  catch (e) { failed++; console.log('FAIL', name, '\n   ', e.message); }
}
const UU = 'df48633d-6588-4b18-bc77-000000000001';
const KEY = '111-222|id:uuid=' + UU;

function boot(opts) {
  const EXT = path.join(__dirname, '../../extension');
  const MANIFEST = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  const bag = {}, sess = {}, listeners = [], host = [], tabMsgs = [];
  const mk = b => ({ async get(k) { const ks = k == null ? Object.keys(b) : (typeof k === 'string' ? [k] : k); const o = {}; ks.forEach(x => { if (x in b) o[x] = JSON.parse(JSON.stringify(b[x])); }); return o; },
                     async set(o) { Object.assign(b, o); }, async remove(k) { delete b[k]; } });
  const chrome = {
    runtime: { id: 'ext', getManifest: () => MANIFEST, onMessage: { addListener: f => listeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
      sendNativeMessage: async (h, m) => {
        host.push(JSON.parse(JSON.stringify(m)));
        if (m.type === 'get_config') return { ok: true, paused: false, dryRun: false, relayConversationId: '111-222', contacts: [{ name: 'Jordan', handle: '15555550123' }], uiReactions: true, undoSeconds: opts.undo || 0 };
        if (m.type === 'send') return opts.send(m);
        if (m.type === 'ui_poll') return opts.poll ? opts.poll() : { ok: true, actions: [] };
        return { ok: true };
      } },
    storage: { local: mk(bag), session: mk(sess), onChanged: { addListener() {} } },
    tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} }, query: async () => [],
            sendMessage: async (tabId, m, o) => { tabMsgs.push({ tabId, m: JSON.parse(JSON.stringify(m)), o }); return opts.tab ? opts.tab(m) : { ok: true, step: 'placed' }; } },
    alarms: { create() {}, clear() {}, onAlarm: { addListener() {} } },
    action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
  };
  vm.runInNewContext(fs.readFileSync(path.join(EXT, 'background.js'), 'utf8'),
    { chrome, console: { log() {}, warn() {} }, setTimeout, clearTimeout, crypto: require('crypto').webcrypto, Uint8Array });
  const fire = (type, item, wait) => new Promise(r => { listeners.forEach(f => f({ type, item }, { id: 'ext', tab: { id: 7 }, frameId: 0 }, () => {})); setTimeout(r, wait || 120); });
  const reply = (text, wait) => fire('relay-log', { ts: new Date().toISOString(), conversationId: '111-222', direction: 'outbound', text, key: KEY, score: 9 }, wait);
  return { host, tabMsgs, fire, reply };
}

(async () => {
  await test('sent reply: ✅ placed in the tab, acked to the host with the pending id', async () => {
    const b = boot({ send: m => ({ ok: true, sent: true, contact: 'Jordan', pendingId: 'aa11', ui: { react: [{ emoji: '\u2705', uuid: UU, pendingId: 'aa11' }] } }) });
    await b.reply('Jordan: on my way');
    const send = b.host.find(m => m.type === 'send');
    assert.strictEqual(send.ui, true);
    assert.strictEqual(send.xUuid, UU);
    const t = b.tabMsgs.find(x => x.m.action === 'react');
    assert.deepStrictEqual([t.tabId, t.m.uuid, t.m.emoji, t.o.frameId], [7, UU, '\u2705', 0]);
    const ack = b.host.find(m => m.type === 'ui_ack');
    assert.strictEqual(ack.pendingId, 'aa11');
    assert.strictEqual(ack.ok, true);
    assert.ok(Number.isFinite(ack.latencyMs));
  });

  await test('a missed reaction is reported as not ok (the Mac then posts the text line)', async () => {
    const b = boot({ send: () => ({ ok: true, sent: true, ui: { react: [{ emoji: '\u2705', uuid: UU, pendingId: 'bb22' }] } }),
                     tab: () => ({ ok: false, step: 'no-react-button' }) });
    await b.reply('Jordan: hi');
    const ack = b.host.find(m => m.type === 'ui_ack');
    assert.deepStrictEqual([ack.ok, ack.step], [false, 'no-react-button']);
  });

  await test('❓ prompt and 🤖 typed prompt are done and acked by note id', async () => {
    const b = boot({ send: () => ({ ok: false, needsConfirm: true, handle: '+15555550123', label: 'Jordan', reason: 'guessed',
                                    ui: { react: [{ emoji: '\u2753', uuid: UU, noteId: 'cc33' }], type: [{ text: '\u{1F916} Who\u2019s this for?', noteId: 'dd44' }] } }) });
    await b.reply('see you soon');
    const acts = b.tabMsgs.map(x => x.m.action + ':' + (x.m.emoji || x.m.text));
    assert.deepStrictEqual(acts, ['react:\u2753', 'type:\u{1F916} Who\u2019s this for?']);
    assert.strictEqual(b.tabMsgs[0].m.prompt, true);
    assert.deepStrictEqual(b.host.filter(m => m.type === 'ui_ack').map(m => m.noteId), ['cc33', 'dd44']);
  });

  await test('the relay\'s own 🤖 text is never sent', async () => {
    const b = boot({ send: () => { throw new Error('must not send'); } });
    await b.reply('\u{1F916} Who\u2019s this for? Start with a name');
    assert.ok(!b.host.some(m => m.type === 'send'));
  });

  await test('untagged "type" directives are refused by the background too', async () => {
    const b = boot({ send: () => ({ ok: false, needsTarget: true, ui: { type: [{ text: 'Jordan: hi', noteId: 'ee55' }] } }) });
    await b.reply('hello there');
    assert.ok(!b.tabMsgs.some(x => x.m.action === 'type'));
    assert.strictEqual(b.host.find(m => m.type === 'ui_ack').ok, false);
  });

  await test('undo window: 🗑️ within it cancels; nothing reaches the host', async () => {
    const b = boot({ undo: 1, send: () => { throw new Error('must not send'); }, tab: m => (m.action === 'await-undo' ? { trashed: true } : { ok: true }) });
    await b.reply('Jordan: oops', 200);
    assert.ok(!b.host.some(m => m.type === 'send'));
    assert.strictEqual(b.tabMsgs[0].m.ms, 1000);
    assert.ok(b.host.some(m => m.type === 'signal' && m.status === 'undo_cancelled'));
  });

  await test('undo window: no 🗑️ -> sent; commands skip the window', async () => {
    const b = boot({ undo: 2, send: () => ({ ok: true, sent: true }), tab: m => (m.action === 'await-undo' ? { trashed: false } : { ok: true }) });
    await b.reply('Jordan: fine', 200);
    assert.strictEqual(b.host.filter(m => m.type === 'send').length, 1);
    const n = b.tabMsgs.length;
    await b.reply('/who', 200);
    assert.strictEqual(b.tabMsgs.length, n, 'no undo wait for a command');
  });

  await test('tick polls the host and performs queued reactions (ids only)', async () => {
    const b = boot({ send: () => ({ ok: true }), poll: () => ({ ok: true, actions: [{ id: 'ff66', action: 'react', uuid: UU, emoji: '\u{1F440}' }] }) });
    await b.fire('relay-ui-tick', undefined, 200);
    const t = b.tabMsgs.find(x => x.m.action === 'react');
    assert.strictEqual(t.m.emoji, '\u{1F440}');
    const ack = b.host.find(m => m.type === 'ui_ack');
    assert.deepStrictEqual([ack.actionId, ack.ok], ['ff66', true]);
  });

  console.log(`${passed} passed, ${failed} failed (v1.4.6 background UI flow)`);
  process.exit(failed ? 1 : 0);
})();
