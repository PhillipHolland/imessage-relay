/* v1.4.2: X Chat's current native Reply ('Replied to <name>' + preview). Harness from detect.test.js. Run:
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
  const statuses = [];
  const seenSet = opts.seenSet || new Set();
  const det = createRelayDetector({
    window, document: window.document, config: Object.assign({}, FAST, opts.config), getState: () => state,
    onEmit: i => logs.push(i), onStatus: s => statuses.push(s), log: () => {},
    now: opts.now, ownerCheck: opts.ownerCheck,
    seen: { has: u => seenSet.has(u), add: u => seenSet.add(u) },
  });
  det.start();
  return { window, doc: window.document, det, logs, statuses, state, dom, seenSet };
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
const fixture = '<html><body><div data-testid="dm-conversation-panel"><div data-testid="message-df48633d-6588-4b18-bc77-000000009999"><span data-testid="message-text-df48633d-6588-4b18-bc77-000000009999">earlier</span></div></div></body></html>';
(async () => {
  await test('"Replied to iMessage" + preview inside the bubble -> quotedText carries "Rory: ..."', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const m = msg(doc, 1, 'out', 'Ha, true');
    const pv = doc.createElement('div');
    pv.innerHTML = '<div><span>Replied to iMessage</span></div><div><span>Rory: Not since the jump</span></div>';
    m.querySelector('div').prepend(pv);
    panel(doc).appendChild(m);
    await sleep(80);
    assert.strictEqual(logs.length, 1);
    assert.strictEqual(logs[0].text, 'Ha, true');
    assert.ok(/Rory: Not since/.test(logs[0].quotedText), JSON.stringify(logs[0].quotedText));
    assert.ok(!/Replied to/.test(logs[0].quotedText));
    det.stop();
  });
  await test('a reference to the replied-to message uuid resolves its text from the page', async () => {
    const { doc, det, logs } = setup();
    panel(doc).appendChild(msg(doc, 2, 'in', 'Rory: Not since the jump'));
    await sleep(200);
    const m = msg(doc, 3, 'out', 'Ha, true');
    const ref = doc.createElement('a');
    ref.setAttribute('href', '#message-' + uuid(2));
    ref.textContent = 'Original message';
    m.querySelector('div').prepend(ref);
    panel(doc).appendChild(m);
    await sleep(80);
    assert.strictEqual(logs.length, 1);
    assert.strictEqual(logs[0].text, 'Ha, true');
    assert.strictEqual(logs[0].quotedText, 'Rory: Not since the jump');
    det.stop();
  });
  await test('"Replied to" row just above the bubble (outside the entry)', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const row = doc.createElement('div');
    const label = doc.createElement('div');
    label.innerHTML = '<span>You replied to iMessage</span><div>Rory: Not since the jump</div>';
    row.appendChild(label);
    row.appendChild(msg(doc, 4, 'out', 'Ha, true'));
    panel(doc).appendChild(row);
    await sleep(80);
    assert.strictEqual(logs.length, 1);
    assert.ok(/^Rory: Not since/.test(logs[0].quotedText), JSON.stringify(logs[0].quotedText));
    det.stop();
  });
  await test('the owner typing "replied to" in the reply itself is not a quote', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(msg(doc, 5, 'out', 'Replied to the landlord already'));
    await sleep(80);
    assert.strictEqual(logs.length, 1);
    assert.ok(!logs[0].quotedText, JSON.stringify(logs[0].quotedText));
    det.stop();
  });
  await test('the older reply-preview testid shape still works', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const m = msg(doc, 6, 'out', 'yes, 7 works');
    const q = doc.createElement('div');
    q.setAttribute('data-testid', 'reply-preview');
    q.innerHTML = '<span>Alex R.: dinner at 7?</span>';
    m.querySelector('div').prepend(q);
    panel(doc).appendChild(m);
    await sleep(80);
    assert.strictEqual(logs[0].quotedText, 'Alex R.: dinner at 7?');
    det.stop();
  });
  console.log(passed + ' passed, ' + failed + ' failed (v1.4.2 native Reply quote)');
  process.exit(failed ? 1 : 0);
})();
