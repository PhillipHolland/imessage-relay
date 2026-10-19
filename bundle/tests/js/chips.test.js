/* v1.3.9: X Chat time/status chips rendered as plain spans inside message-text-<uuid> must never
 * reach the iMessage body. Run: NODE_PATH=/path/to/node_modules node tests/js/chips.test.js */
'use strict';
const assert = require('assert');
const { JSDOM } = require('jsdom');
const { createRelayDetector } = require('../../extension/content.js');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('PASS', name); }
  catch (e) { failed++; console.log('FAIL', name, '\n   ', e.message); }
}
const dom = new JSDOM('<!doctype html><body></body>', { url: 'https://x.com/i/chat/111-222' });
const { window } = dom;
const doc = window.document;
const det = createRelayDetector({ window, document: doc, log: () => {} });
function ex(html) {
  const el = doc.createElement('span');
  el.setAttribute('data-testid', 'message-text-df48633d-6588-4b18-bc77-000000000001');
  el.innerHTML = html;
  return det.extractText(el);
}
const S = det.stripTrailingChips;

// --- post-process (string level) ---
test('exact delivered example: love-you + doubled 9:07 AM', () => {
  assert.strictEqual(S('Good morning - love you so!!\u2764\ufe0f\n9:07 AM\n\n9:07 AM'), 'Good morning - love you so!!\u2764\ufe0f');
});
test('exact earlier example: test + doubled 10:54 PM', () => {
  assert.strictEqual(S('test\n10:54 PM\n\n10:54 PM'), 'test');
});
test('24h clock, seconds, lowercase/dotted am-pm, status words, any repetition', () => {
  assert.strictEqual(S('hi\n21:07'), 'hi');
  assert.strictEqual(S('hi\n21:07:33\n\n\n21:07:33\n21:07:33'), 'hi');
  assert.strictEqual(S('hi\n9:07 a.m.\n9:07pm'), 'hi');
  assert.strictEqual(S('hi\nEdited\n\nSent\nSeen\nDelivered'), 'hi');
  assert.strictEqual(S('hi\nEdited \u00b7 9:07 AM\n\n9:07\u202fAM'), 'hi');
});
test('clock times inside the message are kept', () => {
  assert.strictEqual(S('meet at 9:07 AM ok?\n9:07 AM'), 'meet at 9:07 AM ok?');
  assert.strictEqual(S('line one\n9:07 AM\nline three\n9:08 AM\n\n9:08 AM'), 'line one\n9:07 AM\nline three');
  assert.strictEqual(S('see you at 10:54 PM'), 'see you at 10:54 PM');
  assert.strictEqual(S('I sent it'), 'I sent it');
});
test('a message that is only a time keeps its first line; plain text untouched', () => {
  assert.strictEqual(S('9:07 AM\n9:07 AM\n\n9:07 AM'), '9:07 AM');
  assert.strictEqual(S('9:07 AM'), '9:07 AM');
  assert.strictEqual(S('Sent'), 'Sent');
  assert.strictEqual(S('hello\n\nworld'), 'hello\n\nworld');
});

// --- DOM level (X's plain-span chips) ---
test('DOM: visible + screen-reader time spans after the text are dropped', () => {
  assert.strictEqual(ex('<span>Good morning - love you so!!\u2764\ufe0f</span><div><span>9:07 AM</span></div><div><span aria-hidden="true">9:07 AM</span></div>'),
    'Good morning - love you so!!\u2764\ufe0f');
  assert.strictEqual(ex('test<div>10:54 PM</div><div></div><div>10:54 PM</div>'), 'test');
  assert.strictEqual(ex('test<span>10:54 PM</span><span class="sr-only">10:54\u202fPM</span>'), 'test');
  assert.strictEqual(ex('hi<div><span>Edited</span><span>9:07 AM</span></div><span>Seen</span>'), 'hi');
});
test('DOM: a time span in the middle of the text is kept', () => {
  assert.strictEqual(ex('meet at <span>9:07 AM</span> sharp<div>9:07 AM</div>'), 'meet at 9:07 AM sharp');
  assert.strictEqual(ex('<div>first</div><div>9:07 AM</div><div>last</div><div>9:09 AM</div><div>9:09 AM</div>'), 'first\n\n9:07 AM\n\nlast');   // same block spacing as plain divs
});
test('DOM: a body that is only a time keeps one copy', () => {
  assert.strictEqual(ex('<span>9:07 AM</span><span>9:07 AM</span><span>9:07 AM</span>'), '9:07 AM');
});
test('DOM: old markup still works (<time>, testid chips, line breaks)', () => {
  assert.strictEqual(ex('a<br>b<div>c</div><time datetime="2026-10-19T14:07:00Z">9:07 AM</time><span data-testid="read-receipt">Seen</span>'), 'a\nb\nc');
});

console.log(passed + ' passed, ' + failed + ' failed (v1.3.9 chips)');
process.exit(failed ? 1 : 0);
