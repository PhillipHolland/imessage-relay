/* v1.4.1: a plain "yes"/"ok" confirms a pending reply prompt and "no"/"cancel" cancels it; neither is texted.
 * (Header kept from the v1.3.4 guess test harness.)
 * A guessed reply (host returns needsConfirm) is held in chrome.storage.session (never in local),
 * "/yes" re-sends the stored text with confirmed:true, and a lapsed window signals guess_expired.
 *   NODE_PATH=/path/to/node_modules node tests/js/guess.test.js
 */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const EXT = path.join(__dirname, '../../extension');

const local = {}; const session = {}; const bgListeners = []; const alarmListeners = [];
const hostCalls = []; let nextSendResponse = { ok: true, dryRun: true };
function mkStorage(bag) {
  return {
    async get(keys) { const ks = keys == null ? Object.keys(bag) : (typeof keys === 'string' ? [keys] : keys); const o = {}; ks.forEach(k => { if (k in bag) o[k] = JSON.parse(JSON.stringify(bag[k])); }); return o; },
    async set(obj) { for (const k in obj) bag[k] = obj[k]; },
    async remove(k) { delete bag[k]; },
  };
}
const chrome = {
  runtime: {
    id: 'ext', onMessage: { addListener: f => bgListeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
    sendNativeMessage: async (host, msg) => {
      hostCalls.push(msg);
      if (msg.type === 'get_config') return { ok: true, paused: false, dryRun: true, relayConversationId: '111-222', ownerXHandle: 'OwnerAcct', senderXHandle: 'SenderBot', contacts: [{ name: 'Sam Alpha', label: 'Sam', handle: '+15555550123' }] };
      if (msg.type === 'send') return nextSendResponse;
      return { ok: true };
    },
  },
  storage: { local: mkStorage(local), session: mkStorage(session), onChanged: { addListener() {} } },
  tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} }, query: async () => [] },
  alarms: { create() {}, clear() {}, onAlarm: { addListener: f => alarmListeners.push(f) } },
  action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
};
vm.runInNewContext(fs.readFileSync(path.join(EXT, 'background.js'), 'utf8'), { chrome, console: { log() {}, warn() {} } });

function relayLog(text) {
  const item = { ts: new Date().toISOString(), conversationId: '111-222', direction: 'outbound', text, key: 'k-' + Math.random(), score: 5, source: 'frame' };
  return new Promise(res => { bgListeners.forEach(f => f({ type: 'relay-log', item }, { id: 'ext', tab: { id: 1 } }, () => {})); setTimeout(res, 40); });
}

(async () => {
  await sleep(30);
  const sends = () => hostCalls.filter(c => c.type === 'send');
  // 1) a guessed reply is staged
  nextSendResponse = { ok: false, needsConfirm: true, guess: true, label: 'Sam', handle: '+15555550123', contact: 'Sam', routedBy: 'recent' };
  await relayLog('running late');
  assert.strictEqual(sends().length, 1);
  // 2) a plain "Yes!" confirms with the stored body; the word itself is never sent
  nextSendResponse = { ok: true, dryRun: true, contact: 'Sam' };
  await relayLog('Yes!');
  const conf = sends().filter(c => c.confirmed === true);
  assert.strictEqual(conf.length, 1, 'plain yes confirmed');
  assert.strictEqual(conf[0].text, 'running late');
  assert.ok(!sends().some(c => /^yes/i.test(c.text)), '"Yes!" never sent as a text');
  assert.ok(!session.pendingGuess, 'cleared');
  // 3) "do it" and "ok" confirm too
  for (const w of ['do it', 'OK \u{1F44D}']) {
    nextSendResponse = { ok: false, needsConfirm: true, guess: true, label: 'Sam', handle: '+15555550123' };
    await relayLog('again');
    nextSendResponse = { ok: true, dryRun: true };
    const n = sends().filter(c => c.confirmed === true).length;
    await relayLog(w);
    assert.strictEqual(sends().filter(c => c.confirmed === true).length, n + 1, w + ' confirms');
  }
  // 4) "no" cancels: nothing sent, a body-free guess_cancelled signal
  nextSendResponse = { ok: false, needsConfirm: true, guess: true, label: 'Sam', handle: '+15555550123' };
  await relayLog('see you');
  const n2 = sends().length;
  await relayLog('Cancel.');
  assert.strictEqual(sends().length, n2, 'cancel sends nothing');
  const sig = hostCalls.filter(c => c.type === 'signal' && c.status === 'guess_cancelled');
  assert.strictEqual(sig.length, 1);
  assert.strictEqual(sig[0].label, 'Sam');
  assert.ok(!JSON.stringify(sig[0]).includes('see you'), 'no body in the signal');
  assert.ok(!session.pendingGuess, 'cleared on cancel');
  // 5) with nothing pending here, "yes" goes to the host (which knows about its own prompts)
  nextSendResponse = { ok: true, dryRun: true };
  await relayLog('yes');
  const last = sends()[sends().length - 1];
  assert.strictEqual(last.text, 'yes');
  assert.ok(!last.confirmed);
  console.log('5 passed, 0 failed (v1.4.1 plain yes/no)');
  process.exit(0);
})().catch(e => { console.log('FAIL', e.stack || e.message); process.exit(1); });
