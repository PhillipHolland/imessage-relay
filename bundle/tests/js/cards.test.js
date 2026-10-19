/* v1.4.6: a shared X post / link card in HIS bubble is not a reply quote; its canonical URL goes out as the
 * iMessage text after anything he typed. The fixture follows the real X Chat card markup recorded by the
 * text-free quote_probe on 2026-10-19 (row div > aria-hidden labels > role=group card [data-side > a[href,
 * target] > thumbnail img[srcset]] + role=article [message-text > spans + time/status chips]).
 * Fictional names only. */
'use strict';
const assert = require('assert');
const { JSDOM } = require('jsdom');
const { createRelayDetector } = require('../../extension/content.js');
const sleep = ms => new Promise(r => setTimeout(r, ms));
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('PASS', name); }
  catch (e) { failed++; console.log('FAIL', name, '\n   ', e.message); }
}
const uuid = n => `df48633d-6588-4b18-bc77-${String(n).padStart(12, '0')}`;
const FAST = { settleMs: 150, batchDelayMs: 20, dupWindowMs: 60, emptyRetryDelayMs: 40, urlPollMs: 50, ownerNames: ['OwnerAcct'], botNames: ['SenderBot'] };
const POST = 'https://x.com/ExampleNews/status/1850000000000000001';

// The real structure (probe fingerprint), with fictional text. `typed` null = no message-text element.
function cardMsg(doc, n, { typed = 'Jordan:', href = '/ExampleNews/status/1850000000000000001/photo/1', thumb = true } = {}) {
  const u = uuid(n);
  const row = doc.createElement('div');
  row.setAttribute('data-testid', 'message-' + u);
  row.setAttribute('data-dm-message-row', '');
  row.setAttribute('data-send-status', 'sent');
  row.innerHTML = `
    <div aria-hidden="true"><span hidden id="l1-${n}">You</span><span hidden id="l2-${n}">Shared a post</span><span hidden id="l3-${n}">Sent</span></div>
    <div data-fake-rect="780,0,200,120" data-testid="bubble-sent">
      <div></div>
      <div>
        <div role="group" tabindex="0" data-dm-message-focus-target="" aria-labelledby="l1-${n}">
          <div data-side="right"><div>
            <a href="${href}" target="_blank"><div>
              <div>${thumb ? '<img draggable="false" alt="Image" decoding="async" loading="lazy" sizes="200px" srcset="https://pbs.twimg.com/media/EXAMPLE?format=jpg 1x" src="https://pbs.twimg.com/media/EXAMPLE?format=jpg&name=small">' : ''}</div>
              <div><div id="c-${n}">Example News</div><div>@ExampleNews</div><div>City council approves the new park plan</div></div>
            </div><div><div>x.com</div><div>Oct 19</div></div></a>
          </div></div>
        </div>
        ${typed === null ? '' : `<div role="article" tabindex="0" data-dm-message-focus-target="" aria-labelledby="l1-${n}" aria-describedby="l3-${n}">
          <div data-testid="message-text-${u}" id="t-${n}"><div><span><span dir="auto"><span>${typed}</span></span>
            <span aria-hidden="true"><div><span>3:11 PM</span><svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true"></svg></div></span></span>
            <div><div><span>3:11 PM</span><svg aria-hidden="true"><path d="M0"></path></svg></div></div></div>
          </div>
        </div>`}
      </div>
    </div>`;
  doc.querySelector('[data-testid="dm-conversation-panel"]').appendChild(row);
  return row;
}
function plain(doc, n, text) {
  const e = doc.createElement('div');
  e.setAttribute('data-testid', 'message-' + uuid(n));
  e.innerHTML = `<div data-fake-rect="780,0,200,40" data-testid="bubble-sent"><span data-testid="message-text-${uuid(n)}">${text}</span></div>`;
  doc.querySelector('[data-testid="dm-conversation-panel"]').appendChild(e);
}
async function setup() {
  const dom = new JSDOM('<html><body><div data-testid="dm-conversation-panel"></div></body></html>', { url: 'https://x.com/i/chat/111-222', pretendToBeVisual: true });
  const { window } = dom;
  window.Element.prototype.getBoundingClientRect = function () {
    const v = this.getAttribute('data-fake-rect');
    if (!v) return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
    const [l, t, w, h] = v.split(',').map(Number);
    return { left: l, top: t, width: w, height: h, right: l + w, bottom: t + h };
  };
  const logs = [];
  plain(window.document, 900, 'older history');
  const det = createRelayDetector({ window, document: window.document, config: FAST, getState: () => ({ paused: false, conversationFilter: '' }),
    onEmit: i => logs.push(i), onStatus: () => {}, log: () => {}, onReaction: () => {}, seen: { has: () => false, add: () => {} } });
  det.start();
  await sleep(200);
  return { doc: window.document, det, logs };
}

(async () => {
  await test('"Jordan:" + a shared post card -> the post URL to Jordan; not a quote; no photo', async () => {
    const s = await setup();
    cardMsg(s.doc, 1);
    await sleep(300);
    const it = s.logs.find(l => l.key && l.key.endsWith(uuid(1)));
    assert.ok(it, 'emitted');
    assert.strictEqual(it.direction, 'outbound');
    assert.strictEqual(it.text, 'Jordan: ' + POST);
    assert.ok(!it.quotedText, 'the card is not a native quote: ' + it.quotedText);
    assert.strictEqual(it.cards, 1);
    assert.ok(!it.mediaCount, 'the card thumbnail is not his photo');
  });

  await test('a card shared with no text of his own -> just the URL', async () => {
    const s = await setup();
    cardMsg(s.doc, 2, { typed: null });
    await sleep(400);
    const it = s.logs.find(l => l.key && l.key.endsWith(uuid(2)));
    assert.ok(it, 'emitted');
    assert.strictEqual(it.text, POST);
  });

  await test('a link card -> its URL, on its own line after longer text', async () => {
    const s = await setup();
    cardMsg(s.doc, 3, { typed: 'Sam: this is the article about the new bike lanes downtown', href: 'https://example.com/news/bike-lanes?ref=x', thumb: false });
    await sleep(300);
    const it = s.logs.find(l => l.key && l.key.endsWith(uuid(3)));
    assert.strictEqual(it.text, 'Sam: this is the article about the new bike lanes downtown\nhttps://example.com/news/bike-lanes?ref=x');
  });

  await test('a URL he typed himself is not added twice', async () => {
    const s = await setup();
    cardMsg(s.doc, 4, { typed: 'Alex: ' + POST });
    await sleep(300);
    const it = s.logs.find(l => l.key && l.key.endsWith(uuid(4)));
    assert.strictEqual(it.text, 'Alex: ' + POST);
  });

  await test('canonical URLs', async () => {
    const s = await setup();
    const c = s.det._canonicalCardUrl;
    assert.strictEqual(c('/ExampleNews/status/1850000000000000001/photo/1'), POST);
    assert.strictEqual(c('https://twitter.com/ExampleNews/status/1850000000000000001?s=20'), POST);
    assert.strictEqual(c('https://mobile.x.com/ExampleNews/statuses/1850000000000000001'), POST);
    assert.strictEqual(c('/ExampleNews'), '', 'a profile link is not a card');
    assert.strictEqual(c('https://x.com/i/chat/111-222'), '');
    assert.strictEqual(c('javascript:alert(1)'), '');
    assert.strictEqual(c('https://t.co/abc123'), 'https://t.co/abc123');
  });

  console.log(`${passed} passed, ${failed} failed (v1.4.6 shared post / link cards)`);
  process.exit(failed ? 1 : 0);
})();
