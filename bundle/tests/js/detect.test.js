/* jsdom test for content.js detection logic (v1.3.2: uuid-only). Run:
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
const fixture = fs.readFileSync(path.join(__dirname, 'fake-chat.html'), 'utf8');

(async () => {
  await test('messages present at load are not logged; new ones logged once with right direction + uuid key', async () => {
    const { doc, det, logs } = setup();
    await sleep(80);
    panel(doc).appendChild(msg(doc, 10, 'out', 'added during settle'));
    await sleep(200);
    assert.strictEqual(logs.length, 0, 'nothing from initial/settling content: ' + JSON.stringify(logs.map(l => l.text)));
    panel(doc).appendChild(msg(doc, 11, 'out', 'hello from owner'));
    await sleep(60);
    panel(doc).appendChild(msg(doc, 12, 'in', 'hi from sender bot'));
    await sleep(60);
    assert.deepStrictEqual(logs.map(l => [l.direction, l.text]), [['outbound', 'hello from owner'], ['inbound', 'hi from sender bot']]);
    assert.strictEqual(logs[0].conversationId, '111-222');
    assert.strictEqual(logs[0].key, '111-222|id:uuid=' + uuid(11));
    assert.ok(logs[0].signals[0] === 'entry:uuid:message-' + uuid(11));
    assert.ok(logs[0].signals.some(s => s.startsWith('align:+')), 'outbound align');
    assert.ok(logs[0].signals.some(s => s.startsWith('style:+')), 'outbound style');
    assert.ok(logs[1].signals.some(s => s.startsWith('handle:-')), 'inbound handle');
    for (const k of ['ts', 'conversationId', 'direction', 'text', 'signals']) assert.ok(k in logs[0], 'has ' + k);
    det.stop();
  });

  await test('re-render with the same uuid (even changed text) is not logged again; a new uuid is', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const m = msg(doc, 20, 'out', 'first');
    panel(doc).appendChild(m); await sleep(100);
    m.remove();
    panel(doc).appendChild(msg(doc, 20, 'out', 'first (edited)')); await sleep(100);
    panel(doc).appendChild(msg(doc, 21, 'out', 'first')); await sleep(100);   // new uuid, same text
    assert.deepStrictEqual(logs.map(l => l.text), ['first', 'first']);
    det.stop();
  });

  await test('history prepended at the top (scroll up) is not logged', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const vp = panel(doc);
    vp.insertBefore(msg(doc, 30, 'in', 'older history 1'), vp.firstChild); await sleep(60);
    vp.insertBefore(msg(doc, 31, 'out', 'older history 2'), vp.firstChild); await sleep(60);
    assert.strictEqual(logs.length, 0, JSON.stringify(logs.map(l => l.text)));
    det.stop();
  });

  await test('bulk batch (> maxNewPerBatch) is treated as history', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const frag = doc.createDocumentFragment();
    for (let i = 0; i < 5; i++) frag.appendChild(msg(doc, 40 + i, i % 2 ? 'in' : 'out', 'bulk ' + i));
    panel(doc).appendChild(frag);
    await sleep(60);
    assert.strictEqual(logs.length, 0);
    det.stop();
  });

  await test('pause, conversation filter, composer typing', async () => {
    const { doc, det, logs, state } = setup();
    await sleep(200);
    state.paused = true;
    panel(doc).appendChild(msg(doc, 50, 'out', 'while paused')); await sleep(60);
    state.paused = false;
    state.conversationFilter = 'https://x.com/i/chat/999-999';
    panel(doc).appendChild(msg(doc, 51, 'out', 'other conversation filter')); await sleep(60);
    state.conversationFilter = '111-222';
    doc.getElementById('composer').innerHTML = '<div data-testid="message-' + uuid(52) + '"><span data-testid="message-text-' + uuid(52) + '">draft typing</span></div>';
    await sleep(60);
    panel(doc).appendChild(msg(doc, 53, 'out', 'after unpause')); await sleep(60);
    assert.deepStrictEqual(logs.map(l => l.text), ['after unpause']);
    det.stop();
  });

  await test('owner-identity gate: when not confirmed, nothing is logged and wrong_account is reported', async () => {
    const { doc, det, logs, statuses } = setup(fixture, 'https://x.com/i/chat/111-222', { ownerCheck: () => false });
    await sleep(200);
    panel(doc).appendChild(msg(doc, 60, 'out', 'reply on the wrong account')); await sleep(60);
    assert.strictEqual(logs.length, 0, 'no emit while owner is not confirmed');
    assert.ok(statuses.some(s => s.status === 'wrong_account'), 'wrong_account surfaced');
    assert.ok(!JSON.stringify(statuses).includes('wrong account'), 'status carries no message body');
    det.stop();
  });

  await test('freshness: a bubble with an old time[datetime] is history; a fresh datetime sets ts', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(msg(doc, 70, 'out', 'stale by timestamp', { time: '2020-01-01T00:00:00Z' }));
    await sleep(60);
    assert.strictEqual(logs.length, 0, 'old timestamp treated as history');
    const fresh = new Date(Date.now() - 10000).toISOString();
    panel(doc).appendChild(msg(doc, 71, 'out', 'fresh', { time: fresh })); await sleep(60);
    assert.deepStrictEqual(logs.map(l => l.text), ['fresh']);
    assert.strictEqual(logs[0].ts, fresh, 'ts comes from the bubble time when fresh');
    det.stop();
  });

  await test('item 10: body read only from message-text-<uuid>, line breaks kept, time/status chips dropped', async () => {
    const { doc, det, logs } = setup(fixture, 'https://x.com/i/chat/1111-2222');
    await sleep(200);
    const vp = panel(doc);
    // multi-line body with <br> and block <div>, plus time/status chips that must be dropped
    vp.appendChild(msg(doc, 80, 'out', (span) => {
      span.innerHTML = 'line one<br>line two<div>line three</div><span data-testid="msg-timestamp">4:32 PM</span><span data-testid="read-receipt">Seen</span>';
    }));
    await sleep(60);
    assert.strictEqual(logs[0].text, 'line one\nline two\nline three');
    // a sibling [data-testid="message-text"] without the matching uuid must NOT be read
    const m81 = msg(doc, 81, 'out', 'the real body');
    const decoy = doc.createElement('span');
    decoy.setAttribute('data-testid', 'message-text');          // no uuid -> ignored
    decoy.textContent = 'DECOY should not appear';
    m81.querySelector('div').appendChild(decoy);
    vp.appendChild(m81);
    await sleep(60);
    assert.strictEqual(logs[1].text, 'the real body');
    det.stop();
  });

  await test('item 10: an over-limit reply is refused (owner notice), never truncated', async () => {
    const { doc, det, logs } = setup(fixture, 'https://x.com/i/chat/111-222', { config: { maxTextLength: 50 } });
    await sleep(200);
    const big = 'x'.repeat(200);
    panel(doc).appendChild(msg(doc, 90, 'out', big)); await sleep(80);
    assert.strictEqual(logs.length, 1);
    assert.strictEqual(logs[0].refuse, 'too-long');
    assert.strictEqual(logs[0].limit, 50);
    assert.strictEqual(logs[0].textLength, 200);
    assert.ok(!('text' in logs[0]), 'the over-limit body is not included');
    det.stop();
  });

  await test('cross-session persisted uuids are not emitted again', async () => {
    const seenSet = new Set([uuid(100)]);
    const { doc, det, logs } = setup(fixture, 'https://x.com/i/chat/111-222', { seenSet });
    await sleep(200);
    panel(doc).appendChild(msg(doc, 100, 'out', 'already relayed last session')); await sleep(60);
    panel(doc).appendChild(msg(doc, 101, 'out', 'new this session')); await sleep(60);
    assert.deepStrictEqual(logs.map(l => l.text), ['new this session']);
    assert.ok(seenSet.has(uuid(101)), 'newly relayed uuid is persisted');
    det.stop();
  });

  await test('SPA switch to another chat re-baselines (new chat history not logged)', async () => {
    const { doc, det, logs, dom } = setup();
    await sleep(200);
    dom.reconfigure({ url: 'https://x.com/i/chat/333-444' });
    await sleep(80);
    panel(doc).appendChild(msg(doc, 110, 'in', 'rendered right after switch')); await sleep(200);
    panel(doc).appendChild(msg(doc, 111, 'out', 'new in second chat')); await sleep(60);
    assert.deepStrictEqual(logs.map(l => [l.conversationId, l.text]), [['333-444', 'new in second chat']]);
    det.stop();
  });

  await test('reply-quote: quoted relay message is split off as quotedText, not part of the reply', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const m = msg(doc, 120, 'out', 'yes, 7 works');
    const q = doc.createElement('div');
    q.setAttribute('data-testid', 'reply-preview');
    q.innerHTML = '<span>SenderBot</span> <span>Alex R.: dinner at 7?</span>';
    m.querySelector('div').prepend(q);
    panel(doc).appendChild(m);
    await sleep(80);
    assert.strictEqual(logs.length, 1, 'entry with a quote is still one message');
    assert.strictEqual(logs[0].text, 'yes, 7 works');
    assert.strictEqual(logs[0].quotedText, 'SenderBot Alex R.: dinner at 7?');
    assert.ok(logs[0].signals.some(s => s.startsWith('quote:')));
    det.stop();
  });

  await test('relay chat filter: only relayConversationId is acted on', async () => {
    const { doc, det, logs, state } = setup(fixture, 'https://x.com/i/chat/111-222', { state: { conversationFilter: '333-444' } });
    await sleep(200);
    panel(doc).appendChild(msg(doc, 130, 'out', 'other chat')); await sleep(60);
    assert.strictEqual(logs.length, 0);
    state.conversationFilter = '111-222';
    panel(doc).appendChild(msg(doc, 131, 'out', 'relay chat')); await sleep(60);
    assert.deepStrictEqual(logs.map(l => l.text), ['relay chat']);
    det.stop();
  });

  await test('debug snapshot replaces message text with placeholder', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(msg(doc, 140, 'out', 'secret words here')); await sleep(60);
    const snap = det.debugSnapshot();
    const json = JSON.stringify(snap);
    assert.strictEqual(logs.length, 1);
    assert.ok(snap.primary && snap.primary.outerHTML.includes('[text]'), 'has placeholder');
    assert.ok(!json.includes('secret words here'), 'message text not leaked');
    assert.strictEqual(snap.primary.direction, 'outbound');
    assert.strictEqual(snap.mode, 'uuid');
    det.stop();
  });

  await test('template CONFIG ships with no names and uuid-only selectors', async () => {
    assert.deepStrictEqual(CONFIG.ownerNames, []);
    assert.deepStrictEqual(CONFIG.botNames, []);
    assert.strictEqual(CONFIG.logPrefix, '[imessage-relay]');
    assert.strictEqual(CONFIG.textTestIdPrefix, 'message-text-');
    assert.ok(CONFIG.uuidEntryTestIdRegex.test('message-aaaaaaaa-0000-4000-8000-000000000001'));
  });

  await test('v1.4.14: a bare "2" next to X\'s hidden + visible time is read as "2" (never "26:31 PM6:31 PM")', async () => {
    const { det, doc } = setup('<body></body>');
    const host = doc.createElement('div');
    host.innerHTML = '<div data-testid="message-text-U"><div><span><span dir="auto"><span>2</span></span><span aria-hidden="true"><div><span>6:31 PM</span><svg></svg></div></span></span><div><div><span>6:31 PM</span><svg><path d=""/></svg></div></div></div></div>';
    assert.strictEqual(det.extractText(host.firstChild), '2');
    host.innerHTML = '<div data-testid="message-text-U"><span>9:07 AM</span><span>9:07 AM</span></div>';
    assert.strictEqual(det.extractText(host.firstChild), '9:07 AM');
  });

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
