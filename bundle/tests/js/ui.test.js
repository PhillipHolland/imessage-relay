/* v1.4.6: the UI engine (reactions on the owner's own messages, typed 🤖 prompts, undo), on a fake X Chat UI. Run:
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
let msg = function (doc, n, dir, build, opts = {}) {
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
};
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


const msgBase = msg;
// outbound bubbles carry a "sent" testid token (as real X markup has strong outbound signals)
msg = function (doc, n, dir, build, opts) { const e = msgBase(doc, n, dir, build, opts); if (dir === 'out') e.firstChild.setAttribute('data-testid', 'bubble-sent'); return e; };
const CHECK = '\u2705', QM = '\u2753', ROBOT = '\u{1F916}';
// A fake X Chat UI: hovering a bubble shows an action bar; "React" opens a picker; picking an emoji adds a
// reaction pill. Dangerous buttons record clicks so a test can prove they were never touched.
function fakeUi(doc, opts = {}) {
  const clicks = [];
  const W = doc.defaultView;
  doc.addEventListener('mouseover', ev => {
    const e = ev.target.closest && ev.target.closest('[data-testid^="message-"]');
    if (!e || e.getAttribute('data-testid').startsWith('message-text-') || e.querySelector('.bar')) return;
    const bar = doc.createElement('div'); bar.className = 'bar';
    const mk = (label, fn) => { const b = doc.createElement('button'); b.setAttribute('aria-label', label); b.addEventListener('click', () => { clicks.push(label); fn && fn(); }); bar.appendChild(b); return b; };
    mk('Reply'); mk('Delete'); mk('More');
    if (!opts.noReact) mk(opts.reactLabel || 'React', () => openPicker(e));
    e.appendChild(bar);
  }, true);
  function openPicker(entry) {
    const p = doc.createElement('div'); p.setAttribute('role', 'dialog'); p.className = 'picker';
    const opt = em => { const b = doc.createElement('button'); b.textContent = em; b.addEventListener('click', () => { clicks.push('pick:' + em); p.remove(); if (!opts.noPlace) { const pill = doc.createElement('div'); pill.setAttribute('data-testid', 'reactions'); const s = doc.createElement('span'); s.textContent = em; pill.appendChild(s); entry.appendChild(pill); } }); p.appendChild(b); };
    ['\u{1F44D}', '\u2764\uFE0F', '\u{1F602}'].forEach(opt);
    if (!opts.searchOnly) opt(CHECK);
    const inp = doc.createElement('input'); inp.setAttribute('type', 'search');
    inp.addEventListener('input', () => { if (/check/.test(inp.value) && !p.querySelector('.found')) { opt(CHECK); p.lastChild.className = 'found'; } if (/question/.test(inp.value)) opt(QM); });
    p.appendChild(inp);
    doc.body.appendChild(p);
  }
  return { clicks };
}
function composer(doc, opts = {}) {
  const wrap = doc.createElement('div');
  const ta = doc.createElement('textarea'); ta.setAttribute('data-testid', 'dm-composer-textarea');
  wrap.appendChild(ta);
  let n = 900;
  ta.addEventListener('input', () => {
    let b = doc.querySelector('[data-testid="dm-composer-send-button"]');
    if (ta.value && !b && !opts.noButton) {
      b = doc.createElement('button'); b.setAttribute('data-testid', 'dm-composer-send-button');
      b.addEventListener('click', () => { const v = ta.value; ta.value = ''; b.remove(); if (!opts.swallow) panel(doc).appendChild(msg(doc, ++n, 'out', v)); });
      wrap.appendChild(b);
    }
  });
  doc.body.appendChild(wrap);
  return ta;
}

(async () => {
  await test('✅ lands on the owner\'s own message via the React control; nothing else is clicked', async () => {
    const { doc, det, reacts, logs } = setup();
    const ui = fakeUi(doc);
    await sleep(200);
    const e = msg(doc, 1, 'out', 'on my way'); panel(doc).appendChild(e);
    await sleep(300);
    const r = await det.uiReact(uuid(1), CHECK);
    assert.strictEqual(r.ok, true, JSON.stringify(r));
    assert.strictEqual(r.step, 'placed');
    assert.deepStrictEqual(ui.clicks, ['React', 'pick:' + CHECK]);
    await sleep(300);
    assert.strictEqual(reacts.length, 0, 'its own ✅ is never input');
    assert.ok(logs.every(l => !String(l.text || '').includes(CHECK)), 'a reaction is never a message');
  });

  await test('search fallback finds ✅ when the quick row lacks it', async () => {
    const { doc, det } = setup();
    const ui = fakeUi(doc, { searchOnly: true });
    await sleep(200);
    panel(doc).appendChild(msg(doc, 2, 'out', 'see you'));
    await sleep(100);
    const r = await det.uiReact(uuid(2), CHECK);
    assert.strictEqual(r.ok, true, JSON.stringify(r));
    assert.ok(ui.clicks.includes('pick:' + CHECK));
  });

  await test('no React control -> clean failure, nothing clicked, text-free vocabulary only', async () => {
    const { doc, det } = setup();
    const ui = fakeUi(doc, { noReact: true });
    await sleep(200);
    panel(doc).appendChild(msg(doc, 3, 'out', 'private words here'));
    await sleep(100);
    const r = await det.uiReact(uuid(3), CHECK);
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.step, 'no-react-button');
    assert.deepStrictEqual(ui.clicks, []);
    assert.ok(!JSON.stringify(r).includes('private'));
    assert.ok(r.vocab.includes('delete') && r.vocab.includes('reply'));
  });

  await test('a label like "Remove reaction" or "More reactions" is never used', async () => {
    const { doc, det } = setup();
    const ui = fakeUi(doc, { reactLabel: 'Remove reaction' });
    await sleep(200);
    panel(doc).appendChild(msg(doc, 4, 'out', 'hi'));
    await sleep(100);
    const r = await det.uiReact(uuid(4), CHECK);
    assert.strictEqual(r.ok, false);
    assert.deepStrictEqual(ui.clicks, []);
  });

  await test('never reacts on someone else\'s (inbound) message; already-reacted is a no-op', async () => {
    const { doc, det } = setup();
    const ui = fakeUi(doc);
    await sleep(200);
    panel(doc).appendChild(msg(doc, 5, 'in', 'Jordan: hello'));
    const mine = msg(doc, 6, 'out', 'hey'); panel(doc).appendChild(mine);
    react(doc, mine, CHECK);
    await sleep(100);
    assert.strictEqual((await det.uiReact(uuid(5), CHECK)).step, 'not-mine');
    assert.strictEqual((await det.uiReact(uuid(6), CHECK)).step, 'already');
    assert.strictEqual((await det.uiReact('bad', CHECK)).step, 'no-entry');
    assert.deepStrictEqual(ui.clicks, []);
  });

  await test('picked but not placed -> failure (so the Mac posts the text line)', async () => {
    const { doc, det } = setup();
    fakeUi(doc, { noPlace: true });
    await sleep(200);
    panel(doc).appendChild(msg(doc, 7, 'out', 'hey'));
    await sleep(100);
    const r = await det.uiReact(uuid(7), CHECK);
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.step, 'not-placed');
  });

  await test('❓ on his message makes it a prompt: his new 👍 confirms once; ✅ afterwards ends it', async () => {
    const { doc, det, reacts } = setup();
    fakeUi(doc);
    await sleep(200);
    const e = msg(doc, 8, 'out', 'see you soon'); panel(doc).appendChild(e);
    await sleep(100);
    const r = await det.uiReact(uuid(8), QM, { prompt: true });
    assert.strictEqual(r.ok, true, JSON.stringify(r));
    await sleep(200);
    assert.strictEqual(reacts.length, 0, 'the ❓ itself is not an answer');
    react(doc, e, '\u{1F44D}');
    await sleep(250);
    assert.deepStrictEqual(reacts.map(x => [x.word, x.key]), [['yes', 'reaction:' + uuid(8)]]);
    // a later mirrored 👍 (their tapback) on the same message is never another "yes"
    const e2 = msg(doc, 9, 'out', 'again'); panel(doc).appendChild(e2);
    await sleep(100);
    await det.uiReact(uuid(9), QM, { prompt: true });
    await det.uiReact(uuid(9), CHECK);            // sent -> no longer a prompt
    react(doc, e2, '\u{1F44D}');
    await sleep(250);
    assert.strictEqual(reacts.length, 1);
  });

  await test('typed prompt: only 🤖-tagged text, only into an empty composer, and never relayed', async () => {
    const { doc, det, logs } = setup();
    const ta = composer(doc);
    await sleep(200);
    const r = await det.uiType(ROBOT + ' Who\u2019s this for? Start with a name, like "Jordan: \u2026"');
    assert.strictEqual(r.ok, true, JSON.stringify(r));
    assert.strictEqual(r.step, 'sent');
    await sleep(300);
    assert.strictEqual(logs.length, 0, 'the relay never treats its own prompt as a reply');
    assert.strictEqual((await det.uiType('Jordan: hi')).step, 'untagged');
    ta.value = 'half-typed draft';
    assert.strictEqual((await det.uiType(ROBOT + ' x')).step, 'composer-busy');
    assert.strictEqual(ta.value, 'half-typed draft', 'his draft is untouched');
  });

  await test('typed prompt: no send button -> failure and the composer is cleared back', async () => {
    const { doc, det } = setup();
    const ta = composer(doc, { noButton: true });
    await sleep(200);
    const r = await det.uiType(ROBOT + ' Who\u2019s this for?');
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.step, 'no-send-button');
    assert.strictEqual(ta.value, '');
  });

  await test('undo window: a 🗑️ on his own message is seen; none means go', async () => {
    const { doc, det } = setup();
    await sleep(200);
    const e = msg(doc, 10, 'out', 'oops wrong person'); panel(doc).appendChild(e);
    await sleep(100);
    setTimeout(() => react(doc, e, '\u{1F5D1}\uFE0F'), 300);
    const t0 = Date.now();
    const r = await det.uiAwaitUndo(uuid(10), 3000);
    assert.strictEqual(r.trashed, true);
    assert.ok(Date.now() - t0 < 1500, 'stops as soon as it is seen');
    const e2 = msg(doc, 11, 'out', 'fine'); panel(doc).appendChild(e2);
    await sleep(100);
    assert.strictEqual((await det.uiAwaitUndo(uuid(11), 400)).trashed, false);
  });

  await test('the 🗑️ on his own message is never treated as a block request', async () => {
    const { doc, reacts } = setup();
    await sleep(200);
    const e = msg(doc, 12, 'out', 'oops'); panel(doc).appendChild(e);
    await sleep(150);
    react(doc, e, '\u{1F5D1}\uFE0F');
    await sleep(250);
    assert.strictEqual(reacts.length, 0);
  });

  console.log(`${passed} passed, ${failed} failed (v1.4.6 UI engine)`);
  process.exit(failed ? 1 : 0);
})();
