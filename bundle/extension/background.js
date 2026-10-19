/*
 * iMessage Relay v1.4.2: service worker (/test end-to-end check added in v1.3.8).
 *
 * Reply path: X Chat (owner's own reply, seen by content.js) -> this worker -> local Mac helper
 * (native messaging host com.imessage_relay.host) -> Messages.app -> iMessage.
 *
 * One source of truth: the relay chat id (relayConversationId), the contact labels, paused and
 * dryRun all live in ~/.imessage-relay/config.json on the Mac and are fetched with
 * {type:"get_config"}. Who a reply goes to is decided by the host (quoted message, then a leading
 * "Name:", then the contact relayed last); if unclear it sends nothing and answers needsTarget.
 *
 * v1.3.2: content.js is uuid-only and gates on owner identity, so this worker no longer coordinates
 * a top-frame fallback. It handles two extra content signals: a body-free wrong_account status, and
 * a refused (over-limit) reply, which it turns into an owner-facing event without contacting the host.
 *
 * Privacy: message bodies are never written to extension storage. Events keep time, contact name,
 * status and text LENGTH only. No network requests, no analytics.
 */
'use strict';

const LOG_PREFIX = '[imessage-relay]';
const NATIVE_HOST = 'com.imessage_relay.host';
const MAX_EVENTS = 50;
const MIN_NATIVE_SCORE = 3;             // direction score needed to treat a bubble as the owner's reply
const MAX_ITEM_AGE_MS = 130 * 1000;     // only live items are forwarded (content freshness window is 120s)
const CONFIG_TTL_MS = 30 * 1000;
const DEDUPE_MS = 10 * 60 * 1000;
const GUESS_WINDOW_MS = 2 * 60 * 1000;   // item 3: the owner has 2 minutes to confirm a guessed reply
const YES_RE = /^\s*\/yes\b\s*(.*)$/i; // "/yes" or "/yes <Name>" confirms the pending guessed reply
// v1.4.1: while a confirmation is pending, a plain "yes"/"ok"/"send"/"do it" confirms and "no"/"cancel"
// cancels (any case, punctuation or emoji around it). Never sent as a text while a prompt is pending.
const CONFIRM_WORDS = ['yes', 'y', 'yep', 'yeah', 'yes please', 'ok', 'okay', 'send', 'send it', 'do it'];
const CANCEL_WORDS = ['no', 'n', 'nope', 'cancel', 'cancel it', 'dont', "don't", 'dont send', "don't send"];
function bareConfirmWord(text) {
  const t = String(text || '').toLowerCase().replace(/[^a-z' ]+/g, ' ').split(/\s+/).filter(Boolean).join(' ');
  if (CONFIRM_WORDS.indexOf(t) >= 0) return 'yes';
  if (CANCEL_WORDS.indexOf(t) >= 0) return 'no';
  return null;
}
// v1.3.8 /test: an end-to-end check. Exactly "/test" or "/test send" (send = also text the owner's OWN
// number); anything else after /test is not a test command. Never routed as a reply, never texts a contact.
const TEST_RE = /^\s*\/test(?:\s+(send))?\s*$/i;
const TEST_HOST_TIMEOUT_MS = 45 * 1000;     // host's own steps have shorter timeouts (watcher 12s, send 25s, POST 6s)
const TEST_ROUTINE_WINDOW_MS = 3 * 60 * 1000; // the routine's "🧪 relay test #tag" report must appear in X Chat by then
const TEST_SIGIL = '\u{1F9EA} relay test';

function extVersion() {
  try { return String(chrome.runtime.getManifest().version || ''); } catch (e) { return ''; }
}

// ─── handle normalization (same rules as relay_common.normalize_handle on the Mac) ───
const E164_RE = /^\+[1-9][0-9]{6,14}$/;
const EMAIL_RE = /^[^\s@"\\]+@[^\s@"\\]+\.[^\s@"\\]+$/;
function normalizeHandle(raw, defaultCountry) {
  let h = String(raw == null ? '' : raw).trim();
  if (!h) return '';
  if (/^mailto:/i.test(h)) h = h.slice(7);
  if (h.includes('@')) return h.toLowerCase();
  if (/^tel:/i.test(h)) h = h.slice(4);
  const cc = String(defaultCountry || '1').replace(/[^0-9]/g, '') || '1';
  const plus = h.startsWith('+');
  let d = h.replace(/[^0-9]/g, '');
  if (!d) return '';
  if (plus) return '+' + d;
  if (d.startsWith('00')) return '+' + d.slice(2);
  if (d.length === 10) return '+' + cc + d;
  if (d.length === 11 && d[0] === '1') return '+' + d;
  return '+' + d;
}
function validHandle(h) { return E164_RE.test(h) || EMAIL_RE.test(h); }

// ─── serialized storage writes ───
let chain = Promise.resolve();
function enqueue(fn) {
  chain = chain.then(fn).catch(e => console.warn(LOG_PREFIX, 'storage error', e));
  return chain;
}

// ─── host config ───
let cfgCache = null;
let cfgInflight = null;
async function fetchConfig() {
  let res, err = '';
  try {
    if (!chrome.runtime.sendNativeMessage) throw new Error('nativeMessaging permission missing');
    res = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'get_config', extVersion: extVersion() });
    if (!res || res.ok !== true) err = (res && res.error) || 'bad response from host';
  } catch (e) {
    err = String((e && e.message) || e);
  }
  const contacts = (!err && Array.isArray(res.contacts)) ? res.contacts.filter(c => c && c.handle).map(c => ({
    name: String(c.name || '').slice(0, 80),
    label: String(c.label || c.name || '').slice(0, 40),
    handle: normalizeHandle(c.handle),
  })) : [];
  const relayConversationId = err ? '' : String(res.relayConversationId || '').trim();
  cfgCache = err ? { ok: false, error: err, paused: false, dryRun: true, contacts: [], relayConversationId: '', fetchedAt: Date.now() } : {
    ok: true, paused: !!res.paused, dryRun: res.dryRun !== false, contacts, relayConversationId, relayAll: res.relayAll === true,
    hostVersion: String(res.version || ''), pendingSelfTest: res.pendingSelfTest || null,
    ownerNames: res.ownerXHandle ? [String(res.ownerXHandle).replace(/^@/, '')] : [],
    senderNames: res.senderXHandle ? [String(res.senderXHandle).replace(/^@/, '')] : [],
    ownerXUserId: String(res.ownerXUserId || ''),
    fetchedAt: Date.now(),
  };
  const prevStatus = (await chrome.storage.local.get('status')).status || {};
  await enqueue(() => chrome.storage.local.set({
    paused: cfgCache.paused,
    status: {
      hostConnected: cfgCache.ok, hostError: cfgCache.error || '', checkedAt: new Date().toISOString(),
      paused: cfgCache.paused, dryRun: cfgCache.dryRun, contacts: cfgCache.contacts.map(c => c.label || c.name || '(unnamed)'),
      relayChatSet: !!cfgCache.relayConversationId, relayAll: !!cfgCache.relayAll, hostVersion: cfgCache.hostVersion || '',
      // item 6: wrong-account is sticky across config refresh; only a positive owner check (account_ok) clears it.
      wrongAccount: !!prevStatus.wrongAccount, wrongAccountAt: prevStatus.wrongAccountAt || '',
    },
    relayConversationId: cfgCache.relayConversationId || '',
    ownerNames: cfgCache.ownerNames || [], senderNames: cfgCache.senderNames || [], ownerXUserId: cfgCache.ownerXUserId || '',
  }));
  await refreshBadge();
  if (cfgCache.pendingSelfTest) runSelfTest(cfgCache.pendingSelfTest).catch(() => {});
  return cfgCache;
}
function getConfig(force) {
  if (!force && cfgCache && cfgCache.ok && Date.now() - cfgCache.fetchedAt < CONFIG_TTL_MS) return Promise.resolve(cfgCache);
  if (!cfgInflight) cfgInflight = fetchConfig().finally(() => { cfgInflight = null; });
  return cfgInflight;
}

// ─── self-test relay ───
const selfTestsTried = new Set();
async function runSelfTest(req) {
  const id = String((req && req.id) || '');
  if (!id || selfTestsTried.has(id)) return;
  selfTestsTried.add(id);
  let res;
  try { res = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'selftest', id }); }
  catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
  await addEvent({ kind: 'selftest', status: res && res.ok ? 'sent' : 'error', detail: (res && (res.error || res.detail)) || '' });
}

