/* v1.3.2 frame model: no top-frame "reach into the iframe" fallback. The content script is injected
 * into the top frame AND into same-origin about:srcdoc / about:blank child frames (manifest
 * all_frames + match_about_blank); each injected frame runs ONE detector on its OWN document and
 * takes the conversation id from the top URL (contextUrl). Runs the real content.js + background.js
 * with a fake `chrome`. jsdom ignores srcdoc, so the child frame's document is filled in by hand.
 *   NODE_PATH=/path/to/node_modules node tests/js/frames.test.js
 */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { JSDOM } = require('jsdom');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const CONTENT = fs.readFileSync(path.join(__dirname, '../../extension/content.js'), 'utf8');
const BACKGROUND = fs.readFileSync(path.join(__dirname, '../../extension/background.js'), 'utf8');
const CONV = '1111-2222';
const SETTLE = 3100;
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('PASS', name); }
  catch (e) { failed++; console.log('FAIL', name, '\n   ', e.stack.split('\n').slice(0, 3).join(' | ')); }
}

function makeEnv() {
  const store = {}, storageListeners = [], bgListeners = [];
  const storageApi = () => ({
    async get(keys) { const ks = typeof keys === 'string' ? [keys] : keys; const o = {}; ks.forEach(k => { if (k in store) o[k] = JSON.parse(JSON.stringify(store[k])); }); return o; },
    async set(v) { const ch = {}; for (const k in v) { ch[k] = { oldValue: store[k], newValue: v[k] }; store[k] = v[k]; } storageListeners.forEach(l => l(ch, 'local')); },
  });
  const bgLogs = [], nativeCalls = [];
  const hostConfig = { ok: true, version: '1.3.2', paused: false, dryRun: true, relayConversationId: CONV, ownerXHandle: 'OwnerAcct', senderXHandle: 'SenderBot', contacts: [{ name: 'Test Contact', label: 'Test', handle: '+15555550123' }] };
  const bgChrome = {
    runtime: {
      id: 'ext', onMessage: { addListener: f => bgListeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
      sendNativeMessage: async (host, msg) => { nativeCalls.push(msg); return msg.type === 'get_config' ? hostConfig : { ok: true, dryRun: true }; },
    },
    storage: { local: storageApi(), onChanged: { addListener: f => storageListeners.push(f) } },
    action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
    tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} }, query: async () => [] },
    alarms: { create() {}, onAlarm: { addListener: f => (globalThis.__alarm = f) } },
  };
  vm.runInNewContext(BACKGROUND, { chrome: bgChrome, console: { log: (...a) => bgLogs.push(a.join(' ')), warn() {} } });
  const csListeners = [];
  function chromeFor(frameId) {
    const sender = { id: 'ext', tab: { id: 1 }, frameId };
    return {
      runtime: {
        id: 'ext',
        sendMessage: msg => new Promise(res => {
          let done = false, isAsync = false;
          const sr = r => { if (!done) { done = true; res(r); } };
          bgListeners.forEach(f => { if (f(msg, sender, sr) === true) isAsync = true; });
          if (!isAsync && !done) res(undefined);
        }),
        onMessage: { addListener: f => csListeners.push(f) },
      },
      storage: { local: storageApi(), onChanged: { addListener: f => storageListeners.push(f) } },
    };
  }
  // boot() is wired to listeners that never fire in the harness; prime the host config once so
  // ownerNames reach storage (the owner-identity gate needs them) exactly like the real boot().
  const prime = () => new Promise(res => bgListeners.forEach(f => { if (f({ type: 'relay-status' }, { id: 'ext' }, res) !== true) res(); }));
  return { store, bgLogs, nativeCalls, chromeFor, csListeners, prime };
}

function stubRects(w) {
  w.Element.prototype.getBoundingClientRect = function () {
    const v = this.getAttribute('data-fake-rect');
    if (!v) return { left: 0, top: 0, width: 0, height: 0 };
    const [l, t, wd, h] = v.split(',').map(Number); return { left: l, top: t, width: wd, height: h };
  };
}

let U = 0;
const uuid = () => `df48633d-6588-4b18-bc77-${String(++U).padStart(12, '0')}`;
// The owner account-switcher avatar is the owner-identity proof; it lives in the TOP document.
const TOP_HTML = `<nav data-testid="AppTabBar_Home_Link">nav</nav>
<div data-testid="UserAvatar-Container-OwnerAcct"></div>
<div data-testid="primaryColumn"><div data-testid="xchatEmbedRoute"><iframe title="chat"></iframe></div></div>`;

