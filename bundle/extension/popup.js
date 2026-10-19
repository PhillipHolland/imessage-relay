'use strict';
// Status view only: on/paused/dry run/wrong-account, host connected yes/no, last 5 events. No settings here.
const $ = id => document.getElementById(id);

function renderEvents(events) {
  const list = $('list');
  list.textContent = '';
  if (!events.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'Nothing yet. Reply in the relay chat on x.com to test.';
    list.appendChild(li);
    return;
  }
  for (const ev of events) {             // all text via textContent; events never hold message bodies
    const li = document.createElement('li');
    const s = document.createElement('span');
    s.className = 'st ' + String(ev.status || '').replace(/[^a-z-]/g, '');
    s.textContent = ev.status || '?';
    const meta = document.createElement('span');
    meta.className = 'muted';
    meta.textContent = ' ' + new Date(ev.updatedAt || ev.ts).toLocaleTimeString() + ' · ' +
      (ev.kind === 'selftest' ? 'self-test' : ev.kind === 'test' ? '/test' : ev.contactName ? 'reply to ' + ev.contactName + (ev.routedBy ? ' (by ' + ev.routedBy + ')' : '') : 'reply') +
      (ev.textLength ? ' · ' + ev.textLength + ' chars' : '');
    const det = document.createElement('div');
    det.className = 'det';
    det.textContent = ev.detail || '';
    li.append(s, meta, det);
    list.appendChild(li);
  }
}

function render(status, events) {
  const st = $('state');
  if (status.hostConnected === false) { st.textContent = 'Helper offline'; st.className = 'pill off'; }
  else if (status.wrongAccount) { st.textContent = 'Wrong X account'; st.className = 'pill off'; }
  else if (status.paused) { st.textContent = 'Paused'; st.className = 'pill paused'; }
  else if (status.dryRun) { st.textContent = 'On (dry run)'; st.className = 'pill dry'; }
  else { st.textContent = 'On'; st.className = 'pill on'; }
  $('host').textContent = 'Mac helper connected: ' + (status.hostConnected ? 'yes' : status.hostConnected === false ? 'no' : '…') +
    (status.hostError ? ' (' + status.hostError + ')' : '') + (status.hostVersion ? ' · v' + status.hostVersion : '');
  const names = status.contacts || [];
  if (status.wrongAccount) {
    $('contacts').textContent = 'Relay is off: this browser is signed in to a different X account than the owner. Switch to the owner account (or set ownerXHandle/ownerXUserId), then reload the chat tab.';
  } else {
    $('contacts').textContent = (status.relayChatSet === false ? 'Relay chat not set (relayctl set relayConversationId <id>). ' :
      status.relayChatSet ? 'Relay chat: set. ' : '') + (status.relayAll ? 'Relaying all texts' + (names.length ? ' · contacts: ' + names.join(', ') : '') + '.' :
       names.length ? 'Contacts: ' + names.join(', ') : 'No contacts set up yet.');
  }
  renderEvents(events);
}

chrome.storage.local.get(['status', 'events']).then(v => render(v.status || {}, (v.events || []).slice(-5).reverse()));
chrome.runtime.sendMessage({ type: 'relay-status' }).then(r => { if (r) render(r.status || {}, r.events || []); }).catch(() => {});

// v1.4.3: "Copy page structure" -- a text-free outline of the open chat (tags, roles, sanitized test ids,
// attribute names, 'Replied to' yes/no flags) for fixing quote detection. Never message text, names or handles.
async function copyStructure() {
  const msg = $('structureMsg');
  msg.textContent = 'Reading…';
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error('no active tab');
    const r = await chrome.tabs.sendMessage(tab.id, { type: 'relay-structure' });
    if (!r || !r.ok) throw new Error((r && r.error) || 'open the relay chat on x.com first');
    const json = JSON.stringify(r.structure);
    try { await navigator.clipboard.writeText(json); }
    catch (e) {
      const ta = document.createElement('textarea');
      ta.value = json; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
    msg.textContent = 'Copied (' + json.length + ' chars, no message text).';
  } catch (e) {
    msg.textContent = 'Could not read the page: ' + String((e && e.message) || e).slice(0, 120);
  }
}
const sb = $('structure');
if (sb) sb.addEventListener('click', () => { copyStructure(); });
