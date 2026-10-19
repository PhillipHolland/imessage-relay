/* v1.3.8: the "/test" end-to-end check in background.js.
 * "/test" is intercepted before reply routing (works paused / with no contacts), the host call is
 * time-boxed, the routine's "🧪 relay test #tag" report is matched only on its exact header, a missing
 * report times out as failed, and nothing is ever sent as a reply.
 *   NODE_PATH=/path/to/node_modules node tests/js/e2etest.test.js
 */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const EXT = path.join(__dirname, '../../extension');
const MANIFEST = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));

const local = {}; const session = {}; const bgListeners = []; const alarmListeners = []; const alarms = [];
const hostCalls = [];
let hostUp = true, hangTest = false, testResp = null, cfgOver = {};
function mkStorage(bag) {
  return {
    async get(keys) { const ks = keys == null ? Object.keys(bag) : (typeof keys === 'string' ? [keys] : keys); const o = {}; ks.forEach(k => { if (k in bag) o[k] = JSON.parse(JSON.stringify(bag[k])); }); return o; },
    async set(obj) { for (const k in obj) bag[k] = obj[k]; },
    async remove(k) { delete bag[k]; },
  };
}
const chrome = {
  runtime: {
    id: 'ext', getManifest: () => MANIFEST,
    onMessage: { addListener: f => bgListeners.push(f) }, onInstalled: { addListener() {} }, onStartup: { addListener() {} },
    sendNativeMessage: async (host, msg) => {
      hostCalls.push(msg);
      if (!hostUp) throw new Error('Specified native messaging host not found.');
      if (msg.type === 'get_config') return Object.assign({ ok: true, paused: true, dryRun: true, relayConversationId: '111-222', contacts: [] }, cfgOver);
      if (msg.type === 'test') {
        if (hangTest) return new Promise(() => {});
        return testResp || { ok: true, command: 'test', testId: msg.testId, tag: msg.testId.slice(0, 6), webhookAccepted: true,
          steps: [{ name: 'extension', ok: true }, { name: 'host', ok: true }, { name: 'watcher', ok: false }, { name: 'messages', ok: true }] };
      }
      return { ok: true };
    },
  },
  storage: { local: mkStorage(local), session: mkStorage(session), onChanged: { addListener() {} } },
  tabs: { onUpdated: { addListener() {} }, onRemoved: { addListener() {} }, query: async () => [] },
  alarms: { create(n, o) { alarms.push([n, o]); }, clear() {}, onAlarm: { addListener: f => alarmListeners.push(f) } },
  action: { async setBadgeText() {}, async setBadgeBackgroundColor() {} },
};
// Shrink long timers so the 45s host timeout is exercised in milliseconds.
const fastSetTimeout = (f, ms, ...a) => setTimeout(f, Math.min(ms, 30), ...a);
vm.runInNewContext(fs.readFileSync(path.join(EXT, 'background.js'), 'utf8'),
  { chrome, console: { log() {}, warn() {} }, setTimeout: fastSetTimeout, clearTimeout, crypto: require('crypto').webcrypto, Uint8Array });

function relayLog(text, direction = 'outbound', conv = '111-222', score) {
  const item = { ts: new Date().toISOString(), conversationId: conv, direction, text, key: 'k-' + Math.random(), score: score == null ? (direction === 'outbound' ? 5 : 0) : score, source: 'frame' };
  return new Promise(res => { bgListeners.forEach(f => f({ type: 'relay-log', item }, { id: 'ext', tab: { id: 1 } }, () => {})); setTimeout(res, 80); }).then(() => item);
}
const lastEvent = () => (local.events || []).slice(-1)[0] || {};
const calls = t => hostCalls.filter(c => c.type === t);
let passed = 0;
const ok = (c, m) => { assert.ok(c, m); passed++; };

