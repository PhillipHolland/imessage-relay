/* v1.4.6: his photos in X Chat go out as iMessage attachments. content.js finds images in HIS outbound
 * bubble only (not emoji, not the quoted message) and reads them in 512 KiB chunks; background.js streams
 * them to the Mac (attach_chunk), sends metadata only with the reply, and keeps them for a "yes".
 * Fictional names only. */
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vm = require('vm');
const nodeCrypto = require('crypto');
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

function setup() {
  const dom = new JSDOM('<html><body><div data-testid="dm-conversation-panel"></div></body></html>', { url: 'https://x.com/i/chat/111-222', pretendToBeVisual: true });
  const { window } = dom;
  window.Element.prototype.getBoundingClientRect = function () {
    const v = this.getAttribute('data-fake-rect');
    if (!v) return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
    const [l, t, w, h] = v.split(',').map(Number);
    return { left: l, top: t, width: w, height: h, right: l + w, bottom: t + h };
  };
  const logs = [];
  out(window.document, 900, 'older history');      // already on the page at start: baselined, never sent
  const det = createRelayDetector({ window, document: window.document, config: FAST, getState: () => ({ paused: false, conversationFilter: '' }),
    onEmit: i => logs.push(i), onStatus: () => {}, log: () => {}, onReaction: () => {}, seen: { has: () => false, add: () => {} } });
  det.start();
  return { window, doc: window.document, det, logs };
}
function out(doc, n, text, imgs) {
  const e = doc.createElement('div');
  e.setAttribute('data-testid', 'message-' + uuid(n));
  const b = doc.createElement('div');
  b.setAttribute('data-fake-rect', '780,0,200,40');
  b.setAttribute('data-testid', 'bubble-sent');
  for (const html of imgs || []) b.insertAdjacentHTML('beforeend', html);
  if (text) { const s = doc.createElement('span'); s.setAttribute('data-testid', 'message-text-' + uuid(n)); s.textContent = text; b.appendChild(s); }
  e.appendChild(b);
  doc.querySelector('[data-testid="dm-conversation-panel"]').appendChild(e);
  return e;
}

function bootBg(opts) {
  const EXT = path.join(__dirname, '../../extension');
  const MANIFEST = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  const bag = {}, sess = {}, listeners = [], host = [], tabMsgs = [];
  const mk = b => ({ async get(k) { const ks = k == null ? Object.keys(b) : (typeof k === 'string' ? [k] : k); const o = {}; ks.forEach(x => { if (x in b) o[x] = JSON.parse(JSON.stringify(b[x])); }); return o; },
                     async set(o) { Object.assign(b, o); }, async remove(k) { delete b[k]; } });
  const chrome = {
    runtime: { id: 'ext', getManifest: () => MANIFEST, onMessage: { addListener: f => listeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
      sendNativeMessage: async (h, m) => {
        host.push(JSON.parse(JSON.stringify(m)));
        if (m.type === 'get_config') return { ok: true, paused: false, dryRun: false, relayConversationId: '111-222', contacts: [{ name: 'Jordan', handle: '15555550123' }], uiReactions: true, undoSeconds: 0, relayAttachments: opts.off ? false : true };
        if (m.type === 'send') return opts.send(m);
        if (m.type === 'attach_chunk') return { ok: true };
        return { ok: true, actions: [] };
      } },
    storage: { local: mk(bag), session: mk(sess), onChanged: { addListener() {} } },
    tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} }, query: async () => [],
            sendMessage: async (tabId, m) => { tabMsgs.push(JSON.parse(JSON.stringify(m))); return opts.tab(m); } },
    alarms: { create() {}, clear() {}, onAlarm: { addListener() {} } },
    action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
  };
  vm.runInNewContext(fs.readFileSync(path.join(EXT, 'background.js'), 'utf8'),
    { chrome, console: { log() {}, warn() {} }, setTimeout, clearTimeout, crypto: nodeCrypto.webcrypto, Uint8Array });
  let k = 0;
  const reply = (text, extra, wait) => new Promise(r => {
    const item = Object.assign({ ts: new Date().toISOString(), conversationId: '111-222', direction: 'outbound', text, key: '111-222|id:uuid=' + uuid(++k), score: 9 }, extra);
    listeners.forEach(f => f({ type: 'relay-log', item }, { id: 'ext', tab: { id: 7 }, frameId: 0 }, () => {}));
    setTimeout(r, wait || 150);
  });
  return { host, tabMsgs, reply };
}
const SHA = 'ab'.repeat(32);
const tabOk = m => m.action === 'media-info' ? { ok: true, size: 700000, mime: 'image/jpeg', sha256: SHA, name: 'photo.jpg', chunks: 2 }
  : m.action === 'media-chunk' ? { ok: true, data: 'QUJD' } : { ok: true, step: 'placed' };