// ─── events (no message bodies) ───
async function addEvent(ev) {
  return enqueue(async () => {
    const { events = [] } = await chrome.storage.local.get('events');
    events.push(Object.assign({ ts: new Date().toISOString() }, ev));
    await chrome.storage.local.set({ events: events.slice(-MAX_EVENTS) });
  });
}
async function updateEvent(key, patch) {
  return enqueue(async () => {
    const { events = [] } = await chrome.storage.local.get('events');
    const ev = events.find(e => e.key === key);
    if (!ev) return;
    Object.assign(ev, patch, { updatedAt: new Date().toISOString() });
    const add = patch.status === 'sent' ? 1 : 0;
    const { sentCount = 0 } = await chrome.storage.local.get('sentCount');
    await chrome.storage.local.set({ events, sentCount: sentCount + add });
  }).then(refreshBadge);
}

// ─── badge: II = paused, ! = host down, ACC = wrong X account, number = replies sent ───
async function refreshBadge() {
  const { status = {}, sentCount = 0 } = await chrome.storage.local.get(['status', 'sentCount']);
  let text = sentCount ? String(sentCount) : '', color = '#1d9bf0';
  if (status.hostConnected === false) { text = '!'; color = '#f4212e'; }
  else if (status.wrongAccount) { text = 'ACC'; color = '#f4212e'; }
  else if (status.paused) { text = 'II'; color = '#888888'; }
  else if (status.dryRun && !sentCount) { text = 'DRY'; color = '#8b98a5'; }
  try { await chrome.action.setBadgeText({ text }); await chrome.action.setBadgeBackgroundColor({ color }); } catch (e) { /* no action API in tests */ }
}