(async () => {
  await sleep(30);

  // 1) "/test" while paused with NO contacts: intercepted, host gets {type:test}, never a reply send
  await relayLog('/test');
  const t1 = calls('test');
  ok(t1.length === 1 && t1[0].send === false && /^[0-9a-f]{12}$/.test(t1[0].testId) && t1[0].conversationId === '111-222', 'one test request');
  ok(calls('send').length === 0, '"/test" is never forwarded as a reply');
  ok(calls('get_config').some(c => c.extVersion === MANIFEST.version), 'get_config carries the extension version');
  ok(lastEvent().kind === 'test' && lastEvent().status === 'test-waiting' && /watcher failed/.test(lastEvent().detail), 'popup shows host results');
  ok(session.pendingTest && session.pendingTest.tag === t1[0].testId.slice(0, 6), 'pending test held in session storage');
  ok(alarms.some(a => a[0] === 'relay-test-expiry'), 'expiry alarm armed');

  // 2) a RELAYED text that merely contains the sigil does not count; the real header does
  const tag = session.pendingTest.tag;
  await relayLog('Sam: \u{1F9EA} relay test #' + tag, 'inbound');
  ok(session.pendingTest, 'relayed look-alike ignored');
  await relayLog('\u{1F9EA} relay test #' + tag + ' \u2014 1 failed\n\u274C 3 watcher did not answer', 'inbound');
  ok(!session.pendingTest, 'report matched, pending cleared');
  ok(lastEvent().status === 'test-failed' && /some steps failed/.test(lastEvent().detail), 'a report with a failed step shows failed');
  ok(calls('signal').some(c => c.status === 'test_seen' && c.testId === t1[0].testId), 'host told the report was seen');

  // 3) all-green report -> test-ok
  await relayLog('/test');
  const tag2 = session.pendingTest.tag;
  await relayLog('\u{1F9EA} relay test #' + tag2 + ' \u2014 all checks passed\n\u2705 1 extension saw /test', 'unknown');
  ok((local.events || []).some(e => e.status === 'test-ok'), 'all-green report shows ok');

  // 4) the routine never posts: the expiry alarm reports step 6 failed and signals test_unseen
  await relayLog('/test send');
  ok(calls('test').slice(-1)[0].send === true, '"/test send" asks for the own-number send');
  session.pendingTest.at = Date.now() - 4 * 60 * 1000;
  for (const f of alarmListeners) await f({ name: 'relay-test-expiry' });
  await sleep(40);
  ok(!session.pendingTest && /routine did not post/.test(lastEvent().detail) && lastEvent().status === 'test-failed', 'missing report = failed, not a hang');
  ok(calls('signal').some(c => c.status === 'test_unseen'), 'host told the report never came');

  // 5) the host hangs: time-boxed, reported failed
  hangTest = true;
  await relayLog('/test');
  await sleep(120);
  ok(lastEvent().status === 'test-failed' && /did not finish: timed out/.test(lastEvent().detail), 'host hang becomes a failed step');
  hangTest = false;

  // 6) webhook rejected: no waiting for a report that cannot come
  testResp = { ok: false, command: 'test', testId: 'aaaaaaaaaaaa', tag: 'aaaaaa', webhookAccepted: false, steps: [{ name: 'webhook', ok: false }] };
  await relayLog('/test');
  ok(lastEvent().status === 'test-failed' && /webhook failed/.test(lastEvent().detail) && !session.pendingTest, 'webhook failure reported, nothing pending');
  testResp = null;

  // 7) outside the relay chat, or not the owner's bubble: ignored entirely
  cfgOver = { paused: false, contacts: [{ name: 'Sam', label: 'Sam', handle: '+15555550123' }] };
  const before = calls('test').length;
  await relayLog('/test', 'outbound', '999-888');
  await relayLog('/test', 'outbound', '111-222', 1);
  ok(calls('test').length === before, 'only the owner, only in the relay chat');

  // 8) "/test foo" is not the test command (so the normal reply path sees it; the host never routes it)
  await relayLog('/test foo');
  ok(calls('test').length === before && calls('send').length === 1, '"/test foo" is not intercepted');

  // 9) host down: the extension itself reports step 2 failed
  hostUp = false;
  await relayLog('/test');
  ok(lastEvent().status === 'test-failed' && /2 native host not reachable/.test(lastEvent().detail), 'host down reported locally');

  ok(!JSON.stringify(local).includes('relay test #'), 'no X Chat text kept in local storage');
  console.log(passed + ' passed, 0 failed (v1.3.8 /test)');
  process.exit(0);
})().catch(e => { console.log('FAIL', e.stack || e.message); process.exit(1); });