function chatHtml(prefix) {
  const a = uuid(), b = uuid();
  return `<div data-testid="dm-conversation-panel" style="overflow-y:auto" data-fake-rect="0,0,600,800">
    <div data-testid="message-${a}"><a href="/SenderBot"><img alt="SenderBot"></a><div data-fake-rect="20,0,200,40"><span data-testid="message-text-${a}">${prefix} old inbound</span></div></div>
    <div data-testid="message-${b}"><div data-fake-rect="380,50,200,40"><span data-testid="message-text-${b}">${prefix} old outbound</span></div></div>
  </div>
  <div data-testid="dmComposer"><div contenteditable="true" role="textbox"></div></div>`;
}
function addMsg(d, out, text, root) {
  const vp = (root || d).querySelector('[data-testid="dm-conversation-panel"]');
  const id = uuid();
  const e = d.createElement('div');
  e.setAttribute('data-testid', 'message-' + id);
  e.style.display = 'flex';
  e.style.justifyContent = out ? 'flex-end' : 'flex-start';
  e.innerHTML = (out ? '' : '<a href="/SenderBot"><img alt="SenderBot"></a>') + `<div data-fake-rect="${out ? 380 : 20},0,200,40"><span data-testid="message-text-${id}"></span></div>`;
  e.querySelector('span').textContent = text;
  vp.appendChild(e);
}
const sent = env => env.nativeCalls.filter(m => m.type === 'send').map(m => [m.text, m.conversationId]);