// ─── reply gating ───
function decide(item, cfg) {
  if (item.direction !== 'outbound') return { ignore: true };
  if (cfg.ok && (!cfg.relayConversationId || item.conversationId !== cfg.relayConversationId)) return { ignore: true };
  const base = { contactName: '' };
  if (!cfg.ok) return Object.assign(base, { skip: 'host not connected: ' + cfg.error });
  if (cfg.paused) return Object.assign(base, { skip: 'paused' });
  if (!(Number(item.score) >= MIN_NATIVE_SCORE)) return Object.assign(base, { skip: 'direction score ' + item.score + ' < ' + MIN_NATIVE_SCORE });
  if (!cfg.relayAll && !(cfg.contacts || []).some(c => validHandle(c.handle))) return Object.assign(base, { skip: 'no contacts in config' });
  const age = Date.now() - (Date.parse(item.ts) || 0);
  if (!(age >= -5000 && age <= MAX_ITEM_AGE_MS)) return Object.assign(base, { skip: 'stale item' });
  if (!item.text || item.text === '[no text]') return Object.assign(base, { skip: 'empty text' });
  return base;
}

const recentKeys = new Map();
async function isDuplicate(key) {
  if (!key) return false;
  const t = Date.now();
  for (const [k, v] of recentKeys) if (t - v > DEDUPE_MS) recentKeys.delete(k);
  if (recentKeys.has(key)) return true;
  recentKeys.set(key, t);
  const { events = [] } = await chrome.storage.local.get('events');
  return events.some(e => e.key === key && t - (Date.parse(e.ts) || 0) < DEDUPE_MS);
}

// A reply the content script refused to forward (over the host length limit): owner notice, no host call.
async function handleRefusal(item) {
  if (await isDuplicate(item.key)) return;
  await addEvent({
    kind: 'reply', key: item.key, contactName: '', textLength: item.textLength || 0,
    status: 'refused', detail: 'reply too long to relay (' + (item.textLength || 0) + ' chars, limit ' + (item.limit || 4000) + ') — nothing sent, shorten it',
  });
}

let pendingGuessMem = null;   // fallback store when chrome.storage.session is unavailable
async function setPendingGuess(g) {
  if (chrome.storage && chrome.storage.session) {
    try { await chrome.storage.session.set({ pendingGuess: g }); return; } catch (e) { /* fall through */ }
  }
  pendingGuessMem = g;
}
async function getPendingGuess() {
  if (chrome.storage && chrome.storage.session) {
    try { return (await chrome.storage.session.get('pendingGuess')).pendingGuess || null; } catch (e) { /* fall through */ }
  }
  return pendingGuessMem;
}
async function clearPendingGuess() {
  if (chrome.storage && chrome.storage.session) {
    try { await chrome.storage.session.remove('pendingGuess'); } catch (e) { /* ignore */ }
  }
  pendingGuessMem = null;
  try { if (chrome.alarms) await chrome.alarms.clear('relay-guess-expiry'); } catch (e) { /* ignore */ }
}

