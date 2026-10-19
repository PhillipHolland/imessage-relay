/* item 1 regression: the content-script owner-identity gate (ownerConfirmed()). When ownerXUserId is
 * set, the twid cookie is parsed exactly (u%3D<digits>) and compared by equality - no prefix/substring
 * match, no fallback on mismatch. The avatar fallback runs only when there is no readable twid cookie,
 * is scoped to the account switcher (or, in layouts without one, to avatars outside the conversation
 * panel), and is case-insensitive on the handle. Runs the real content.js ownerConfirmed().
 *   NODE_PATH=/path/to/node_modules node tests/js/owner.test.js
 */
'use strict';
const fs = require('fs'), path = require('path'), assert = require('assert');
const { JSDOM } = require('jsdom');
const src = fs.readFileSync(path.join(__dirname, '../../extension/content.js'), 'utf8');
const fn = src.match(/function ownerConfirmed\(\) \{[\s\S]*?\n  \}\n/)[0];

function check(cookie, html, uid, handle) {
  const dom = new JSDOM('<body>' + html + '</body>', { url: 'https://x.com/i/chat/1-2' });
  const doc = dom.window.document; if (cookie) doc.cookie = cookie;
  const CONFIG = { ownerXUserId: uid, ownerXHandle: handle };          // eslint-disable-line no-unused-vars
  const topDoc = () => doc; const document = doc;                       // eslint-disable-line no-unused-vars
  return eval('(' + fn + ')')();
}
const sw = h => `<nav><div data-testid="SideNav_AccountSwitcher_Button"><div data-testid="UserAvatar-Container-${h}"></div></div></nav>`;
const header = h => `<div data-testid="dm-conversation-panel"><div data-testid="UserAvatar-Container-${h}"></div></div>`;
const inbox = h => `<div data-testid="dm-inbox-panel"><a href="/i/chat/1-2"><div data-testid="UserAvatar-Container-${h}"></div></a></div>`;
const mainHeader = h => `<div role="main"><header><div data-testid="UserAvatar-Container-${h}"></div></header></div>`;
const bareNav = h => `<nav data-testid="AppTabBar_Home_Link">nav</nav><div data-testid="UserAvatar-Container-${h}"></div><div data-testid="primaryColumn"></div>`;

let passed = 0, failed = 0;
function t(name, got, want) {
  try { assert.strictEqual(got, want); passed++; console.log('PASS', name); }
  catch (e) { failed++; console.log('FAIL', name, '-> got', got, 'want', want); }
}

t('T1 exact twid matches', check('twid=u%3D1234567890', '', '1234567890', ''), true);
t('T2 prefix twid (12345 vs 1234567890) is NOT a match', check('twid=u%3D1234567890', '', '12345', ''), false);
t('T3 twid present (sender) + owner avatar in convo header -> false', check('twid=u%3D999', sw('SenderBot') + header('OwnerAcct'), '111', 'OwnerAcct'), false);
t('T4 no uid: account-switcher avatar, case-insensitive handle', check('', sw('OwnerAcct'), '', 'owneracct'), true);
t('T5 no uid and no handle -> false', check('', sw('OwnerAcct'), '', ''), false);
t('T6 uid set but no readable twid + only a header avatar -> false', check('', header('SenderBot'), '111', 'OwnerAcct'), false);

// item 1 (v1.3.3) T7-T9: fail CLOSED when there is no readable twid AND no account switcher. An owner
// avatar drawn inside the inbox list, a header, or the main column is not proof of the logged-in
// account; only the account-nav avatar (outside every conversation/inbox/header region) counts. The
// twid parser also accepts the quoted / legacy u= forms.
t('T7 no twid, no switcher, owner avatar in inbox list -> false', check('', inbox('OwnerAcct') + header('OwnerAcct'), '111', 'OwnerAcct'), false);
t('T8 no twid, no switcher, owner avatar in <header> under role=main -> false', check('', mainHeader('OwnerAcct'), '111', 'OwnerAcct'), false);
t('T9 quoted legacy twid "u=999" (sender) is parsed and compared -> false', check('twid="u=999"', inbox('OwnerAcct'), '111', 'OwnerAcct'), false);
t('T10 no twid, no switcher, account-nav avatar outside any panel -> true', check('', bareNav('OwnerAcct'), '', 'OwnerAcct'), true);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
