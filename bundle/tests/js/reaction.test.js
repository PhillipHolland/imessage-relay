/* v1.4.2: thumbs-up / thumbs-down on a pending prompt (content.js). Harness from detect.test.js. Run:
 *   NODE_PATH=/path/to/node_modules node tests/js/detect.test.js
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
  await test('a new thumbs-up on a fresh prompt confirms once', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const p = msg(doc, 1, 'in', PROMPT, { time: nowIso(1000) });
    panel(doc).appendChild(p);
    await sleep(60);
    assert.strictEqual(reacts.length, 0);
    react(doc, p, '\u{1F44D}');
    await sleep(60);
    assert.deepStrictEqual(reacts.map(r => r.word), ['yes']);
    assert.strictEqual(reacts[0].conversationId, '111-222');
    assert.ok(!JSON.stringify(reacts[0]).includes('Sam'), 'no prompt text in the event');
    react(doc, p, '\u{1F44D}\u{1F3FD}');
    await sleep(60);
    assert.strictEqual(reacts.length, 1, 'only once');
  });
  await test('thumbs-down cancels', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const p = msg(doc, 2, 'in', PROMPT); panel(doc).appendChild(p); await sleep(60);
    react(doc, p, '\u{1F44E}'); await sleep(60);
    assert.deepStrictEqual(reacts.map(r => r.word), ['no']);
  });
  await test('a reaction already there at page load does nothing', async () => {
    const html = '<html><body><div data-testid="dm-conversation-panel"></div></body></html>';
    const dom0 = new (require('jsdom').JSDOM)(html);
    const p0 = msg(dom0.window.document, 3, 'in', PROMPT, { time: nowIso(5000) });
    react(dom0.window.document, p0, '\u{1F44D}');
    dom0.window.document.querySelector('[data-testid="dm-conversation-panel"]').appendChild(p0);
    const { doc, reacts } = setup(dom0.serialize());
    await sleep(200);
    const p = doc.querySelector('[data-testid="message-' + uuid(3) + '"]');
    p.appendChild(doc.createElement('i'));      // any later change on it
    await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });
  await test('reactions on anything but a prompt do nothing', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const a = msg(doc, 4, 'in', 'Sam: running late'); panel(doc).appendChild(a); await sleep(60);
    react(doc, a, '\u{1F44D}'); await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });
  await test('an emoji in the text, or a hover picker, is not a reaction', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const p = msg(doc, 5, 'in', PROMPT); panel(doc).appendChild(p); await sleep(60);
    p.querySelector('[data-testid^="message-text-"]').textContent = PROMPT + ' \u{1F44D}';
    await sleep(60);
    react(doc, p, '\u2764\uFE0F\u{1F602}\u{1F62E}\u{1F622}\u{1F44D}\u{1F44E}');
    await sleep(60);
    const menu = doc.createElement('div'); menu.setAttribute('role', 'menu'); menu.textContent = '\u{1F44D}'; p.appendChild(menu);
    await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });
  await test('an expired prompt (over 2 min) does nothing', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const p = msg(doc, 6, 'in', PROMPT, { time: nowIso(150000) }); panel(doc).appendChild(p); await sleep(60);
    react(doc, p, '\u{1F44D}'); await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });
  await test('only the newest prompt counts', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const p1 = msg(doc, 7, 'in', PROMPT, { time: nowIso(30000) }); panel(doc).appendChild(p1); await sleep(40);
    const p2 = msg(doc, 8, 'in', '\u2753 Send to Alex? Reply yes within 2 min.', { time: nowIso(1000) }); panel(doc).appendChild(p2); await sleep(60);
    react(doc, p1, '\u{1F44D}'); await sleep(60);
    assert.strictEqual(reacts.length, 0, 'older prompt ignored');
    react(doc, p2, '\u{1F44D}'); await sleep(60);
    assert.strictEqual(reacts.length, 1);
  });
  await test('no owner identity -> nothing', async () => {
    const { doc, reacts } = setup(null, null, { ownerCheck: () => false });
    await sleep(200);
    const p = msg(doc, 9, 'in', PROMPT); panel(doc).appendChild(p); await sleep(60);
    react(doc, p, '\u{1F44D}'); await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });
  await test('paused or another conversation -> nothing', async () => {
    const { doc, reacts, state } = setup(null, null, { state: { conversationFilter: '999-000' } });
    await sleep(200);
    const p = msg(doc, 10, 'in', PROMPT); panel(doc).appendChild(p); await sleep(60);
    react(doc, p, '\u{1F44D}'); await sleep(60);
    assert.strictEqual(reacts.length, 0);
  });
  console.log(passed + ' passed, ' + failed + ' failed (v1.4.2 prompt reactions)');
  process.exit(failed ? 1 : 0);
})();