// item 3: the owner typed "/yes" (optionally "/yes <Name>"). Re-send the STORED text with confirmed:true
// so the host skips the guess check. The raw "/yes" is never forwarded to the host.
async function handleCancel(item, g) {
  await clearPendingGuess();
  await addEvent({ kind: 'reply', key: item.key, contactName: g.label || '', status: 'skipped', detail: 'cancelled' });
  try { await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'signal', status: 'guess_cancelled', label: g.label || '' }); } catch (e) { /* host may be down */ }
}

async function handleYes(item, nameArg) {
  const g = await getPendingGuess();
  if (!g || Date.now() - (g.at || 0) > GUESS_WINDOW_MS) {
    await clearPendingGuess();
    await addEvent({ kind: 'reply', key: item.key, contactName: '', status: 'skipped', detail: 'nothing to confirm' });
    return;
  }
  if (nameArg && String(nameArg).trim() && String(nameArg).trim().toLowerCase() !== String(g.label || '').toLowerCase()) {
    await addEvent({ kind: 'reply', key: item.key, contactName: g.label || '', status: 'skipped',
                     detail: 'that name does not match the reply waiting for ' + (g.label || 'them') });
    return;
  }
  await clearPendingGuess();
  const payload = { type: 'send', text: g.text, quotedText: g.quotedText || '', conversationId: g.conversationId, ts: item.ts, confirmed: true, handle: g.handle || '' };
  const ev = { kind: 'reply', key: item.key, contactName: g.label || '', textLength: (g.text || '').length, status: 'sending' };
  await addEvent(ev);
  let res, status, detail;
  try {
    res = await chrome.runtime.sendNativeMessage(NATIVE_HOST, payload);
    if (!res) { status = 'error'; detail = 'no response'; }
    else if (!res.ok) { status = 'refused'; detail = res.error || 'host refused'; }
    else if (res.dryRun) { status = 'dry-run'; detail = 'host logged it, nothing sent'; }
    else { status = 'sent'; detail = res.delivered === true ? 'delivered' : 'delivery unknown'; }
  } catch (e) { status = 'error'; detail = String((e && e.message) || e); cfgCache = null; }
  const patch = { status, detail };
  if (res && res.contact) { patch.contactName = String(res.contact).slice(0, 40); patch.routedBy = String(res.routedBy || ''); }
  await updateEvent(item.key, patch);
}

// ─── v1.3.8 /test ───
let pendingTestMem = null;
async function setPendingTest(t) {
  if (chrome.storage && chrome.storage.session) {
    try { await chrome.storage.session.set({ pendingTest: t }); return; } catch (e) { /* fall through */ }
  }
  pendingTestMem = t;
}
async function getPendingTest() {
  if (chrome.storage && chrome.storage.session) {
    try { return (await chrome.storage.session.get('pendingTest')).pendingTest || null; } catch (e) { /* fall through */ }
  }
  return pendingTestMem;
}
async function clearPendingTest() {
  if (chrome.storage && chrome.storage.session) {
    try { await chrome.storage.session.remove('pendingTest'); } catch (e) { /* ignore */ }
  }
  pendingTestMem = null;
  try { if (chrome.alarms) await chrome.alarms.clear('relay-test-expiry'); } catch (e) { /* ignore */ }
}
function withTimeout(p, ms) {
  let timer;
  return Promise.race([p, new Promise((_, rej) => { timer = setTimeout(() => rej(new Error('timed out after ' + Math.round(ms / 1000) + 's')), ms); })])
    .finally(() => clearTimeout(timer));
}
function randomTestId() {
  const b = new Uint8Array(6);
  try { crypto.getRandomValues(b); } catch (e) { for (let i = 0; i < b.length; i++) b[i] = Math.floor(Math.random() * 256); }
  return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
}
function testSummary(steps) {
  const failed = (steps || []).filter(s => s && s.ok === false).map(s => String(s.name || '?'));
  return failed.length ? failed.join(', ') + ' failed' : 'all host checks ok';
}

