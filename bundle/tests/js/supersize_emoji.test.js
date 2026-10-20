/* v1.4.21: X draws a lone emoji outside message-text-<uuid> (a 64px glyph, or an img alt).
 * A native Reply's preview is the previous sibling of that glyph. The row's own justify-end
 * makes it outbound even with no layout. data-send-status=sent is not a direction signal.
 * Run: NODE_PATH=/path/to/node_modules node tests/js/supersize_emoji.test.js
 */
'use strict';
const assert = require('assert');
const { JSDOM } = require('jsdom');
const { createRelayDetector } = require('../../extension/content.js');

const sleep = ms => new Promise(r => setTimeout(r, ms));
const FAST = { settleMs: 150, batchDelayMs: 20, dupWindowMs: 60, emptyRetryDelayMs: 40, urlPollMs: 50, ownerNames: ['OwnerAcct'], botNames: ['SenderBot'] };
const ROCKET = '\u{1F680}';
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('PASS', name); }
  catch (e) { failed++; console.log('FAIL', name, '\n   ', e.message); }
}
const uuid = n => `df48633d-6588-4b18-bc77-${String(n).padStart(12, '0')}`;

function setup(html) {
  const dom = new JSDOM(html, { url: 'https://x.com/i/chat/111-222', pretendToBeVisual: true });
  const { window } = dom;
  window.Element.prototype.getBoundingClientRect = function () {
    const v = this.getAttribute('data-fake-rect');
    if (!v) return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
    const [l, t, w, h] = v.split(',').map(Number);
    return { left: l, top: t, width: w, height: h, right: l + w, bottom: t + h };
  };
  const logs = [];
  const det = createRelayDetector({
    window, document: window.document, config: FAST, getState: () => ({ paused: false, conversationFilter: '' }),
    onEmit: i => logs.push(i), onStatus: () => {}, log: () => {},
    seen: { has: () => false, add: () => {} },
  });
  det.start();
  return { doc: window.document, det, logs };
}

// The live own-row shape: justify-end on message-<uuid>, an absolute overlay, a hidden "You"
// label, then the right-hand column. No role=article and no message-text-<uuid>.
function jumboRow(doc, n, opts) {
  const row = doc.createElement('div');
  row.setAttribute('data-testid', 'message-' + uuid(n));
  row.setAttribute('data-send-status', 'sent');
  row.classList.add(opts.inbound ? 'justify-start' : 'justify-end');
  if (!opts.bare) {
    row.style.display = 'flex';
    row.style.justifyContent = opts.inbound ? 'flex-start' : 'flex-end';
    row.setAttribute('data-fake-rect', '0,0,1000,80');
  }
  const overlay = doc.createElement('div');
  overlay.setAttribute('aria-hidden', 'true');
  if (!opts.bare) overlay.setAttribute('data-fake-rect', '0,0,1000,80');
  const you = doc.createElement('span');
  you.hidden = true;
  you.textContent = 'You';
  const clock = doc.createElement('span');
  clock.hidden = true;
  clock.textContent = '11:20 PM';
  const grid = doc.createElement('div');
  if (!opts.bare) grid.setAttribute('data-fake-rect', opts.inbound ? '40,0,500,80' : '450,0,500,80');
  const col = doc.createElement('div');
  if (!opts.bare) {
    col.style.display = 'flex';
    col.style.flexDirection = 'column';
    col.style.alignItems = opts.inbound ? 'flex-start' : 'flex-end';
    col.setAttribute('data-fake-rect', opts.inbound ? '40,0,500,80' : '450,0,500,80');
  }
  if (opts.quoted) {
    const pv = doc.createElement('span');
    pv.textContent = opts.quoted;
    col.appendChild(pv);
  }
  const wrap = doc.createElement('div');
  if (!opts.bare) wrap.setAttribute('data-fake-rect', opts.inbound ? '40,40,80,40' : '860,40,80,40');
  const glyph = doc.createElement('span');
  glyph.style.fontSize = '64px';
  if (!opts.bare) glyph.setAttribute('data-fake-rect', opts.inbound ? '40,40,64,64' : '860,40,64,64');
  if (opts.img) {
    const img = doc.createElement('img');
    img.alt = ROCKET;
    img.src = 'https://abs.twimg.com/emoji/v2/svg/1f680.svg';
    glyph.appendChild(img);
  } else glyph.textContent = ROCKET;
  wrap.appendChild(glyph);
  col.appendChild(wrap);
  grid.appendChild(col);
  row.append(overlay, you, clock, grid);
  return row;
}