(async () => {
  await test('content: only real images in HIS bubble count (emoji images are skipped)', async () => {
    const s = setup();
    await sleep(200);
    out(s.doc, 1, 'look', ['<img src="blob:https://x.com/1111" width="300">', '<img src="https://abs-0.twimg.com/emoji/v2/svg/1f600.svg" alt="\u{1F600}">']);
    await sleep(250);
    const it = s.logs.find(l => l.text === 'look');
    assert.ok(it, 'emitted');
    assert.strictEqual(it.mediaCount, 1);
    assert.ok(!JSON.stringify(it).includes('blob:'), 'no source URL leaves the tab');
  });

  await test('content: a photo-only bubble is emitted with empty text', async () => {
    const s = setup();
    await sleep(200);
    out(s.doc, 2, '', ['<img src="blob:https://x.com/2222" width="300">']);
    await sleep(250);
    const it = s.logs.find(l => l.mediaCount === 1);
    assert.ok(it, 'emitted');
    assert.strictEqual(it.text, '');
  });

  await test('content: bytes are read in-page, hashed, chunked, then released', async () => {
    const s = setup();
    await sleep(200);
    out(s.doc, 3, 'pic', ['<img src="blob:https://x.com/3333" width="300">']);
    await sleep(250);
    const bytes = new Uint8Array(600 * 1024); bytes[0] = 0xff; bytes[1] = 0xd8; bytes[2] = 0xff; bytes[700] = 7;
    s.window.fetch = async (src) => { assert.strictEqual(src, 'blob:https://x.com/3333'); return { ok: true, headers: { get: () => null }, arrayBuffer: async () => bytes.buffer }; };
    const info = await s.det.uiMediaInfo(uuid(3), 0);
    assert.deepStrictEqual([info.ok, info.size, info.mime, info.chunks, info.name], [true, bytes.length, 'image/jpeg', 2, 'photo.jpg']);
    assert.strictEqual(info.sha256, nodeCrypto.createHash('sha256').update(bytes).digest('hex'));
    const c0 = Buffer.from(s.det.uiMediaChunk(uuid(3), 0, 0).data, 'base64');
    const c1 = Buffer.from(s.det.uiMediaChunk(uuid(3), 0, 1).data, 'base64');
    assert.strictEqual(c0.length, 512 * 1024);
    assert.ok(Buffer.concat([c0, c1]).equals(Buffer.from(bytes)));
    s.det.uiMediaRelease(uuid(3));
    assert.strictEqual(s.det.uiMediaChunk(uuid(3), 0, 0).step, 'expired');
  });

  await test('content: over 25 MB is refused before reading', async () => {
    const s = setup();
    await sleep(200);
    out(s.doc, 4, 'big', ['<img src="blob:https://x.com/4444" width="300">']);
    await sleep(250);
    s.window.fetch = async () => ({ ok: true, headers: { get: h => h === 'content-length' ? String(30 * 1024 * 1024) : null }, arrayBuffer: async () => { throw new Error('read'); } });
    const info = await s.det.uiMediaInfo(uuid(4), 0);
    assert.deepStrictEqual([info.ok, info.step], [false, 'too-large']);
  });

  await test('background: chunks stream in order, the reply carries metadata only', async () => {
    const b = bootBg({ tab: tabOk, send: () => ({ ok: true, sent: true }) });
    await b.reply('Jordan: look', { mediaCount: 1 });
    const chunks = b.host.filter(m => m.type === 'attach_chunk');
    assert.deepStrictEqual(chunks.map(c => c.seq), [0, 1]);
    assert.ok(/^[0-9a-f]{32}$/.test(chunks[0].uploadId));
    const send = b.host.find(m => m.type === 'send');
    assert.deepStrictEqual(send.attachments, [{ uploadId: chunks[0].uploadId, size: 700000, sha256: SHA, mime: 'image/jpeg', name: 'photo.jpg' }]);
    assert.ok(b.host.indexOf(send) > b.host.indexOf(chunks[1]), 'bytes first, then the send');
    assert.ok(b.tabMsgs.some(m => m.action === 'media-release'));
  });

  await test('background: an unreadable photo is reported as skipped, never silently dropped', async () => {
    const b = bootBg({ tab: m => m.action === 'media-info' ? { ok: false, step: 'too-large' } : { ok: true }, send: () => ({ ok: false, error: 'attachments could not be read' }) });
    await b.reply('', { mediaCount: 1 });
    const send = b.host.find(m => m.type === 'send');
    assert.strictEqual(send.attachmentsSkipped, 1);
    assert.ok(!send.attachments);
    assert.strictEqual(b.host.filter(m => m.type === 'attach_chunk').length, 0);
  });

  await test('background (v1.4.11): "2" answers a pick prompt with the 2nd offered handle', async () => {
    const sends = [];
    const b = bootBg({ tab: tabOk, send: m => { sends.push(m); return m.confirmed ? { ok: true, delivered: true, contact: 'Jordan Rivera' }
      : { ok: false, needsConfirm: true, label: 'Jordan Rivera', handle: '+15555550103', reason: 'new_contact', options: ['+15555550103', '+15555550101', '+15555550102'] }; } });
    await b.reply('Jordan Rivera: hi there', {});
    await b.reply('2', {});
    const c = sends.filter(m => m.confirmed);
    assert.strictEqual(c.length, 1, JSON.stringify(sends));
    assert.strictEqual(c[0].handle, '+15555550101');
    assert.strictEqual(c[0].text, 'Jordan Rivera: hi there');
    await b.reply('3', {});
    assert.strictEqual(sends.filter(m => m.confirmed).length, 1, 'the pick is used once');
  });
  await test('background (v1.4.12): last 4 digits or an email start picks; a non-match is never a pick', async () => {
    for (const [ans, want] of [['0102', '+15555550102'], ['jr.', 'jr.rivera@icloud.com'], ['8500', null]]) {
      const sends = [];
      const b = bootBg({ tab: tabOk, send: m => { sends.push(m); return m.confirmed ? { ok: true, delivered: true }
        : { ok: false, needsConfirm: true, label: 'Jordan Rivera', handle: '+15555550103', reason: 'new_contact',
            options: ['+15555550103', '+15555550102', 'jr.rivera@icloud.com'] }; } });
      await b.reply('Jordan Rivera: hi there', {});
      await b.reply(ans, {});
      const c = sends.filter(m => m.confirmed);
      if (want) { assert.strictEqual(c.length, 1, ans); assert.strictEqual(c[0].handle, want); }
      else assert.strictEqual(c.length, 0, 'no option ends in ' + ans);
    }
  });
  await test('background (v1.4.10): a skipped reply is never silent: it gets a warning reaction', async () => {
    const b = bootBg({ send: () => ({ ok: true }), tab: tabOk });
    await b.reply('', {});
    assert.strictEqual(b.host.filter(m => m.type === 'send').length, 0);
    const r = b.tabMsgs.find(m => m.action === 'react');
    assert.ok(r && r.emoji === '\u26A0\uFE0F' && r.uuid === uuid(1), JSON.stringify(b.tabMsgs));
  });
  await test('background: attachments off -> nothing is read, the host is told', async () => {
    const b = bootBg({ off: true, tab: tabOk, send: () => ({ ok: true, sent: true }) });
    await b.reply('Jordan: look', { mediaCount: 2 });
    assert.strictEqual(b.tabMsgs.filter(m => /^media-/.test(m.action)).length, 0);
    assert.strictEqual(b.host.find(m => m.type === 'send').attachmentsSkipped, 2);
  });

  await test('background: a confirm keeps the uploads for "yes"; "no" discards them', async () => {
    let n = 0;
    const b = bootBg({ tab: tabOk, send: m => (++n === 1 ? { ok: false, needsConfirm: true, handle: '+15555550123', label: 'Jordan', reason: 'guessed' } : { ok: true, sent: true }) });
    await b.reply('see this', { mediaCount: 1 });
    const first = b.host.find(m => m.type === 'send');
    await b.reply('yes');
    const sends = b.host.filter(m => m.type === 'send');
    assert.strictEqual(sends.length, 2);
    assert.strictEqual(sends[1].confirmed, true);
    assert.deepStrictEqual(sends[1].attachments, first.attachments);
    n = 0;
    await b.reply('look again', { mediaCount: 1 });
    await b.reply('no');
    assert.strictEqual(b.host.filter(m => m.type === 'attach_discard').length, 1);
  });

  console.log(`${passed} passed, ${failed} failed (v1.4.6 attachments)`);
  process.exit(failed ? 1 : 0);
})();
