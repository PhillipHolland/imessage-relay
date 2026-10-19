/* v1.4.3: text-free quote diagnostics (structure fingerprint on each outbound, popup page structure). Run:
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

function bgHarness() {
  const vm = require('vm');
  const bgListeners = [], hostCalls = [];
  const bag = {};
  const st = { async get(k) { const o = {}; (typeof k === 'string' ? [k] : (k || Object.keys(bag))).forEach(x => { if (x in bag) o[x] = bag[x]; }); return o; }, async set(o) { Object.assign(bag, o); }, async remove(k) { delete bag[k]; } };
  const chrome = {
    runtime: { id: 'ext', onMessage: { addListener: f => bgListeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
      sendNativeMessage: async (h, m) => { hostCalls.push(m); if (m.type === 'get_config') return { ok: true, paused: false, dryRun: true, relayConversationId: '111-222', ownerXHandle: 'OwnerAcct', senderXHandle: 'SenderBot', contacts: [{ name: 'Sam Alpha', label: 'Sam', handle: '+15555550123' }] }; return { ok: true, dryRun: true }; } },
    storage: { local: st, session: st, onChanged: { addListener() {} } },
    tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} }, query: async () => [] },
    alarms: { create() {}, clear() {}, onAlarm: { addListener() {} } },
    action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../extension/background.js'), 'utf8'), { chrome, console: { log() {}, warn() {} } });
  return { bgListeners, hostCalls };
}
const SECRET = ['Ha, true', 'Rory', 'Not since the jump', 'OwnerAcct', 'SenderBot', 'df48633d'];
const noText = (s, extra = []) => { for (const t of SECRET.concat(extra)) assert.ok(!String(s).includes(t), 'leaked ' + t + ': ' + s); };

(async () => {
  await test('outbound carries a text-free fingerprint; "Replied to" text is a flag only', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const m = msg(doc, 1, 'out', 'Ha, true');
    const pv = doc.createElement('div');
    pv.setAttribute('data-testid', 'reply-quote');
    pv.innerHTML = '<div><span>Replied to Rory</span></div><div role="button" aria-label="Not since the jump"><span>Rory: Not since the jump</span></div>';
    m.querySelector('div').prepend(pv);
    panel(doc).appendChild(m);
    await sleep(80);
    const fp = logs[0].quoteProbe;
    assert.ok(fp && fp.startsWith('E='), fp);
    assert.ok(fp.includes('!RT'), fp);
    assert.ok(fp.includes('@reply-quote'), fp);
    assert.ok(fp.includes('#button{aria-label}'), fp);
    assert.ok(!fp.includes('!RA'), fp);
    assert.ok(fp.includes('@message-<self>'), fp);
    noText(fp);
    det.stop();
  });
  await test('aria-label value is redacted except a "Replied to" flag', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const m = msg(doc, 2, 'out', 'Ha, true');
    m.setAttribute('aria-label', 'Replied to Rory: Not since the jump');
    panel(doc).appendChild(m);
    await sleep(80);
    const fp = logs[0].quoteProbe;
    assert.ok(/^E=0\.div@message-<self>\{[^}]*aria-label[^}]*\}!RA/.test(fp), fp);
    noText(fp);
    det.stop();
  });
  await test('preceding siblings recorded; a reference to another message is REF', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const row = doc.createElement('div');
    row.setAttribute('data-quoted-id', 'message-' + uuid(77));
    row.innerHTML = '<span>Rory: Not since the jump</span>';
    panel(doc).appendChild(row);
    panel(doc).appendChild(msg(doc, 3, 'out', 'Ha, true'));
    await sleep(80);
    const fp = logs[logs.length - 1].quoteProbe;
    assert.ok(/S-1=0\.div\{data-quoted-id\}!REF/.test(fp), fp);
    assert.ok(/S-2\(msg\)=0\.div@message-<uuid>/.test(fp), fp);
    noText(fp);
    det.stop();
  });
  await test('test ids holding a handle or digits are masked', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    const m = msg(doc, 4, 'out', 'Ha, true');
    m.querySelector('div').insertAdjacentHTML('afterbegin', '<div data-testid="UserAvatar-Container-OwnerAcct"></div><div data-testid="cell-5551234"></div>');
    panel(doc).appendChild(m);
    await sleep(80);
    const fp = logs[0].quoteProbe;
    assert.ok(fp.includes('@UserAvatar-Container-<x>'), fp);
    assert.ok(fp.includes('@cell-<x>'), fp);
    noText(fp, ['5551234']);
    det.stop();
  });
  await test('inbound messages carry no fingerprint', async () => {
    const { doc, det, logs } = setup();
    await sleep(200);
    panel(doc).appendChild(msg(doc, 5, 'in', 'Rory: Not since the jump'));
    await sleep(80);
    assert.ok(logs.length && !logs[0].quoteProbe);
    det.stop();
  });
  await test('popup page structure: last entries, structure only', async () => {
    const { doc, det } = setup();
    await sleep(200);
    panel(doc).appendChild(msg(doc, 6, 'in', 'Rory: Not since the jump'));
    panel(doc).appendChild(msg(doc, 7, 'out', 'Ha, true'));
    await sleep(80);
    const ps = det.pageStructure();
    assert.ok(ps.entries.length >= 2);
    assert.ok(ps.entries.every(e => typeof e.structure === 'string' && 'quoteSeen' in e));
    const js = JSON.stringify(ps);
    noText(js);
    assert.ok(!js.includes('earlier'));
    det.stop();
  });
  await test('background forwards the fingerprint (closed charset) to the Mac', async () => {
    const { bgListeners, hostCalls } = bgHarness();
    await sleep(30);
    const item = { ts: new Date().toISOString(), conversationId: '111-222', direction: 'outbound', text: 'Sam: hi', key: 'k1', score: 5, source: 'frame',
                   quoteProbe: 'E=0.div@message-<self> "quoted words" \u{1F600}' };
    bgListeners.forEach(f => f({ type: 'relay-log', item }, { id: 'ext', tab: { id: 1 } }, () => {}));
    await sleep(60);
    const s = hostCalls.find(c => c.type === 'send');
    assert.ok(s && s.quoteProbe, JSON.stringify(s));
    assert.ok(s.quoteProbe.startsWith('E=0.div@message-<self>'));
    assert.ok(!/["\u{1F600}]/u.test(s.quoteProbe), s.quoteProbe);
  });
  console.log(passed + ' passed, ' + failed + ' failed (v1.4.3 quote diagnostics)');
  process.exit(failed ? 1 : 0);
})();