const EARLIER = '<div data-testid="message-df48633d-6588-4b18-bc77-000000009999"><span data-testid="message-text-df48633d-6588-4b18-bc77-000000009999">earlier</span></div>';
const WIDE = '<html><body><div data-testid="dm-conversation-panel" data-fake-rect="0,0,1000,800">' + EARLIER + '</div></body></html>';
const BARE = '<html><body><div data-testid="dm-conversation-panel">' + EARLIER + '</div></body></html>';
const panel = doc => doc.querySelector('[data-testid="dm-conversation-panel"]');

(async () => {
  await test('a lone emoji native Reply is the body, the contact preview is the quote, and the own row is outbound', async () => {
    const laid = setup(WIDE);
    await sleep(200);
    panel(laid.doc).appendChild(jumboRow(laid.doc, 1, { quoted: 'Alex: on my way' }));
    await sleep(80);
    assert.strictEqual(laid.logs.length, 1);
    const own = laid.logs[0];
    assert.strictEqual(own.text, ROCKET);
    assert.ok(/Alex: on my way/.test(own.quotedText || ''), JSON.stringify(own.quotedText));
    assert.ok(!/You|11:20/.test(own.quotedText || ''), 'hidden label is not the quote');
    assert.strictEqual(own.direction, 'outbound');
    assert.ok(own.score >= 3, 'score ' + own.score + ' ' + own.signals.join(' '));
    assert.ok(own.signals.some(s => s.indexOf('row:+3') === 0), own.signals.join(' '));
    laid.det.stop();

    const img = setup(WIDE);
    await sleep(200);
    panel(img.doc).appendChild(jumboRow(img.doc, 2, { quoted: 'Alex: on my way', img: true }));
    await sleep(80);
    assert.strictEqual(img.logs.length, 1);
    assert.strictEqual(img.logs[0].text, ROCKET);
    assert.ok(/Alex: on my way/.test(img.logs[0].quotedText || ''));
    assert.strictEqual(img.logs[0].direction, 'outbound');
    assert.ok(img.logs[0].score >= 3, img.logs[0].signals.join(' '));
    img.det.stop();

    // No rects and no inline justify: the justify-end class alone must still clear the host's bar.
    const bare = setup(BARE);
    await sleep(200);
    panel(bare.doc).appendChild(jumboRow(bare.doc, 3, { quoted: 'Alex: on my way', bare: true }));
    await sleep(80);
    assert.strictEqual(bare.logs.length, 1);
    assert.strictEqual(bare.logs[0].text, ROCKET);
    assert.ok(/Alex: on my way/.test(bare.logs[0].quotedText || ''));
    assert.strictEqual(bare.logs[0].direction, 'outbound');
    assert.ok(bare.logs[0].score >= 3, bare.logs[0].signals.join(' '));
    assert.ok(bare.logs[0].signals.some(s => s.indexOf('row:+3') === 0));
    bare.det.stop();

    // data-send-status=sent is on received rows too. justify-start stays inbound.
    const inbound = setup(WIDE);
    await sleep(200);
    panel(inbound.doc).appendChild(jumboRow(inbound.doc, 4, { inbound: true }));
    await sleep(80);
    assert.strictEqual(inbound.logs.length, 1);
    assert.strictEqual(inbound.logs[0].text, ROCKET);
    assert.strictEqual(inbound.logs[0].direction, 'inbound');
    assert.ok(inbound.logs[0].score <= -3, inbound.logs[0].signals.join(' '));
    inbound.det.stop();
  });
  console.log(passed + ' passed, ' + failed + ' failed (v1.4.21 supersize emoji)');
  process.exit(failed ? 1 : 0);
})();