(async () => {
  await test('chat in the TOP document: owner reply forwarded once, inbound never, conv id from URL', async () => {
    const env = makeEnv();
    const dom = new JSDOM(`<nav data-testid="SideNav"></nav><div data-testid="UserAvatar-Container-OwnerAcct"></div>` + chatHtml('T'), { url: 'https://x.com/i/chat/' + CONV, runScripts: 'outside-only' });
    const w = dom.window; stubRects(w);
    w.console = { log() {}, warn() {} };
    w.chrome = env.chromeFor(0);
    await env.prime();
    w.eval(CONTENT);
    await sleep(SETTLE);
    addMsg(w.document, true, 'owner reply top'); await sleep(400);
    addMsg(w.document, false, 'bot reply top'); await sleep(400);
    assert.deepStrictEqual(sent(env), [['owner reply top', CONV]], 'outbound once; inbound never');
    assert.ok(!JSON.stringify(env.store).includes('owner reply'), 'no bodies in storage');
    w.close();
  });

  await test('chat inside a same-origin child frame: the frame instance forwards; top stays silent; conv id from top URL', async () => {
    const env = makeEnv();
    const dom = new JSDOM(TOP_HTML, { url: 'https://x.com/i/chat/' + CONV, runScripts: 'outside-only' });
    const w = dom.window; stubRects(w);
    w.console = { log() {}, warn() {} };
    w.chrome = env.chromeFor(0);
    await env.prime();
    w.eval(CONTENT);                                   // top frame: top doc has no messages
    const iframe = w.document.querySelector('iframe');
    const fw = iframe.contentWindow; stubRects(fw);
    iframe.contentDocument.body.innerHTML = chatHtml('F');
    fw.console = { log() {}, warn() {} };
    fw.chrome = env.chromeFor(7);                      // the child frame instance (frameId 7)
    fw.eval(CONTENT);
    await sleep(SETTLE);
    addMsg(iframe.contentDocument, true, 'owner reply frame'); await sleep(400);
    addMsg(iframe.contentDocument, false, 'bot reply frame'); await sleep(400);
    assert.deepStrictEqual(sent(env), [['owner reply frame', CONV]], 'frame forwards; conv id from top URL');
    w.close();
  });

  await test('owner-identity gate: a different account in the top doc means nothing is forwarded', async () => {
    const env = makeEnv();
    // top doc carries SOMEONE ELSE's avatar, not the owner's
    const dom = new JSDOM(`<div data-testid="UserAvatar-Container-SomeoneElse"></div>` + chatHtml('W'), { url: 'https://x.com/i/chat/' + CONV, runScripts: 'outside-only' });
    const w = dom.window; stubRects(w);
    w.console = { log() {}, warn() {} };
    w.chrome = env.chromeFor(0);
    await env.prime();
    w.eval(CONTENT);
    await sleep(SETTLE);
    addMsg(w.document, true, 'reply on wrong account'); await sleep(400);
    assert.deepStrictEqual(sent(env), [], 'nothing forwarded off the owner account');
    assert.strictEqual(env.store.status.wrongAccount, true, 'wrong_account surfaced for the popup');
    assert.ok(!JSON.stringify(env.store).includes('wrong account'), 'no message body stored');
    w.close();
  });

  await test('background dedupes the same uuid key even if two frame instances report it', async () => {
    const env = makeEnv();
    const c = env.chromeFor(7);
    const item = { ts: new Date().toISOString(), conversationId: CONV, direction: 'outbound', text: 'dup', signals: [], score: 3, key: CONV + '|id:uuid=' + uuid(), source: 'frame' };
    await env.prime();
    await c.runtime.sendMessage({ type: 'relay-log', item });
    await c.runtime.sendMessage({ type: 'relay-log', item: Object.assign({}, item, { source: 'top' }) });
    await sleep(150);
    assert.strictEqual(sent(env).length, 1);
    assert.strictEqual(env.store.events.length, 1);
  });

  await test('chat inside an open shadow root within the frame', async () => {
    const env = makeEnv();
    const dom = new JSDOM(TOP_HTML, { url: 'https://x.com/i/chat/' + CONV, runScripts: 'outside-only' });
    const w = dom.window; stubRects(w);
    w.console = { log() {}, warn() {} };
    w.chrome = env.chromeFor(0);
    await env.prime();
    const iframe = w.document.querySelector('iframe');
    const fw = iframe.contentWindow; stubRects(fw);
    const d = iframe.contentDocument;
    const host = d.createElement('xchat-app'); d.body.appendChild(host);
    const sr = host.attachShadow({ mode: 'open' });
    sr.innerHTML = chatHtml('S');
    fw.console = { log() {}, warn() {} };
    fw.chrome = env.chromeFor(3);
    fw.eval(CONTENT);
    w.eval(CONTENT);
    await sleep(SETTLE);
    addMsg(d, true, 'from inside shadow', sr); await sleep(400);
    addMsg(d, false, 'bot inside shadow', sr); await sleep(400);
    assert.deepStrictEqual(sent(env).map(x => x[0]), ['from inside shadow']);
    w.close();
  });

  await test('item 7: a frame started on the /i/chat inbox relays after in-app nav to /i/chat/<id>', async () => {
    const env = makeEnv();
    const dom = new JSDOM(TOP_HTML, { url: 'https://x.com/i/chat', runScripts: 'outside-only' });
    const w = dom.window; stubRects(w); w.console = { log() {}, warn() {} }; w.chrome = env.chromeFor(0);
    await env.prime(); w.eval(CONTENT);
    const iframe = w.document.querySelector('iframe'); const fw = iframe.contentWindow; stubRects(fw);
    iframe.contentDocument.body.innerHTML = '<div>inbox list</div>';
    fw.console = { log() {}, warn() {} }; fw.chrome = env.chromeFor(7); fw.eval(CONTENT);
    await sleep(300);
    w.history.pushState({}, '', '/i/chat/' + CONV);
    iframe.contentDocument.body.innerHTML = chatHtml('N');
    await sleep(SETTLE);
    addMsg(iframe.contentDocument, true, 'reply after in-app nav'); await sleep(600);
    assert.deepStrictEqual(sent(env).map(x => x[0]), ['reply after in-app nav']);
    w.close();
  });

  await test('item 6: the wrong-account flag survives the 1-minute config refresh', async () => {
    const env = makeEnv();
    const dom = new JSDOM(`<div data-testid="UserAvatar-Container-SomeoneElse"></div>` + chatHtml('W'), { url: 'https://x.com/i/chat/' + CONV, runScripts: 'outside-only' });
    const w = dom.window; stubRects(w); w.console = { log() {}, warn() {} }; w.chrome = env.chromeFor(0);
    await env.prime(); w.eval(CONTENT); await sleep(SETTLE);
    addMsg(w.document, true, 'r1'); await sleep(400);
    assert.strictEqual(env.store.status.wrongAccount, true, 'wrong account detected');
    globalThis.__alarm({ name: 'relay-refresh' }); await sleep(200);
    addMsg(w.document, true, 'r2'); await sleep(400);
    assert.strictEqual(env.store.status.wrongAccount, true, 'still flagged after the refresh');
    w.close();
  });

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
