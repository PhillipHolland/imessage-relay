/* v1.4.4: X Chat's real native Reply markup, rebuilt from a v1.4.3 quote_probe fingerprint. Run:
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

// The row shape X served on 2026-10-19 (from the text-free fingerprint):
// E=0.div@message-<self>{data-dm-message-row,data-send-status}
//   1.div{aria-hidden} (empty), 1.span{hidden,id}+t x2      (screen-reader labels)
//   1.div > 2.div (empty) , 2.div > 3.div (PREVIEW: 4.div[5.svg icon, 5.div>6.div>7.div+t] 4.div>5.span>6.span+t)
//                                  3.div#article{aria-labelledby,...} > 4.div@message-text-<self> ...
function xRow(doc, n, opts = {}) {
  const u = uuid(n);
  const row = doc.createElement('div');
  row.setAttribute('data-testid', 'message-' + u);
  row.setAttribute('data-dm-message-row', '');
  row.setAttribute('data-send-status', 'sent');
  const sr = opts.srText || ['You sent', '12:56 PM'];
  row.innerHTML =
    '<div aria-hidden="true"></div><span hidden id="a' + n + '">' + sr[0] + '</span><span hidden id="b' + n + '">' + sr[1] + '</span>' +
    '<div><div></div><div>' +
      (opts.preview === false ? '' :
        '<div class="pv"><div><svg data-icon="reply" viewBox="0 0 24 24"><path d="M0 0"></path></svg><div><div><div>' + (opts.head || 'iMessage') + '</div></div></div></div>' +
        '<div><span><span>' + (opts.quoted || 'Jordan: (I got it)') + '</span></span></div></div>') +
      '<div role="article" tabindex="0" aria-labelledby="a' + n + '" aria-describedby="b' + n + '">' +
        '<div data-testid="message-text-' + u + '" id="t' + n + '"><div><span><span dir="auto"><span>' + (opts.text || 'Thanks!') + '</span></span>' +
        '<span aria-hidden="true"><div><span>12:56 PM</span><svg data-icon="check"></svg></div></span></span></div></div>' +
      '</div>' +
    '</div></div>';
  return row;
}

(async () => {
  await test('real X Reply row: preview before role=article -> quotedText names Jordan', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 1));
    await sleep(80);
    assert.strictEqual(logs.length, 1);
    assert.ok(/Jordan: \(I got it\)/.test(logs[0].quotedText || ''), JSON.stringify(logs[0]));
    assert.ok(!/Thanks/.test(logs[0].quotedText));
    assert.ok(!/12:56|You sent/.test(logs[0].quotedText), 'hidden labels / chips are not the quote');
    det.stop();
  });
  await test('same row without a preview -> no quote', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 2, { preview: false, text: 'hello there' }));
    await sleep(80);
    assert.strictEqual(logs.length, 1);
    assert.ok(!logs[0].quotedText, JSON.stringify(logs[0].quotedText));
    det.stop();
  });
  await test('reply body text never leaks into the quote, and the quote is not in the body', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 3, { quoted: 'Rory: Not since the jump', text: 'Ha, true' }));
    await sleep(80);
    assert.strictEqual(logs[0].text, 'Ha, true');
    assert.ok(/^iMessage Rory: Not since the jump$/.test(logs[0].quotedText), JSON.stringify(logs[0].quotedText));
    det.stop();
  });
  await test('fingerprint of the rebuilt row matches the probe shape', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 4));
    await sleep(80);
    const fp = logs[0].quoteProbe;
    assert.ok(/^E=0\.div@message-<self>\{data-dm-message-row,data-send-status\} 1\.div\{aria-hidden\} 1\.span\{hidden,id\}\+t 1\.span\{hidden,id\}\+t 1\.div 2\.div 2\.div/.test(fp), fp);
    assert.ok(/3\.div#article\{tabindex,aria-labelledby,aria-describedby\} 4\.div@message-text-<self>\{id\}/.test(fp), fp);
    det.stop();
  });
  console.log(passed + ' passed, ' + failed + ' failed (v1.4.4 real X Reply markup)');
  process.exit(failed ? 1 : 0);
})();
