/* v1.4.10: native Reply to a relayed PHOTO (image bubble or its caption) with an emoji-only body. Run:
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


const HEART = '<img alt="\u2764\uFE0F" draggable="false" src="https://abs-0.twimg.com/emoji/v2/svg/2764.svg">';
const THUMB = '<img alt="Image" src="https://pbs.twimg.com/media/Fabc123?format=jpg&name=small" width="200" height="150">';
(async () => {
  await test('emoji-only reply quoting the IMAGE bubble: text is the emoji, thumbnail is not his photo, quote = photo with no name', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 1, { quoted: THUMB, head: '', text: HEART + HEART + HEART }));
    await sleep(150);
    assert.strictEqual(logs.length, 1, JSON.stringify(logs));
    assert.strictEqual(logs[0].text, '\u2764\uFE0F\u2764\uFE0F\u2764\uFE0F');
    assert.ok(!logs[0].mediaCount, 'the quoted thumbnail must not ride along as his photo');
    assert.ok(/^(iMessage )?\u{1F4F7} Photo$/u.test(logs[0].quotedText), logs[0].quotedText);
    det.stop();
  });
  await test('emoji-only reply quoting the CAPTION (preview has thumbnail + "Jordan: 📷 Photo"): routes by the caption name', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 2, { quoted: THUMB + 'Jordan: \u{1F4F7} Photo', text: HEART + HEART + HEART }));
    await sleep(150);
    assert.strictEqual(logs.length, 1, JSON.stringify(logs));
    assert.strictEqual(logs[0].text, '\u2764\uFE0F\u2764\uFE0F\u2764\uFE0F');
    assert.ok(!logs[0].mediaCount);
    assert.ok(/^(iMessage )?Jordan: \u{1F4F7} Photo$/u.test(logs[0].quotedText), logs[0].quotedText);
    det.stop();
  });
  await test('plain emoji-only message (no reply) is valid text', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 3, { preview: false, text: HEART }));
    await sleep(150);
    assert.strictEqual(logs.length, 1);
    assert.strictEqual(logs[0].text, '\u2764\uFE0F');
    det.stop();
  });
  await test('a word image alt ("Image") is never read as text', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(xRow(doc, 4, { preview: false, text: 'hi ' + THUMB }));
    await sleep(150);
    assert.strictEqual(logs[0].text, 'hi');
    det.stop();
  });
  console.log(passed + ' passed, ' + failed + ' failed (v1.4.10 reply to a photo)');
  process.exit(failed ? 1 : 0);
})();
