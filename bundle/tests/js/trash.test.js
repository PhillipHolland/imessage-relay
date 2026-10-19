/* v1.4.5: a wastebasket reaction on a relayed message asks the Mac "Block <sender>?" (content.js + background.js).
 *   NODE_PATH=/path/to/node_modules node tests/js/trash.test.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');
const { CONFIG, createRelayDetector } = require('../../extension/content.js');

const sleep = ms => new Promise(r => setTimeout(r, ms));
const FAST = { settleMs: 150, batchDelayMs: 20, dupWindowMs: 60, emptyRetryDelayMs: 40, urlPollMs: 50, ownerNames: ['OwnerAcct'], botNames: ['SenderBot'] };
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('PASS', name); }
  catch (e) { failed++; console.log('FAIL', name, '\n   ', e.message); }
}

function setup(html, url, opts = {}) {
  const dom = new JSDOM(html || fixture, { url: url || 'https://x.com/i/chat/111-222', pretendToBeVisual: true });
  const { window } = dom;
  window.Element.prototype.getBoundingClientRect = function () {
    const v = this.getAttribute('data-fake-rect');
    if (!v) return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
    const [l, t, w, h] = v.split(',').map(Number);
    return { left: l, top: t, width: w, height: h, right: l + w, bottom: t + h };
  };
  const state = Object.assign({ paused: false, conversationFilter: '' }, opts.state);
  const logs = [];
  const reacts = [];
  const statuses = [];
  const seenSet = opts.seenSet || new Set();
  const det = createRelayDetector({
    window, document: window.document, config: Object.assign({}, FAST, opts.config), getState: () => state,
    onEmit: i => logs.push(i), onStatus: s => statuses.push(s), log: () => {}, onReaction: r => reacts.push(r),
    now: opts.now, ownerCheck: opts.ownerCheck,
    seen: { has: u => seenSet.has(u), add: u => seenSet.add(u) },
  });
  det.start();
  return { window, doc: window.document, det, logs, statuses, state, dom, seenSet, reacts };
}

// uuid helpers
const uuid = n => `df48633d-6588-4b18-bc77-${String(n).padStart(12, '0')}`;
function msg(doc, n, dir, build, opts = {}) {
  const e = doc.createElement('div');
  e.setAttribute('data-testid', 'message-' + uuid(n));
  e.style.display = 'flex';
  e.style.justifyContent = dir === 'out' ? 'flex-end' : 'flex-start';
  const left = dir === 'out' ? 780 : 420;
  const b = doc.createElement('div');
  b.setAttribute('data-fake-rect', `${left},0,200,40`);
  if (dir === 'in') b.innerHTML = '<a href="/SenderBot"><img alt="SenderBot"></a>';
  const span = doc.createElement('span');
  span.setAttribute('data-testid', 'message-text-' + uuid(n));
  if (typeof build === 'function') build(span, doc); else span.textContent = build;
  b.appendChild(span);
  if (opts.time) { const t = doc.createElement('time'); t.setAttribute('datetime', opts.time); t.textContent = 't'; b.appendChild(t); }
  e.appendChild(b);
  return e;
}
const panel = doc => doc.querySelector('[data-testid="dm-conversation-panel"]');
const fixture = '<html><body><div data-testid="dm-conversation-panel"></div></body></html>';
const PROMPT = '\u2753 Reply to Sam? Reply yes within 2 min.';
function react(doc, entry, emoji) {
  const pill = doc.createElement('div');
  pill.setAttribute('data-testid', 'reactions');
  const s = doc.createElement('span'); s.textContent = emoji; pill.appendChild(s);
  entry.appendChild(pill);
  return pill;
}
const nowIso = (ago) => new Date(Date.now() - (ago || 0)).toISOString();

(async () => {
  const TRASH = '\u{1F5D1}\uFE0F';
  await test('a new wastebasket on a relayed message emits one block event with that message', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const m = msg(doc, 10, 'in', 'Sam: buy now');
    panel(doc).appendChild(m);
    await sleep(60);
    assert.strictEqual(reacts.length, 0);
    react(doc, m, TRASH);
    await sleep(60);
    assert.deepStrictEqual(reacts.map(r => [r.word, r.quotedText, r.conversationId]), [['block', 'Sam: buy now', '111-222']]);
    react(doc, m, TRASH);
    await sleep(60);
    assert.strictEqual(reacts.length, 1, 'only once');
  });
  await test('a wastebasket already there does nothing', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const m = msg(doc, 11, 'in', 'Sam: old');
    react(doc, m, TRASH);
    panel(doc).appendChild(m);
    await sleep(60);
    react(doc, m, '\u{1F602}');
    await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });
  await test('my own message, a prompt, other emoji, or a non-owner never block', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const out = msg(doc, 12, 'out', 'Sam: hi'); panel(doc).appendChild(out);
    const p = msg(doc, 13, 'in', PROMPT, { time: nowIso(1000) }); panel(doc).appendChild(p);
    const m = msg(doc, 14, 'in', 'Sam: hey'); panel(doc).appendChild(m);
    await sleep(60);
    react(doc, out, TRASH); react(doc, p, TRASH); react(doc, m, '\u2764\uFE0F');
    await sleep(60);
    assert.deepStrictEqual(reacts.map(r => r.quotedText), []);
    const s2 = setup(null, null, { ownerCheck: () => false });
    await sleep(200);
    const m2 = msg(s2.doc, 15, 'in', 'Sam: hey'); panel(s2.doc).appendChild(m2); await sleep(60);
    react(s2.doc, m2, TRASH); await sleep(60);
    assert.strictEqual(s2.reacts.length, 0);
  });
  await test('paused: nothing', async () => {
    const { doc, reacts } = setup(null, null, { state: { paused: true } });
    await sleep(200);
    const m = msg(doc, 16, 'in', 'Sam: hey'); panel(doc).appendChild(m); await sleep(60);
    react(doc, m, TRASH); await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });

  // background.js: forwards {word:'block', quotedText} to the Mac, nothing else
  await test('background forwards block with the quoted text only', async () => {
    const vm = require('vm');
    const EXT = path.join(__dirname, '../../extension');
    const MANIFEST = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
    const bag = {}, sess = {}, listeners = [], host = [];
    const mk = b => ({ async get(k) { const ks = k == null ? Object.keys(b) : (typeof k === 'string' ? [k] : k); const o = {}; ks.forEach(x => { if (x in b) o[x] = JSON.parse(JSON.stringify(b[x])); }); return o; },
                       async set(o) { Object.assign(b, o); }, async remove(k) { delete b[k]; } });
    const chrome = {
      runtime: { id: 'ext', getManifest: () => MANIFEST, onMessage: { addListener: f => listeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
        sendNativeMessage: async (h, m) => { host.push(m); return m.type === 'get_config' ? { ok: true, paused: false, dryRun: true, relayConversationId: '111-222', contacts: [{ name: 'Sam', handle: '15555550150' }] } : { ok: true }; } },
      storage: { local: mk(bag), session: mk(sess), onChanged: { addListener() {} } },
      tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} }, query: async () => [] },
      alarms: { create() {}, clear() {}, onAlarm: { addListener() {} } },
      action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
    };
    vm.runInNewContext(fs.readFileSync(path.join(EXT, 'background.js'), 'utf8'),
      { chrome, console: { log() {}, warn() {} }, setTimeout, clearTimeout, crypto: require('crypto').webcrypto, Uint8Array });
    const fire = item => new Promise(r => { listeners.forEach(f => f({ type: 'relay-reaction', item }, { id: 'ext', tab: { id: 1 } }, () => {})); setTimeout(r, 80); });
    await fire({ word: 'block', quotedText: 'Sam: buy now', conversationId: '111-222', key: 'trash:a' });
    await fire({ word: 'block', quotedText: 'Sam: buy now', conversationId: '111-222', key: 'trash:a' });   // duplicate
    await fire({ word: 'block', quotedText: 'Sam: buy now', conversationId: '999-000', key: 'trash:b' });   // other chat
    await fire({ word: 'block', quotedText: '', conversationId: '111-222', key: 'trash:c' });
    const r = host.filter(m => m.type === 'reaction');
    assert.deepStrictEqual(JSON.parse(JSON.stringify(r)), [{ type: 'reaction', word: 'block', quotedText: 'Sam: buy now', conversationId: '111-222', ui: true }]);   // v1.4.6: + boolean ui flag
    assert.ok(!host.some(m => m.type === 'send'), 'never a send');
    assert.ok(!JSON.stringify(bag).includes('buy now'), 'not stored');
  });

  console.log(`${passed} passed, ${failed} failed (v1.4.5 wastebasket)`);
  process.exit(failed ? 1 : 0);
})();