// The owner typed "/test" in the relay chat. Runs even while paused or with no contacts (it never
// sends a reply). Every step is time-boxed so a missing piece is reported as failed, not a hang.
async function handleTest(item, sendArg) {
  const cfg = await getConfig(true);
  if (cfg.ok && (!cfg.relayConversationId || item.conversationId !== cfg.relayConversationId)) return;   // not the relay chat
  if (!(Number(item.score) >= MIN_NATIVE_SCORE)) return;
  const age = Date.now() - (Date.parse(item.ts) || 0);
  if (!(age >= -5000 && age <= MAX_ITEM_AGE_MS)) return;
  if (await isDuplicate(item.key)) return;
  await addEvent({ kind: 'test', key: item.key, contactName: '', status: 'testing', detail: '1 extension saw /test' });
  if (!cfg.ok) {   // the only place this can be reported: the host (and so the webhook) is unreachable
    await updateEvent(item.key, { status: 'test-failed', detail: '2 native host not reachable: ' + String(cfg.error || '').slice(0, 120) });
    return;
  }
  const testId = randomTestId();
  let res;
  try {
    res = await withTimeout(chrome.runtime.sendNativeMessage(NATIVE_HOST, {
      type: 'test', testId, send: !!sendArg, extVersion: extVersion(), conversationId: item.conversationId,
    }), TEST_HOST_TIMEOUT_MS);
  } catch (e) {
    cfgCache = null;
    await updateEvent(item.key, { status: 'test-failed', detail: '2 native host did not finish: ' + String((e && e.message) || e).slice(0, 120) });
    return;
  }
  if (!res || res.command !== 'test') {
    await updateEvent(item.key, { status: 'test-failed', detail: 'host refused /test: ' + String((res && res.error) || 'no response').slice(0, 120) });
    return;
  }
  if (!res.webhookAccepted) {
    await updateEvent(item.key, { status: 'test-failed', detail: testSummary(res.steps) + ' (nothing could reach X Chat)' });
    return;
  }
  await setPendingTest({ testId: String(res.testId || testId), tag: String(res.tag || '').slice(0, 6), key: item.key,
                         conversationId: item.conversationId, at: Date.now() });
  try { if (chrome.alarms) chrome.alarms.create('relay-test-expiry', { delayInMinutes: TEST_ROUTINE_WINDOW_MS / 60000 }); } catch (e) { /* tests */ }
  await updateEvent(item.key, { status: 'test-waiting', detail: testSummary(res.steps) + '; waiting for the routine report' });
}

// A bubble in the relay chat that is NOT the owner's: is it the routine's report for the pending test?
// Matched on the exact header prefix + this test's tag, so a relayed text ("Sam: 🧪 …") never matches.
async function maybeTestReport(item) {
  const t = await getPendingTest();
  if (!t || !t.tag || item.conversationId !== t.conversationId) return false;
  const text = String(item.text || '').replace(/^\s+/, '');
  if (!text.startsWith(TEST_SIGIL + ' #' + t.tag)) return false;
  await clearPendingTest();
  const failed = /\u274C/.test(text);
  await updateEvent(t.key, { status: failed ? 'test-failed' : 'test-ok', detail: failed ? 'report posted; some steps failed (see X Chat)' : 'all steps passed; routine posted the report' });
  try { await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'signal', status: 'test_seen', testId: t.testId }); } catch (e) { /* host may be down */ }
  return true;
}
async function onTestExpiry() {
  const t = await getPendingTest();
  if (!t) return;
  if (Date.now() - (t.at || 0) < TEST_ROUTINE_WINDOW_MS) return;
  await clearPendingTest();
  await updateEvent(t.key, { status: 'test-failed', detail: '6 routine did not post the report within 3 min' });
  try { await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'signal', status: 'test_unseen', testId: t.testId }); } catch (e) { /* host may be down */ }
}

