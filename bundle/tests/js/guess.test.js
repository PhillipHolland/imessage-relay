/* item 3 (v1.3.4): confirm-when-guessed flow in background.js.
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
  // 1) a guessed reply: the host asks to confirm; nothing is sent, text is held in session (not local)
  nextSendResponse = { ok: false, needsConfirm: true, guess: true, label: 'Sam', handle: '+15555550123', contact: 'Sam', routedBy: 'recent' };
  await relayLog('running late');
  const sendsBefore = hostCalls.filter(c => c.type === 'send');
  assert.strictEqual(sendsBefore.length, 1, 'the original reply reached the host once (and got needsConfirm)');
  assert.ok(session.pendingGuess && session.pendingGuess.text === 'running late', 'text held in session storage');
  assert.ok(!JSON.stringify(local).includes('running late'), 'body never written to local storage');

  // 2) "/yes" re-sends the stored text with confirmed:true
  nextSendResponse = { ok: true, dryRun: true, contact: 'Sam', routedBy: 'recent' };
  await relayLog('/yes');
  const confirmed = hostCalls.filter(c => c.type === 'send' && c.confirmed === true);
  assert.strictEqual(confirmed.length, 1, 'exactly one confirmed send');
  assert.strictEqual(confirmed[0].text, 'running late', 'the stored body is what gets sent');
  assert.strictEqual(confirmed[0].handle, '+15555550123', 'C1: /yes carries the confirmed handle so the host never re-guesses');
  assert.ok(!session.pendingGuess, 'pending cleared after confirm');

  // 3) "/yes" with no pending -> nothing sent, "nothing to confirm"
  const before = hostCalls.filter(c => c.type === 'send').length;
  await relayLog('/yes');
  assert.strictEqual(hostCalls.filter(c => c.type === 'send').length, before, '/yes with nothing pending sends nothing');

  // 4) "/yes <wrong name>" does not fire the staged reply
  nextSendResponse = { ok: false, needsConfirm: true, guess: true, label: 'Sam', handle: '+15555550123' };
  await relayLog('see you');
  const b2 = hostCalls.filter(c => c.type === 'send' && c.confirmed === true).length;
  await relayLog('/yes Robin');
  assert.strictEqual(hostCalls.filter(c => c.type === 'send' && c.confirmed === true).length, b2, 'wrong name does not confirm');
  assert.ok(session.pendingGuess, 'pending kept when the name did not match');

  // 5) expiry: age the pending, fire the alarm -> host gets a body-free guess_expired signal
  session.pendingGuess.at = Date.now() - 3 * 60 * 1000;
  for (const f of alarmListeners) await f({ name: 'relay-guess-expiry' });
  await sleep(20);
  const exp = hostCalls.filter(c => c.type === 'signal' && c.status === 'guess_expired');
  assert.strictEqual(exp.length, 1, 'one guess_expired signal');
  assert.strictEqual(exp[0].label, 'Sam');
  assert.ok(!session.pendingGuess, 'pending cleared on expiry');

  console.log('5 passed, 0 failed (item 3 guess flow)');
  process.exit(0);
})().catch(e => { console.log('FAIL', e.stack || e.message); process.exit(1); });
