/* Smoke test: runs content.js + background.js with a fake `chrome` object to check the
 * extension wiring end to end: content -> background -> get_config -> send to host, and
 * pause coming from the host config. Run:
 *   NODE_PATH=/path/to/node_modules node tests/js/smoke-chrome.test.js
 */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { JSDOM } = require('jsdom');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const EXT = path.join(__dirname, '../../extension');

const store = {}; const storageListeners = []; const bgListeners = [];
let badge = { text: '' };
const hostCalls = [];
let hostPaused = false;
function storageApi() {
  return {
    async get(keys) { const ks = typeof keys === 'string' ? [keys] : keys; const o = {}; ks.forEach(k => { if (k in store) o[k] = JSON.parse(JSON.stringify(store[k])); }); return o; },
    async set(obj) { const ch = {}; for (const k in obj) { ch[k] = { oldValue: store[k], newValue: obj[k] }; store[k] = obj[k]; } storageListeners.forEach(l => l(ch, 'local')); },
  };
}
const bgChrome = {
  runtime: {
    id: 'ext', onMessage: { addListener: f => bgListeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
    sendNativeMessage: async (host, msg) => {
      hostCalls.push(msg);
      if (msg.type === 'get_config') return { ok: true, paused: hostPaused, dryRun: true, relayConversationId: '111-222', ownerXHandle: 'OwnerAcct', senderXHandle: 'SenderBot', contacts: [{ name: 'Test Contact', label: 'Test', handle: '+15555550123' }] };
      return { ok: true, dryRun: true };
    },
  },
  storage: { local: storageApi(), session: { async get() { return {}; }, async set() {} }, onChanged: { addListener: f => storageListeners.push(f) } },
  tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} } },
  alarms: { create() {}, onAlarm: { addListener() {} } },
  action: { async setBadgeText(o) { badge = o; }, async setBadgeBackgroundColor() {} },
};
vm.runInNewContext(fs.readFileSync(path.join(EXT, 'background.js'), 'utf8'), { chrome: bgChrome, console: { log() {}, warn() {} } });

const html = fs.readFileSync(path.join(__dirname, 'fake-chat.html'), 'utf8');
const dom = new JSDOM(html, { url: 'https://x.com/i/chat/111-222', runScripts: 'outside-only' });
const w = dom.window;
w.Element.prototype.getBoundingClientRect = function () {
  const v = this.getAttribute('data-fake-rect'); if (!v) return { left: 0, top: 0, width: 0, height: 0 };
  const [l, t, wd, h] = v.split(',').map(Number); return { left: l, top: t, width: wd, height: h };
};
w.console = { log() {}, warn() {} };
w.chrome = {
  runtime: { id: 'ext', sendMessage: async msg => { bgListeners.forEach(f => f(msg, { id: 'ext', tab: { id: 1 } }, () => {})); }, onMessage: { addListener() {} } },
  storage: { local: storageApi(), onChanged: { addListener: f => storageListeners.push(f) } },
};
w.eval(fs.readFileSync(path.join(EXT, 'content.js'), 'utf8'));

(async () => {
  // manifest sanity: v1.3.2, no options page, fixed key, expected permissions
  const m = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  assert.strictEqual(m.version, '1.4.5');
  assert.ok(!m.options_page && !m.options_ui, 'no options page');
  assert.ok(m.key && m.key.length > 300, 'fixed public key -> stable extension id');
  assert.deepStrictEqual(m.permissions.sort(), ['alarms', 'nativeMessaging', 'storage']);
  for (const f of ['options.html', 'options.js']) assert.ok(!fs.existsSync(path.join(EXT, f)), f + ' removed');

  await sleep(2800); // real CONFIG.settleMs = 2500
  // prime the host config so ownerNames reach storage (boot() does this in the real extension),
  // which lets the content-script owner-identity gate confirm the owner before any reply.
  await new Promise(res => bgListeners.forEach(f => f({ type: 'relay-status' }, { id: 'ext' }, res)));
  await sleep(50);
  const vp = w.document.querySelector('[data-testid="dm-conversation-panel"]');
  let u = 0;
  const uuid = n => `df48633d-6588-4b18-bc77-${String(n).padStart(12, '0')}`;
  const add = (right, text) => { const id = uuid(++u); const e = w.document.createElement('div'); e.setAttribute('data-testid', 'message-' + id); e.style.display = 'flex'; e.style.justifyContent = right ? 'flex-end' : 'flex-start'; e.innerHTML = `<div data-fake-rect="${right ? 780 : 420},0,200,40"><span data-testid="message-text-${id}">${text}</span></div>`; vp.appendChild(e); };
  add(true, 'smoke out'); await sleep(400);
  add(false, 'smoke in'); await sleep(400);
  hostPaused = true;
  await new Promise(res => bgListeners.forEach(f => f({ type: 'relay-status' }, { id: 'ext' }, res)));   // popup refresh picks up pause
  await sleep(50);
  add(true, 'paused msg'); await sleep(400);
  const sends = hostCalls.filter(c => c.type === 'send').map(c => c.text);
  assert.deepStrictEqual(sends, ['smoke out']);
  assert.strictEqual(store.paused, true, 'paused mirrored from host config');
  assert.strictEqual(store.relayConversationId, '111-222', 'relay chat mirrored for the content-script filter');
  assert.strictEqual(badge.text, 'II', 'badge shows paused');
  assert.ok(!JSON.stringify(store).includes('smoke'), 'no message bodies in storage');
  console.log('PASS chrome wiring smoke: sent=', JSON.stringify(sends), 'badge(paused)=', badge.text, 'events=', store.events.map(e => e.status).join(','));
  process.exit(0);
})().catch(e => { console.log('FAIL', e.message); process.exit(1); });