async function handleItem(item) {
  if (item.refuse) { await handleRefusal(item); return; }
  if (item.direction !== 'outbound') { await maybeTestReport(item); return; }
  const tm = TEST_RE.exec(String(item.text || ''));
  if (tm) {                                   // "/test [send]": end-to-end check, never a reply (v1.3.8)
    await handleTest(item, tm[1]);
    return;
  }
  const bw = bareConfirmWord(item.text);
  if (bw) {                                   // v1.4.1: plain "yes" / "no" answers a pending reply prompt
    const c0 = await getConfig(false);
    if (c0.ok && c0.relayConversationId && item.conversationId !== c0.relayConversationId) return;
    const g = await getPendingGuess();
    if (g && Date.now() - (g.at || 0) <= GUESS_WINDOW_MS) {
      if (await isDuplicate(item.key)) return;
      if (bw === 'yes') await handleYes(item, '');
      else await handleCancel(item, g);
      return;
    }                                         // nothing pending here: the host checks its own prompts
  }
  const ym = YES_RE.exec(String(item.text || ''));
  if (ym) {                                   // "/yes [Name]" confirms a pending guessed reply (item 3)
    if (await isDuplicate(item.key)) return;
    await handleYes(item, ym[1]);
    return;
  }
  const cfg = await getConfig(false);
  const d = decide(item, cfg);
  if (d.ignore) return;
  if (await isDuplicate(item.key)) return;
  const ev = { kind: 'reply', key: item.key, contactName: d.contactName, textLength: (item.text || '').length };
  if (d.skip) { await addEvent(Object.assign(ev, { status: 'skipped', detail: d.skip })); return; }
  await addEvent(Object.assign(ev, { status: 'sending' }));
  const payload = { type: 'send', text: item.text, quotedText: item.quotedText || '', conversationId: item.conversationId, ts: item.ts };
  console.log(LOG_PREFIX, 'forwarding reply to host', { conversationId: item.conversationId, textLength: item.text.length, quoted: !!item.quotedText });
  let res, status, detail;
  try {
    res = await chrome.runtime.sendNativeMessage(NATIVE_HOST, payload);
    if (!res) { status = 'error'; detail = 'no response'; }
    else if (res.needsConfirm && res.handle) {  // item 3 / W1: hold the text in session; confirm with /yes
      await setPendingGuess({ text: item.text, label: String(res.label || ''), handle: String(res.handle || ''),
                              conversationId: item.conversationId, quotedText: item.quotedText || '', at: Date.now() });
      try { if (chrome.alarms) chrome.alarms.create('relay-guess-expiry', { delayInMinutes: 2 }); } catch (e) { /* tests */ }
      status = 'needs-confirm';
      detail = (res.reason === 'new_contact')
        ? 'text ' + (res.label || 'them') + ' (from Contacts)? reply yes within 2 min'
        : (res.reason === 'directive')
        ? 'send to ' + (res.label || 'them') + '? reply yes within 2 min'
        : 'reply to ' + (res.label || 'them') + '? reply yes within 2 min';
    }
    else if (res.needsTarget) { status = 'needs-name'; detail = res.error || "Who's this for? Start with a name."; }
    else if (!res.ok) { status = 'refused'; detail = res.error || 'host refused'; }
    else if (res.skipped) { status = 'skipped-by-host'; detail = res.skipped; }
    else if (res.dryRun) { status = 'dry-run'; detail = 'host logged it, nothing sent'; }
    else { status = 'sent'; detail = res.delivered === true ? 'delivered' : res.delivered === false ? 'not confirmed delivered' : 'delivery unknown'; }
  } catch (e) {
    status = 'error'; detail = String((e && e.message) || e);
    cfgCache = null;
  }
  const patch = { status, detail };
  if (res && res.contact) { patch.contactName = String(res.contact).slice(0, 40); patch.routedBy = String(res.routedBy || ''); }
  await updateEvent(item.key, patch);
}

// v1.4.2: the owner put a thumbs-up / thumbs-down on the newest pending prompt (content.js checks the
// prompt, the 2-minute window and the owner identity). A pending reply prompt is answered here (the text
// is held here); a /block or /add prompt is answered by the host. A reaction is never sent as a message.
async function handleReaction(r) {
  const word = r && (r.word === 'yes' || r.word === 'no') ? r.word : '';
  if (!word) return;
  const cfg = await getConfig(false);
  if (!cfg.ok || !cfg.relayConversationId || String(r.conversationId || '') !== cfg.relayConversationId || cfg.paused) return;
  const key = String(r.key || '').slice(0, 300);
  if (await isDuplicate(key)) return;
  const item = { key, conversationId: cfg.relayConversationId, ts: String(r.ts || new Date().toISOString()), text: '' };
  const g = await getPendingGuess();
  if (g && Date.now() - (g.at || 0) <= GUESS_WINDOW_MS) {
    if (word === 'yes') await handleYes(item, '');
    else await handleCancel(item, g);
    return;
  }
  try { await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'reaction', word, conversationId: cfg.relayConversationId }); } catch (e) { /* host down */ }
}

function sanitizeItem(raw) {
  const dirs = ['outbound', 'inbound', 'unknown'];
  const out = {
    ts: String(raw.ts || new Date().toISOString()),
    conversationId: String(raw.conversationId || ''),
    direction: dirs.includes(raw.direction) ? raw.direction : 'unknown',
    text: String(raw.text || '').slice(0, 4000),
    quotedText: String(raw.quotedText || '').slice(0, 500),
    key: String(raw.key || '').slice(0, 300),
    score: Number.isFinite(Number(raw.score)) ? Number(raw.score) : 0,
    source: ['frame', 'top', 'page'].includes(raw.source) ? raw.source : 'page',
  };
  if (raw.refuse) { out.refuse = String(raw.refuse).slice(0, 40); out.textLength = Number(raw.textLength) || 0; out.limit = Number(raw.limit) || 4000; }
  return out;
}

// ─── wrong-account status (content signal) ───
async function setWrongAccount(on) {
  let transitioned = false, at = '';
  await enqueue(async () => {
    const { status = {} } = await chrome.storage.local.get('status');
    transitioned = on && !status.wrongAccount;
    status.wrongAccount = !!on;
    if (on) status.wrongAccountAt = new Date().toISOString();
    else delete status.wrongAccountAt;
    at = status.wrongAccountAt || '';
    await chrome.storage.local.set({ status });
  });
  await refreshBadge();
  // One body-free webhook so X Chat shows the owner why relaying stopped (no message text involved).
  // Carry the episode timestamp so each wrong-account EPISODE gets a unique webhook id (item 6): a
  // later episode is not swallowed by the routine's 24h id dedupe, while repeats inside one episode
  // keep the same id and stay deduped.
  if (transitioned) {
    try { await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'signal', status: 'wrong_account', at }); } catch (e) { /* host may be down */ }
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || sender.id !== chrome.runtime.id) return false;
  if (msg.type === 'relay-log' && msg.item && sender.tab) {
    handleItem(sanitizeItem(msg.item)).catch(e => console.warn(LOG_PREFIX, 'reply handling failed', e));
    return false;
  }
  if (msg.type === 'relay-signal' && msg.signal && sender.tab) {
    if (msg.signal.status === 'wrong_account') setWrongAccount(true).catch(() => {});
    else if (msg.signal.status === 'account_ok') setWrongAccount(false).catch(() => {});
    return false;
  }
  if (msg.type === 'relay-reaction' && msg.item && sender.tab) {
    handleReaction(msg.item).catch(e => console.warn(LOG_PREFIX, 'reaction handling failed', e));
    return false;
  }
  if (msg.type === 'relay-hello') return false;   // frame instance announced itself; no coordination needed
  if (msg.type === 'relay-status') {
    getConfig(true).then(async () => {
      const { status = {}, events = [] } = await chrome.storage.local.get(['status', 'events']);
      sendResponse({ status, events: events.slice(-5).reverse() });
    });
    return true;
  }
  return false;
});

// Re-check the host every minute.
function boot() {
  try { chrome.alarms.create('relay-refresh', { periodInMinutes: 1 }); } catch (e) { /* tests */ }
  getConfig(true).catch(() => {});
}
// On install or update, reload any open X Chat tabs so the new content script is the one running.
async function reloadChatTabs() {
  try {
    const tabs = await chrome.tabs.query({ url: ['https://x.com/i/chat/*', 'https://x.com/messages/*'] });
    for (const t of tabs) { try { await chrome.tabs.reload(t.id); } catch (e) { /* ignore */ } }
  } catch (e) { /* tabs API may be unavailable in tests */ }
}
async function onGuessExpiry() {
  const g = await getPendingGuess();
  if (!g) return;
  if (Date.now() - (g.at || 0) < GUESS_WINDOW_MS) return;   // a newer guess reset the clock
  await clearPendingGuess();
  try { await chrome.runtime.sendNativeMessage(NATIVE_HOST, { type: 'signal', status: 'guess_expired', label: g.label || '' }); } catch (e) { /* host may be down */ }
}
if (chrome.alarms && chrome.alarms.onAlarm) chrome.alarms.onAlarm.addListener(a => {
  if (a.name === 'relay-refresh') getConfig(true).catch(() => {});
  else if (a.name === 'relay-guess-expiry') onGuessExpiry().catch(() => {});
  else if (a.name === 'relay-test-expiry') onTestExpiry().catch(() => {});
});
chrome.runtime.onInstalled.addListener(details => { boot(); if (!details || details.reason === 'install' || details.reason === 'update') reloadChatTabs(); });
chrome.runtime.onStartup.addListener(boot);
