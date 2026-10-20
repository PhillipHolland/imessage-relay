/*
 * iMessage Relay v1.3.2: content script (READ ONLY).
 *
 * Observes the X Chat page the owner already has open and reports each NEW message bubble
 * (with a direction guess) to the service worker (background.js) via chrome.runtime.sendMessage,
 * which stays inside the browser. It never types, clicks, posts, or calls any X API, and makes
 * no network requests.
 *
 * v1.3.2 (second audit): UUID-ONLY. A message is a `[data-testid="message-<uuid>"]` bubble and
 * nothing else; the <uuid> is the dedupe key. The weak / fallback / plain-text heuristics and the
 * top-frame "reach into the iframe" fallback are gone: every frame runs ONE detector on its OWN
 * document and takes the conversation id from the top URL (contextUrl). Replies are emitted only
 * when the owner's identity is positively confirmed (twid cookie u%3D<ownerXUserId>, else the
 * owner's UserAvatar-Container in the top doc); otherwise nothing is emitted and a body-free
 * wrong_account status is surfaced in the popup. Message text is read only from the bubble's own
 * message-text-<uuid> element, line breaks kept, time/status chips stripped, and an over-limit
 * reply is refused (owner notice) rather than truncated.
 *
 * v1.3.9: X Chat now renders the bubble time as plain spans inside message-text-<uuid> (twice: a
 * visible and a screen-reader copy). Trailing clock-time / status chips are dropped (element and
 * line level); clock times inside the message text are kept.
 *
 * v1.4.21: a lone emoji is not message-text-<uuid>. X draws it as a ~64px glyph (a text node or
 * an img alt) and the native-Reply preview is the previous sibling of that glyph. The message
 * row's own justify-end / justify-start is a direction signal. data-send-status is not: X sets
 * "sent" on received rows too.
 */
(function () {
  'use strict';

  const CONFIG = {
    // Filled at runtime from the Mac helper config (via chrome.storage.local): who the owner and
    // the relay-bot are, used for the direction guess and the owner-identity gate.
    ownerNames: [],
    botNames: [],
    ownerXUserId: '',          // numeric X id; twid cookie value u%3D<id> proves the owner is signed in
    ownerXHandle: '',          // @handle; UserAvatar-Container-<handle> in the top doc is the fallback proof
    logPrefix: '[imessage-relay]',

    // Conversation id, parsed from the (top) location.pathname.
    conversationUrlPatterns: [/^\/i\/chat\/([^/?#]+)/, /^\/messages\/([^/?#]+)/],
    nonConversationIds: ['compose', 'requests', 'settings', 'new', 'search', 'pin'],

    shadowRescanMs: 3000,      // re-scan for newly attached open shadow roots
    frameScanDelayMs: 3000,    // one-time structure-only "frame scan" log

    // Chat root (confirmed live): the conversation panel. If none matches, document.body is used.
    chatRootSelectors: [
      '[data-testid="dm-conversation-panel"]',
      '[data-testid="DmActivityViewport"]',
      '[data-testid="DmScrollerContainer"]',
      '[data-testid="DMConversationBody"]',
    ],

    // The ONE message selector (confirmed live): data-testid="message-<uuid>".
    uuidEntryPrefixSelector: '[data-testid^="message-"]',
    uuidEntryTestIdRegex: /^message-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i,
    // The bubble's own text lives in data-testid="message-text-<uuid>" (same uuid). Item 10: this
    // is the element the reply body is read from. v1.4.21: a lone emoji has no such node; the
    // ~64px glyph is the body instead.
    textTestIdPrefix: 'message-text-',

    // Native reply-quote: the snippet of the replied-to message shown inside the owner's bubble.
    // Kept OUT of the reply body and sent separately as quotedText (host reads the "Name:" prefix
    // to route; never stored). Best effort.
    quoteTestIdPattern: /quot|reply[-_]?(?:preview|context|snippet|to|message|parent)|replied|in[-_]?reply[-_]?to/i,
    quoteSelectors: ['blockquote'],
    maxQuoteLength: 500,

    // Dropped from the body text: <time>, [datetime], and testids that read as time/status/receipts.
    stripTestIdPattern: /time|timestamp|status|receipt|seen|edited|delivered|reaction/i,
    // v1.3.9: X Chat now renders the bubble time as plain <span>s (a visible copy plus a screen-reader
    // copy) INSIDE message-text-<uuid>, with no <time>/[datetime]/testid. A "chip" is an element (or a
    // line) whose whole text is a clock time ("9:07 AM", "21:07") or a status word (Edited/Sent/Seen/
    // Delivered), optionally combined ("Edited · 9:07 AM"). Chips are dropped ONLY at the end of the
    // body, after real text; a clock time inside the message is never touched.
    chipLinePattern: /^(?:(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?\s?m\.?)?|edited|sent|seen|delivered)(?:\s*[\u00b7\u2022,|\-\u2013\u2014]?\s*)?)+$/i,
    blockTags: ['DIV', 'P', 'LI', 'SECTION', 'ARTICLE', 'UL', 'OL', 'BLOCKQUOTE'],

    excludeAncestorTags: ['NAV', 'HEADER', 'FORM', 'TEXTAREA', 'INPUT'],
    excludeAncestorTestIdPattern: /inbox|conversationList|conversation_list|DMDrawer|composer|SideNav|DmInbox/i,

    // Direction signals (positive = outbound/owner, negative = inbound).
    testIdTokens: {
      outbound: ['sent', 'outgoing', 'outbound', 'self', 'own', 'mine', 'me', 'right', 'fromme'],
      inbound: ['received', 'incoming', 'inbound', 'other', 'left', 'their', 'them', 'fromthem'],
    },
    ariaPatterns: {
      outbound: [/\byou sent\b/i, /\bsent by you\b/i, /\byour message\b/i, /^you\s*[:,]/i, /\bfrom you\b/i],
      inbound: [/\breceived\b/i, /\bsent by (?!you\b)\S+/i, /\bfrom (?!you\b)@?\w+/i],
    },
    alignThreshold: 0.08,
    maxBubbleWidthFraction: 0.9,
    weights: { testId: 3, aria: 3, handle: 2, align: 2, style: 1 },
    decisionThreshold: 2,

    // "New message" rules.
    settleMs: 2500,            // after start / chat switch, everything already there is history
    batchDelayMs: 200,
    maxNewPerBatch: 3,         // a batch bigger than this = history/initial load
    dupWindowMs: 3000,
    freshMs: 120000,           // a bubble with a time[datetime] older than this is history, not new
    ackMs: 10000,              // v1.4.24: a typed relay-chat send with no host ack posts a warning
    emptyTextRetries: 3,
    emptyRetryDelayMs: 400,
    urlPollMs: 1000,
    newestAt: 'auto',
    maxTextLength: 4000,       // aligned with the host's MAX_TEXT; over this = refuse, never truncate
    debugHtmlMaxLength: 15000,
  };

  function createRelayDetector(opts) {
    const win = opts.window;
    const doc = opts.document;
    const cfg = Object.assign({}, CONFIG, opts.config || {});
    const now = opts.now || (() => Date.now());
    const emit = opts.onEmit || (() => {});
    const onReaction = opts.onReaction || (() => {});    // v1.4.2: owner thumbs-up/down on a pending prompt
    const onStatus = opts.onStatus || (() => {});
    const getState = opts.getState || (() => ({ paused: false, conversationFilter: '' }));
    // Owner-identity gate. Default: confirmed (unit tests of pure detection don't wire it);
    // the Chrome boot passes a real check (cookie / avatar). Returns true when confirmed.
    const ownerCheck = opts.ownerCheck || (() => true);
    // Cross-session dedupe of already-relayed uuids (keys only). Default: in-memory.
    const seen = opts.seen || (function () { const s = new Set(); return { has: u => s.has(u), add: u => s.add(u) }; })();
    const source = opts.source || 'page';
    const info = opts.log || ((...a) => win.console.log(cfg.logPrefix, '(' + source + ')', ...a));
    const getContextUrl = opts.getContextUrl || (() => win.location.href);
    const MO = (doc.defaultView && doc.defaultView.MutationObserver) || win.MutationObserver;

    let observer = null;
    let observedRoots = new WeakSet();
    let flushCache = null;
    let rootMemo = null;
    let shadowTimer = null;
    let scanTimer = null;
    let pending = new Set();
    let flushTimer = null;
    let urlTimer = null;
    let processed = new WeakSet();
    let retryCounts = new WeakMap();
    let seenKeys = new Set();
    let settleUntil = 0;
    let lastUrl = '';
    let mode = 'none';
    let wrongAccountReported = false;
    let accountOkReported = false;
    let recentEmits = [];
    const lastEls = {};
    const stats = { baselined: 0, emitted: 0, skippedHistory: 0, skippedDup: 0, skippedFilter: 0, skippedPaused: 0, skippedBulk: 0, skippedSettle: 0, skippedWrongAccount: 0, skippedOld: 0, refusedTooLong: 0 };

    // ---------- small helpers ----------
    function safeMatches(el, sel) { try { return !!(el && el.matches && el.matches(sel)); } catch (e) { return false; } }
    function parentOf(n) { return (n && (n.parentElement || (n.parentNode && n.parentNode.host))) || null; }
    function deepContains(a, b) { for (let n = b; n; n = parentOf(n)) if (n === a) return true; return false; }
    function walkElements(root, cb) {
      const visit = node => {
        for (let c = node.firstElementChild; c; c = c.nextElementSibling) {
          cb(c);
          if (c.shadowRoot) visit(c.shadowRoot);
          visit(c);
        }
      };
      if (root) visit(root);
    }
    const selOk = new Map();
    function validSel(sel) {
      if (!selOk.has(sel)) { try { doc.createDocumentFragment().querySelector(sel); selOk.set(sel, true); } catch (e) { selOk.set(sel, false); } }
      return selOk.get(sel);
    }
    function safeAll(root, sel) {
      if (!root || !validSel(sel)) return [];
      const out = [];
      walkElements(root, el => { if (el.matches(sel)) out.push(el); });
      return out;
    }
    function tokens(s) { return String(s || '').replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean); }
    function lc(s) { return String(s || '').toLowerCase(); }
    function mentions(s, names) { const l = lc(s); return names.some(n => l.includes(lc(n))); }
    function rect(el) { try { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width, height: r.height }; } catch (e) { return { left: 0, top: 0, width: 0, height: 0 }; } }
    function style(el) { try { return ((el.ownerDocument && el.ownerDocument.defaultView) || win).getComputedStyle(el); } catch (e) { return null; } }
    function contextPath() { try { return new URL(getContextUrl() || '', 'https://x.com').pathname; } catch (e) { return ''; } }
    function cssEscape(s) { return String(s).replace(/["\\\x00-\x1f\x7f]/g, '\\$&'); }

    function conversationId() {
      const p = contextPath();
      for (const re of cfg.conversationUrlPatterns) {
        const m = p.match(re);
        if (m && !cfg.nonConversationIds.includes(m[1])) return decodeURIComponent(m[1]);
      }
      return '';
    }
    function normalizeFilter(v) {
      v = String(v || '').trim();
      if (!v) return '';
      try { if (/^https?:/i.test(v)) v = new URL(v).pathname; } catch (e) { /* keep */ }
      for (const re of cfg.conversationUrlPatterns) { const m = v.match(re); if (m) return decodeURIComponent(m[1]); }
      return v.replace(/^\/+|\/+$/g, '');
    }

    function chatRoot() {
      if (rootMemo && rootMemo.el && rootMemo.el.isConnected && now() - rootMemo.t < 1000) return rootMemo;
      let found = null;
      for (const sel of cfg.chatRootSelectors) {
        const el = safeAll(doc, sel)[0];
        if (el) { found = { el, selector: sel }; break; }
      }
      rootMemo = Object.assign(found || { el: doc.body, selector: 'body (fallback)' }, { t: now() });
      return rootMemo;
    }

    function isExcluded(el) {
      for (let n = el; n && n.nodeType === 1; n = parentOf(n)) {
        if (cfg.excludeAncestorTags.includes(n.tagName)) return true;
        if (n.isContentEditable || n.getAttribute('contenteditable') === 'true' || n.getAttribute('role') === 'textbox') return true;
        const tid = n.getAttribute('data-testid');
        if (tid && cfg.excludeAncestorTestIdPattern.test(tid)) return true;
      }
      return false;
    }

    function uuidOf(el) {
      const m = el && el.getAttribute && (el.getAttribute('data-testid') || '').match(cfg.uuidEntryTestIdRegex);
      return m ? m[1].toLowerCase() : '';
    }
    function isUuidEntry(el) { return !!uuidOf(el); }

    // UUID-only: the page is in 'uuid' mode iff at least one message-<uuid> bubble exists.
    function detectMode(root) {
      const list = [root, ...safeAll(root, cfg.uuidEntryPrefixSelector)];
      return list.some(e => isUuidEntry(e) && !isExcluded(e)) ? 'uuid' : 'none';
    }

    function closestEntry(el) {
      for (let n = el; n && n.nodeType === 1; n = parentOf(n)) if (isUuidEntry(n)) return n;
      return null;
    }

    function collectEntries(root) {
      if (!root || root.nodeType !== 1) return [];
      const enclosing = closestEntry(root);
      if (enclosing) return isExcluded(enclosing) ? [] : [enclosing];
      let c = [root, ...safeAll(root, cfg.uuidEntryPrefixSelector)].filter(isUuidEntry);
      c = Array.from(new Set(c)).filter(e => !isExcluded(e));
      // outermost only (a message-<uuid> never nests inside another)
      c = c.filter(e => !c.some(o => o !== e && deepContains(o, e)));
      return c;
    }

    function allEntries() {
      if (flushCache) return flushCache;
      const r = chatRoot().el;
      return r ? collectEntries(r) : [];
    }

    function newestAtTop() {
      if (cfg.newestAt === 'top') return true;
      if (cfg.newestAt === 'bottom') return false;
      const r = chatRoot().el;
      const s = r && style(r);
      return !!(s && s.flexDirection === 'column-reverse');
    }

    // ---------- quote / strip ----------
    function isQuoteEl(el) {
      if (!el || el.nodeType !== 1 || el.tagName === 'BUTTON') return false;
      const tid = el.getAttribute('data-testid');
      if (tid && cfg.uuidEntryTestIdRegex.test(tid)) return false;
      if (tid && tid.indexOf(cfg.textTestIdPrefix) === 0) return false;   // the bubble's own text is not a quote
      if (tid && cfg.quoteTestIdPattern.test(tid)) return true;
      return safeMatches(el, cfg.quoteSelectors.join(','));
    }
    function findQuote(entry) {
      const sel = ['[data-testid]'].concat(cfg.quoteSelectors).join(',');
      return safeAll(entry, sel).find(isQuoteEl) || null;
    }
    function isStripEl(el) {
      if (el.tagName === 'TIME' || el.hasAttribute('datetime')) return true;
      if (isQuoteEl(el)) return true;
      const tid = el.getAttribute('data-testid');
      if (tid && tid.indexOf(cfg.textTestIdPrefix) === 0) return false;   // never strip the body itself
      if (tid && cfg.stripTestIdPattern.test(tid) && !cfg.uuidEntryTestIdRegex.test(tid)) return true;
      return false;
    }

    // Item 10: innerText-style extraction. Keeps line breaks (block boundaries + <br>), collapses
    // intra-line whitespace, strips only time/status chips, and never truncates.
    function normSpaces(t) { return String(t || '').replace(/[\u00a0\u202f\u2009\u200a\u2007]/g, ' ').replace(/[\u200b-\u200d\u2060\u200e\u200f\ufeff]/g, ''); }
    function isChipText(t) { const v = normSpaces(t).replace(/\s+/g, ' ').trim(); return !!v && cfg.chipLinePattern.test(v); }
    // v1.3.9: drop trailing lines that are only clock times / status words (any number, blank lines
    // between). Keeps at least the first line, so a message that IS just "9:07 AM" survives.
    function stripTrailingChips(text) {
      const lines = String(text || '').split('\n');
      let end = lines.length;
      while (end > 1 && (!lines[end - 1].trim() || isChipText(lines[end - 1]))) end--;
      if (end === lines.length) return text;
      // only strip if something real remains (the first kept line is not itself a chip-only body)
      const kept = lines.slice(0, end);
      if (!kept.some(l => l.trim() && !isChipText(l))) return kept.filter(l => l.trim())[0] || text;
      return kept.join('\n').replace(/\n+$/, '');
    }
    function isEmojiAlt(alt) { return !!alt && alt.length <= 16 && !/[A-Za-z0-9]/.test(alt) && /[^\s]/.test(alt); }
    function hasEmojiImg(el) { return safeAll(el, 'img[alt]').some(i => isEmojiAlt(String(i.getAttribute('alt') || ''))); }
    // v1.4.14: an element is a chip only if each child element holding text is a chip too. X wraps a bubble's
    // text and its (hidden spacer + visible) time in one span, so "2" + "6:31 PM" + "6:31 PM" read as one
    // string "26:31 PM6:31 PM" that the chip pattern accepted as times: a short number became that string.
    function isChipEl(el) {
      if (!isChipText(el.textContent)) return false;
      for (let c = el.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 1 && String(c.textContent || '').trim() && !isChipEl(c)) return false;
      }
      return true;
    }
    function extractText(textEl) {
      const parts = [];   // strings, or {chip: text} for chip-only elements (kept only if not trailing)
      function walk(node) {
        for (let c = node.firstChild; c; c = c.nextSibling) {
          if (c.nodeType === 3) { parts.push(c.nodeValue); continue; }
          if (c.nodeType !== 1) continue;
          if (c.tagName === 'BR') { parts.push('\n'); continue; }
          if (c.tagName === 'IMG') {          // v1.4.10: X draws emoji as <img alt="❤️">: they are text
            const alt = String(c.getAttribute('alt') || '');
            if (isEmojiAlt(alt)) parts.push(alt);
            continue;
          }
          if (isStripEl(c)) continue;
          const block = cfg.blockTags.includes(c.tagName);
          if (!c.shadowRoot && isChipEl(c) && !hasEmojiImg(c)) {   // v1.4.10: emoji images are text
            parts.push({ chip: (block ? '\n' : '') + c.textContent + (block ? '\n' : '') });
            continue;
          }
          if (block) parts.push('\n');
          if (c.shadowRoot) walk(c.shadowRoot);
          walk(c);
          if (block) parts.push('\n');
        }
      }
      walk(textEl);
      // A chip element counts as real text unless everything after it is whitespace/chips and some
      // real (non-chip) text comes before it.
      let lastReal = -1;
      parts.forEach((p, i) => { if (typeof p === 'string' && p.trim()) lastReal = i; });
      // A body that is ONLY chips (the owner typed just "9:07 AM", wrapped in its own span) keeps the
      // first one and drops the copies X appends after it.
      const firstChip = lastReal >= 0 ? -1 : parts.findIndex(p => typeof p !== 'string');
      const out = parts.map((p, i) => (typeof p === 'string' ? p
        : (lastReal >= 0 ? (i > lastReal ? '' : p.chip) : (i === firstChip ? p.chip : ''))));
      let text = normSpaces(out.join('')).replace(/[ \t]+/g, ' ');
      text = text.split('\n').map(l => l.trim()).join('\n').replace(/\n{3,}/g, '\n\n').replace(/^\n+|\n+$/g, '');
      return stripTrailingChips(text);
    }

    // ---------- per-entry analysis ----------
    function bodyEl(entry) {
      const u = uuidOf(entry);
      if (!u) return null;
      const sel = '[data-testid="' + cfg.textTestIdPrefix + u + '"]';
      if (safeMatches(entry, sel)) return entry;
      return safeAll(entry, sel)[0] || null;
    }
    // v1.4.21: X skips message-text-<uuid> when supersizeEmoji is set. The glyph is a ~64px span
    // (plain text, or an img whose alt is the emoji). An img is 1em, so the parent font counts.
    function emojiOnlyText(t) {
      const s = normSpaces(t).replace(/\s+/g, '');
      if (!s || s.length > 32 || /[A-Za-z0-9]/.test(s)) return '';
      try { if (!/\p{Extended_Pictographic}/u.test(s)) return ''; } catch (e) { return ''; }
      return s;
    }
    function fontPx(el) {
      const px = n => {
        const s = n && style(n);
        const v = s ? parseFloat(s.fontSize) : NaN;
        return Number.isFinite(v) ? v : 0;
      };
      const self = px(el);
      return self >= 40 ? self : px(parentOf(el));
    }
    function supersizeEmoji(entry) {
      // The reply preview is before the owner's glyph. Keep the last big emoji so a quoted
      // emoji is not read as the reply. Hidden screen-reader nodes ("You") are not the body.
      let found = null;
      walkElements(entry, el => {
        if (isHiddenEl(el)) return;
        for (let p = parentOf(el); p && p !== entry; p = parentOf(p)) if (isHiddenEl(p)) return;
        if (!(fontPx(el) >= 40)) return;
        if (el.tagName === 'IMG') {
          const alt = emojiOnlyText(el.getAttribute('alt') || '');
          if (alt) found = { el, text: alt };
          return;
        }
        const own = emojiOnlyText(ownText(el));
        if (own) found = { el, text: own };
      });
      return found;
    }
    function entryText(entry) {
      const el = bodyEl(entry);
      const text = el ? extractText(el) : '';
      if (text) return { textEl: el, text };
      const big = supersizeEmoji(entry);
      if (big) return { textEl: big.el, text: big.text };
      return { textEl: el, text: '' };
    }
    // v1.4.2: X Chat's current native Reply shows "Replied to <name>" plus a preview of the replied-to
    // message, which may carry no quote-like testid. Three more ways to find it, in order:
    //  (1) an element carrying ANOTHER message's uuid (message-<u2> / message-text-<u2>) inside the bubble,
    //      or a link/attribute naming that uuid: the replied-to message. Its text is read from the page.
    //  (2) a "Replied to ..." / "Replying to ..." label outside the bubble's own text: the preview is the
    //      label's container minus the label.
    //  (3) the same label just above the bubble (a sibling row of the entry).
    const REPLIED_RE = /^\s*(?:you\s+)?(?:replied|replying|reply)\s+to\b/i;
    const UUID_ANY_RE = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
    function ownText(el) { let t = ''; for (let n = el.firstChild; n; n = n.nextSibling) if (n.nodeType === 3) t += n.nodeValue; return t; }
    function foreignUuidQuote(entry) {
      const own = uuidOf(entry);
      const textEl = bodyEl(entry);
      let other = '';
      walkElements(entry, el => {
        if (other || (textEl && deepContains(textEl, el))) return;
        for (const at of ['data-testid', 'href', 'data-message-id', 'data-reply-to', 'aria-describedby']) {
          const v = el.getAttribute(at);
          const m = v && UUID_ANY_RE.exec(v);
          if (m && m[1].toLowerCase() !== String(own).toLowerCase()) { other = m[1]; return; }
        }
      });
      if (!other) return '';
      const src = safeAll(doc, '[data-testid="' + cfg.textTestIdPrefix + other + '"]').find(x => !deepContains(entry, x)) ||
        safeAll(entry, '[data-testid="' + cfg.textTestIdPrefix + other + '"]')[0];
      return src ? extractText(src) : '';
    }
    function repliedLabelQuote(scope, textEl) {
      let label = null;
      walkElements(scope, el => {
        if (label || (textEl && deepContains(textEl, el))) return;
        if (REPLIED_RE.test(ownText(el)) || (REPLIED_RE.test(el.textContent || '') && el.children.length === 0)) label = el;
      });
      if (!label) return '';
      // climb to the smallest container that holds more than the label (the preview), never the reply text
      let box = label;
      for (let i = 0; i < 5; i++) {
        const up = parentOf(box);
        if (!up || up === scope.parentNode || (textEl && deepContains(up, textEl))) break;
        box = up;
        if (String(box.textContent || '').trim().length > String(label.textContent || '').trim().length + 1) break;
      }
      const full = String(box.textContent || '').replace(/\s+/g, ' ').trim();
      const lab = String(label.textContent || '').replace(/\s+/g, ' ').trim();
      return full.indexOf(lab) === 0 ? full.slice(lab.length).trim() : full.replace(lab, '').trim();
    }
    // v1.4.4: X Chat's real Reply markup (read from a v1.4.3 quote_probe): inside the message row, the
    // preview is an un-labelled block (reply icon svg + the quoted sender/text) that comes BEFORE the
    // role="article" holding message-text-<uuid>. No "Replied to" text, no testid, no other message id.
    // Take the visible text of the article's preceding siblings, walking up to the row.
    function isHiddenEl(el) {
      if (!el || el.nodeType !== 1) return false;
      if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true') return true;
      const s = style(el);
      return !!(s && (s.display === 'none' || s.visibility === 'hidden'));
    }
    function visibleText(root) {
      const parts = [];
      const visit = el => {
        for (let c = el.firstChild; c; c = c.nextSibling) {
          if (c.nodeType === 3) { const v = normSpaces(c.nodeValue).trim(); if (v) parts.push(v); continue; }
          if (c.nodeType !== 1) continue;
          const tag = String(c.tagName).toLowerCase();
          if (tag === 'svg' || tag === 'time' || tag === 'script' || tag === 'style' || isHiddenEl(c)) continue;
          visit(c);
        }
      };
      if (root && !isHiddenEl(root)) visit(root);
      return parts.join(' ').replace(/\s+/g, ' ').trim();
    }
    // ---------- v1.4.6 shared post / link cards in HIS bubble ----------
    // Sharing an X post (or a link) into the chat renders a card block (a link around a thumbnail) next to
    // the typed text. It is not a reply quote: its canonical URL is what goes out as the iMessage text.
    const STATUS_URL_RE = /^(?:https?:\/\/(?:www\.|mobile\.)?(?:x|twitter)\.com)?\/([A-Za-z0-9_]{1,15})\/status(?:es)?\/(\d{5,25})(?:[/?#]|$)/i;
    function canonicalCardUrl(href) {
      const h = String(href || '').trim();
      const m = STATUS_URL_RE.exec(h);
      if (m) return 'https://x.com/' + m[1] + '/status/' + m[2];
      if (!/^https?:\/\//i.test(h)) return '';
      try {
        const u = new URL(h);
        if (/^(?:www\.|mobile\.)?(?:x|twitter)\.com$/i.test(u.hostname)) return '';   // profile / chat links are not cards
        return u.href;
      } catch (e) { return ''; }
    }
    function cardInfo(entry) {
      let body = bodyEl(entry);
      const none = { urls: [], blocks: [] };
      if (body === entry) return none;
      if (repliedLabelQuote(entry, body)) return none;              // a native Reply preview is never a card
      const typed = body ? extractText(body) : '';
      if (!typed) body = body || null;                             // a card shared with no text of his own
      const urls = [], blocks = [];
      for (const a of safeAll(entry, 'a[href]')) {
        if ((body && deepContains(body, a)) || isHiddenEl(a)) continue;   // links in his typed text stay text
        const url = canonicalCardUrl(a.getAttribute('href'));
        if (!url) continue;
        // the block: climb while the parent does not also hold his text (or is the entry itself)
        let blk = a;
        for (let up = parentOf(blk); up && up !== entry && !(body && deepContains(up, body)); up = parentOf(up)) blk = up;
        if (!blocks.includes(blk)) blocks.push(blk);
        const sm = STATUS_URL_RE.exec(url);
        if (sm ? typed.indexOf('/status/' + sm[2]) >= 0 : /https?:\/\//i.test(typed)) continue;   // he typed it already
        if (!urls.includes(url)) urls.push(url);
        if (urls.length >= 5) break;
      }
      return { urls, blocks };
    }
    function inCard(entry, el, info) {
      return (info || cardInfo(entry)).blocks.some(b => deepContains(b, el));
    }
    function withCards(text, urls) {
      const t = String(text || '').replace(/\s+$/, '');
      if (!urls.length) return t;
      return t ? t + (/:$/.test(t) || t.indexOf('\n') < 0 && t.length < 40 ? ' ' : '\n') + urls.join('\n') : urls.join('\n');
    }

    // v1.4.10: the preview block(s) of a native Reply: the article's preceding siblings at the nearest level
    // that shows text OR a photo thumbnail (a reply to an image bubble has a preview with no text at all).
    function previewInfo(entry) {
      const none = { els: [], text: '', media: false };
      const body = bodyEl(entry);
      let start = null;
      if (body && body !== entry) {
        let art = null;
        for (let n = body; n && n !== entry; n = parentOf(n)) if (n.getAttribute && n.getAttribute('role') === 'article') { art = n; break; }
        start = art || body;
      }
      // v1.4.21: a lone emoji has no message-text / article. The preview is the previous
      // sibling of that glyph (the contact's relayed line). A real message-text body wins.
      if (!(body && body !== entry && extractText(body))) {
        const big = supersizeEmoji(entry);
        if (big) start = big.el;
      }
      if (!start || start === entry) return none;
      const cards = cardInfo(entry);
      for (let n = start; n && n !== entry; n = parentOf(n)) {
        const els = [], texts = [];
        let media = false;
        for (let sib = n.previousElementSibling; sib; sib = sib.previousElementSibling) {
          if (isUuidEntry(sib) || safeAll(sib, cfg.uuidEntryPrefixSelector).some(isUuidEntry)) break;   // another message
          if (isHiddenEl(sib)) continue;
          if (cards.blocks.some(b => deepContains(b, sib) || deepContains(sib, b))) continue;   // v1.4.6: a shared card, not a quote
          const t = visibleText(sib);
          const img = safeAll(sib, 'img').some(i => MEDIA_SRC_RE.test(String(i.getAttribute('src') || '')) && !isEmojiImg(i)) ||
            safeAll(sib, '[data-testid*="image" i],[data-testid*="photo" i],[data-testid*="media" i]').length > 0;
          if ((t && !isChipText(t)) || img) { els.unshift(sib); if (t && !isChipText(t)) texts.unshift(t); media = media || img; }
        }
        if (els.length) return { els, text: texts.join(' ').trim(), media };   // the nearest level with a preview wins
      }
      return none;
    }
    function previewQuote(entry) {
      const p = previewInfo(entry);
      // a quoted photo whose preview shows no caption: mark it as a photo; with no "Name:" the host falls back
      // to its own message mapping, then asks ❓
      if (p.media && p.text.indexOf('\u{1F4F7}') < 0) return (p.text ? p.text + ' ' : '') + '\u{1F4F7} Photo';
      return p.text;
    }
    function inPreview(entry, el, info) { return (info || previewInfo(entry)).els.some(b => deepContains(b, el)); }
    function entryQuote(entry) {
      const q = findQuote(entry);
      let t = q ? extractText(q) : '';
      if (!t) t = foreignUuidQuote(entry);
      if (!t) t = repliedLabelQuote(entry, bodyEl(entry));
      const scanAbove = start => {
        let sib = start && start.previousElementSibling;
        for (let i = 0; !t && sib && i < 2; i++, sib = sib.previousElementSibling) {
          if (isUuidEntry(sib) || collectEntries(sib).length) break;     // another message, not a preview
          t = repliedLabelQuote(sib, null);
        }
      };
      if (!t) scanAbove(entry);
      const wrap = parentOf(entry);
      if (!t && wrap && wrap !== doc.body && collectEntries(wrap).length === 1) scanAbove(wrap);
      if (!t) t = previewQuote(entry);
      return String(t || '').replace(/\n+/g, ' ').trim().slice(0, cfg.maxQuoteLength);
    }
    function timeEl(entry) { return safeAll(entry, 'time[datetime],time,[datetime]')[0] || null; }
    function entryTimeIso(entry) {
      const t = timeEl(entry);
      if (!t) return '';
      return t.getAttribute('datetime') || '';
    }

    function refElement(entry) {
      for (let n = parentOf(entry); n && n !== doc.documentElement; n = parentOf(n)) {
        const s = style(n);
        if (s && /(auto|scroll)/.test(s.overflowY || '')) return { el: n, how: 'scroll-ancestor' };
      }
      const r = chatRoot();
      if (r.el && r.el !== doc.body) return { el: r.el, how: 'chat-root' };
      return { el: parentOf(entry) || entry, how: 'entry-parent' };
    }
    function pickBubble(entry, textEl, refW) {
      let best = null;
      const start = textEl || entry.firstElementChild || entry;
      for (let n = start; n; n = parentOf(n)) {
        const r = rect(n);
        if (r.width > 0 && r.width <= cfg.maxBubbleWidthFraction * refW) best = { el: n, r };
        if (n === entry) break;
      }
      return best;
    }

    function analyze(entry) {
      const conv = conversationId();
      const { textEl, text } = entryText(entry);
      const uuid = uuidOf(entry);
      const signals = ['entry:uuid:message-' + uuid];
      let score = 0;
      const W = cfg.weights;

      // 1) data-testid tokens (entry, descendants, 3 ancestors)
      const tidEls = [entry, ...safeAll(entry, '[data-testid]').slice(0, 40)];
      for (let a = parentOf(entry), i = 0; a && i < 3; a = parentOf(a), i++) tidEls.push(a);
      let tOut = null, tIn = null;
      for (const el of tidEls) {
        const v = el.getAttribute && el.getAttribute('data-testid');
        if (!v || cfg.uuidEntryTestIdRegex.test(v) || v.indexOf(cfg.textTestIdPrefix) === 0) continue;
        const tk = tokens(v);
        if (!tOut && tk.some(t => cfg.testIdTokens.outbound.includes(t))) tOut = v;
        if (!tIn && tk.some(t => cfg.testIdTokens.inbound.includes(t))) tIn = v;
      }
      if (tOut && !tIn) { score += W.testId; signals.push('testid:+' + W.testId + '(' + tOut + ')'); }
      else if (tIn && !tOut) { score -= W.testId; signals.push('testid:-' + W.testId + '(' + tIn + ')'); }
      else if (tIn && tOut) signals.push('testid:conflict');

      // 2) aria-label / title / alt
      const labelEls = [entry, ...safeAll(entry, '[aria-label],[title],img[alt],[aria-description]').slice(0, 40)];
      for (let a = parentOf(entry), i = 0; a && i < 2; a = parentOf(a), i++) labelEls.push(a);
      const labels = [];
      for (const el of labelEls) for (const at of ['aria-label', 'title', 'alt', 'aria-description']) { const v = el.getAttribute && el.getAttribute(at); if (v) labels.push(v); }
      let aOut = null, aIn = null;
      for (const l of labels) {
        if (!aOut && cfg.ariaPatterns.outbound.some(re => re.test(l))) aOut = l;
        if (!aIn && !mentions(l, cfg.ownerNames) && cfg.ariaPatterns.inbound.some(re => re.test(l))) aIn = l;
      }
      const redact = s => (text ? s.split(text).join('[text]') : s).slice(0, 80);
      if (aOut && !aIn) { score += W.aria; signals.push('aria:+' + W.aria + '(' + redact(aOut) + ')'); }
      else if (aIn && !aOut) { score -= W.aria; signals.push('aria:-' + W.aria + '(' + redact(aIn) + ')'); }
      else if (aIn && aOut) signals.push('aria:conflict');

      // 3) owner / bot handle in links, labels, test ids
      const hay = [...labels];
      safeAll(entry, 'a[href]').slice(0, 20).forEach(a => hay.push(a.getAttribute('href')));
      tidEls.slice(0, 41).forEach(el => { const v = el.getAttribute && el.getAttribute('data-testid'); if (v) hay.push(v); });
      const hOwner = hay.find(h => mentions(h, cfg.ownerNames));
      const hBot = hay.find(h => mentions(h, cfg.botNames));
      if (hBot && !hOwner) { score -= W.handle; signals.push('handle:-' + W.handle + '(' + redact(hBot) + ')'); }
      else if (hOwner && !hBot) { score += W.handle; signals.push('handle:+' + W.handle + '(' + redact(hOwner) + ')'); }
      else if (hOwner && hBot) signals.push('handle:both');

      // 4) alignment of the bubble in the scroll container
      const ref = refElement(entry);
      const rr = rect(ref.el);
      let bubble = null;
      if (rr.width > 0) {
        bubble = pickBubble(entry, textEl, rr.width);
        if (bubble) {
          const off = ((bubble.r.left + bubble.r.width / 2) - (rr.left + rr.width / 2)) / rr.width;
          const o = off.toFixed(2);
          if (off > cfg.alignThreshold) { score += W.align; signals.push('align:+' + W.align + '(right,off=' + o + ',ref=' + ref.how + ')'); }
          else if (off < -cfg.alignThreshold) { score -= W.align; signals.push('align:-' + W.align + '(left,off=' + o + ',ref=' + ref.how + ')'); }
          else signals.push('align:center(off=' + o + ')');
        } else signals.push('align:no-narrow-bubble');
      } else signals.push('align:no-layout');

      // 5) flexbox styling
      const sStart = (bubble && bubble.el) || textEl || entry;
      let sVote = 0, sWhy = '';
      for (let n = sStart, i = 0; n && i < 7 && !sVote; n = parentOf(n), i++) {
        const s = style(n);
        const p = parentOf(n);
        const ps = p ? style(p) : null;
        const isEnd = v => /^(flex-end|end|right)$/.test(v || '');
        const isStart = v => /^(flex-start|start|left)$/.test(v || '');
        const col = ps && /column/.test(ps.flexDirection || '');
        if (s && isEnd(s.alignSelf)) { sVote = 1; sWhy = 'align-self:' + s.alignSelf; }
        else if (s && isStart(s.alignSelf)) { sVote = -1; sWhy = 'align-self:' + s.alignSelf; }
        else if (n.style && n.style.marginLeft === 'auto' && n.style.marginRight !== 'auto') { sVote = 1; sWhy = 'margin-left:auto'; }
        else if (n.style && n.style.marginRight === 'auto' && n.style.marginLeft !== 'auto') { sVote = -1; sWhy = 'margin-right:auto'; }
        else if (ps && ps.display && /flex/.test(ps.display)) {
          const axis = col ? ps.alignItems : ps.justifyContent;
          if (isEnd(axis)) { sVote = 1; sWhy = (col ? 'align-items:' : 'justify-content:') + axis; }
          else if (isStart(axis)) { sVote = -1; sWhy = (col ? 'align-items:' : 'justify-content:') + axis; }
        }
        if (n === parentOf(entry)) break;
      }
      if (sVote) { score += sVote * W.style; signals.push('style:' + (sVote > 0 ? '+' : '-') + W.style + '(' + sWhy + ')'); }

      // v1.4.21: the message row's own justify-end / justify-start. The style walk reads the
      // parent, so it misses this, and a background tab has no rects for the align vote. Weight
      // it like a testid so an own jumbo-emoji row is still outbound. The class is what X sets;
      // computed style covers an inline style with no class. data-send-status is "sent" on
      // received rows too and is not a direction signal.
      let rowEnd = false, rowStart = false;
      {
        const rs = style(entry);
        const jc = (rs && rs.justifyContent) || '';
        const endish = v => /^(flex-end|end|right)$/.test(v || '');
        const startish = v => /^(flex-start|start|left)$/.test(v || '');
        const cls = entry.classList;
        const clsEnd = !!(cls && cls.contains('justify-end'));
        const clsStart = !!(cls && cls.contains('justify-start'));
        if (clsEnd !== clsStart) { rowEnd = clsEnd; rowStart = clsStart; }
        else if (!clsEnd) { rowEnd = endish(jc); rowStart = startish(jc); }
        if (rowEnd && !rowStart) { score += W.testId; signals.push('row:+' + W.testId + '(justify-end)'); }
        else if (rowStart && !rowEnd) { score -= W.testId; signals.push('row:-' + W.testId + '(justify-start)'); }
      }

      // v1.4.24: X's screen-reader label on the owner's row is a hidden span whose whole text is
      // "You sent". A text reply often has no justify-end class, and a background tab has no rects,
      // so the score stayed 0 and the service worker dropped it with no notice. A bare "You" is not
      // enough. A justify-start row stays inbound.
      let youSent = false;
      if (!rowStart) {
        walkElements(entry, el => {
          if (youSent || !isHiddenEl(el)) return;
          const t = normSpaces(el.textContent || '').replace(/\s+/g, ' ').trim();
          if (/^you sent$/i.test(t)) youSent = true;
        });
      }
      if (youSent) {
        score += W.testId;
        if (score < W.testId) score = W.testId;
        signals.push('sr:+' + W.testId + '(you sent)');
      }

      const quotedText = entryQuote(entry);
      // A native-reply preview on a row that is not clearly someone else's (justify-start) is the
      // owner's reply. Hidden screen-reader text stays out of quotedText (visibleText skips it).
      if (quotedText && !rowStart && score > -cfg.decisionThreshold && score < W.testId) {
        score = W.testId;
        signals.push('preview:+' + W.testId);
      }

      const direction = score >= cfg.decisionThreshold ? 'outbound' : score <= -cfg.decisionThreshold ? 'inbound' : 'unknown';
      signals.push('score:' + score);
      const key = conv + '|id:uuid=' + uuid;
      if (quotedText) signals.push('quote:' + quotedText.length);
      return { conv, uuid, text, quotedText, timeIso: entryTimeIso(entry), key, direction, score, signals, textEl, bubbleEl: bubble && bubble.el, ref, youSent };
    }

    // ---------- baseline / flush ----------
    function observeShadowRoots(root) {
      if (!observer) return;
      const add = el => { if (el.shadowRoot && !observedRoots.has(el.shadowRoot)) { observedRoots.add(el.shadowRoot); observer.observe(el.shadowRoot, OBS_OPTS); } };
      if (root && root.nodeType === 1) add(root);
      walkElements(root, add);
    }

    const CATCHUP_MS = 45000;   // v1.4.26: a self-send this fresh is still caught after the script attaches
    function freshStamp(iso) {
      const parsed = Date.parse(iso || '');
      return !!iso && !isNaN(parsed) && parsed <= now() + 5000 && now() - parsed <= CATCHUP_MS;
    }
    function holdsSelfSend(a) {
      // Only a relay-chat self-send with a fresh clock. Startup paint and history stay history.
      if (!a || !relayChatHere() || seen.has(a.uuid)) return false;
      if (UI_TAG_RE.test(a.text || '')) return false;
      const self = a.direction === 'outbound' || (a.direction === 'unknown' && (a.quotedText || a.youSent));
      return !!(self && freshStamp(a.timeIso));
    }
    function baseline(reason) {
      rootMemo = null;
      mode = detectMode(chatRoot().el || doc.body);
      const list = allEntries();
      const keep = new Set();
      const newest = list.length ? (newestAtTop() ? list[0] : list[list.length - 1]) : null;
      if (newest) {
        let a = null;
        try { a = analyze(newest); } catch (err) { a = null; }
        if (holdsSelfSend(a)) keep.add(newest);
      }
      for (const e of list) {
        if (keep.has(e)) continue;
        processed.add(e);
        seenKeys.add(conversationId() + '|id:uuid=' + uuidOf(e));
      }
      stats.baselined += list.length - keep.size;
      try { scanReactions(list); } catch (e) { /* prompts get marked on their next change */ }
      settleUntil = now() + cfg.settleMs;
      recentEmits = [];
      if (keep.size) { keep.forEach(e => pending.add(e)); schedule(0); }
      info('baseline (' + reason + '): context=' + contextPath() + ' conversation=' + (conversationId() || '(none)') + ' mode=' + mode + ' root=' + chatRoot().selector + ' existing=' + list.length);
    }

    function schedule(delay) {
      if (flushTimer) return;
      flushTimer = win.setTimeout(() => { flushTimer = null; flush(); }, delay == null ? cfg.batchDelayMs : delay);
    }

    function onMutations(muts) {
      for (const m of muts) {
        if (m.type === 'characterData') { const pe = parentOf(m.target); if (pe) pending.add(pe); continue; }
        if (m.type === 'attributes') { pending.add(m.target); continue; }
        m.addedNodes.forEach(n => {
          if (n.nodeType === 1) pending.add(n);
          else if (n.nodeType === 3 && parentOf(n)) pending.add(parentOf(n));
        });
      }
      if (pending.size) schedule();
    }

    // ---------- v1.4.2: a thumbs-up / thumbs-down on a pending "Reply yes" prompt ----------
    // The bot's prompts are inbound bubbles whose text says "reply yes within 2 min". A reaction is an
    // emoji drawn on that bubble OUTSIDE its message text. Only a reaction that APPEARS after the prompt
    // was first seen counts (never one already there at page load), only on the newest prompt, only
    // within 2 minutes of it, only once, and only with the owner's identity confirmed. The extension
    // reads the prompt's own text only to recognise it (prompts carry no message bodies).
    const PROMPT_RE = /reply yes within 2 min/i;
    const UP_RE = /\u{1F44D}/u, DOWN_RE = /\u{1F44E}/u, TRASH_RE = /\u{1F5D1}/u;   // v1.4.5: wastebasket
    const PICT_RE = /\p{Extended_Pictographic}/gu;
    const PROMPT_WINDOW_MS = 120000;
    const promptMarks = new Map();   // uuid -> { at, up, down, fired }
    function reactionState(entry) {
      const textEl = bodyEl(entry);
      const quoteEl = findQuote(entry);
      const kinds = new Set();
      let up = false, down = false, trash = false;
      const look = s => {
        if (!s) return;
        if (UP_RE.test(s)) up = true;
        if (DOWN_RE.test(s)) down = true;
        if (TRASH_RE.test(s)) trash = true;
        const m = s.match(PICT_RE); if (m) m.forEach(k => kinds.add(k));
      };
      walkElements(entry, el => {
        if ((textEl && deepContains(textEl, el)) || (quoteEl && deepContains(quoteEl, el))) return;
        if (el.closest && el.closest('[role="menu"],[role="menuitem"],[role="dialog"],[role="toolbar"],[role="tooltip"],[role="listbox"]')) return;
        for (let n = el.firstChild; n; n = n.nextSibling) if (n.nodeType === 3) look(n.nodeValue);
        look(el.getAttribute('alt')); look(el.getAttribute('aria-label'));
      });
      if (kinds.size >= 4) return { up: false, down: false, trash: false };    // an emoji picker / hover bar, not a reaction
      return { up, down, trash };
    }
    function isPrompt(entry) {
      if (isUiPrompt(uuidOf(entry))) return true;          // v1.4.6: the relay's ❓ on the owner's own message
      const tEl = bodyEl(entry);
      if (!tEl) return false;
      const t = String(tEl.textContent || '');
      return PROMPT_RE.test(t) && t.length < 600;
    }
    // v1.4.5: a wastebasket reaction the owner adds to a RELAYED message (an inbound bubble that is not
    // a prompt) asks to block that sender. Like the prompt reactions: only one that appears after the
    // bubble was first seen (never one already there at load), once per bubble, owner identity confirmed.
    // The bubble's own text goes to the Mac in memory only (the Mac reads its "Label:" to find the
    // sender, exactly like a quote); it is never logged or stored.
    const trashMarks = new Map();   // uuid -> { trash, fired }
    function scanTrash(e, u, conv) {
      const st = reactionState(e);
      const mk = trashMarks.get(u);
      if (!mk) {
        if (trashMarks.size > 2000) trashMarks.clear();
        trashMarks.set(u, { trash: st.trash, fired: false });
        return;
      }
      if (mk.fired || !st.trash || mk.trash) { mk.trash = mk.trash || st.trash; return; }
      mk.trash = true;
      if (!ownerCheck()) return;
      const a = analyze(e);
      if (a.direction !== 'inbound' || !a.text) return;   // only a relayed (inbound) message, never mine or unclear
      mk.fired = true;
      try {
        onReaction({ conversationId: conv, key: 'trash:' + u, word: 'block', quotedText: a.text.slice(0, 500),
                     ts: new Date(now()).toISOString() });
      } catch (err) { info('reaction emit failed', err); }
    }
    function scanReactions(roots) {
      const conv = conversationId();
      const state = getState() || {};
      const filter = normalizeFilter(state.conversationFilter);
      if (!conv || state.paused || (filter && filter !== conv)) return;
      const entries = new Set();
      for (const r of roots) {
        if (!r || !r.isConnected) continue;
        const e = closestEntry(r); if (e) entries.add(e);
        for (const x of collectEntries(r)) entries.add(x);
      }
      for (const e of entries) {
        const u = uuidOf(e);
        if (u && !isPrompt(e)) { scanTrash(e, u, conv); continue; }
        if (!u) continue;
        if (!ownerCheck()) return;
        const st = reactionState(e);
        const mk = promptMarks.get(u);
        if (!mk) {
          const iso = entryTimeIso(e);
          const t = iso ? (Date.parse(iso) || now()) : now();
          promptMarks.set(u, { at: Math.min(t, now()), up: st.up, down: st.down, fired: false });
          continue;
        }
        if (mk.fired) continue;
        const word = (st.up && !mk.up) ? 'yes' : (st.down && !mk.down) ? 'no' : '';
        mk.up = mk.up || st.up; mk.down = mk.down || st.down;
        if (!word) continue;
        if (now() - mk.at > PROMPT_WINDOW_MS) continue;
        const newer = Array.from(promptMarks.values()).some(o => o !== mk && o.at > mk.at);
        if (newer) continue;
        if (analyze(e).direction === 'outbound' && !isUiPrompt(u) && !UI_TAG_RE.test(entryText(e).text)) continue;   // the owner's own message is never a prompt (except a relay ❓ / 🤖 prompt)
        mk.fired = true;
        try { onReaction({ conversationId: conv, key: 'reaction:' + u, word, ts: new Date(now()).toISOString() }); } catch (err) { info('reaction emit failed', err); }
      }
    }

    // v1.4.24: a quoted body with no "Name:" prefix. The sender is the nearest preceding
    // "💬 name · time" header, and only when every match names that one person.
    const HEADER_LINE_RE = /^\s*\u{1F4AC}\s*(.+?)\s*\u00b7\s*\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?\s*$/u;
    function normBody(s) {
      return normSpaces(s).replace(/\s+/g, ' ').trim().toLowerCase().replace(/(?:\u2026|\.\.\.)\s*$/, '').replace(/\s+$/, '');
    }
    function headerLabelOf(entry) {
      const m = HEADER_LINE_RE.exec(normSpaces(entryText(entry).text).replace(/\s+/g, ' ').trim());
      if (!m) return '';
      return m[1].replace(/[\r\n]/g, '').trim().slice(0, 80);
    }
    function headerBefore(entries, index) {
      for (let i = index - 1; i >= 0; i--) {
        const lab = headerLabelOf(entries[i]);
        if (lab) return lab;
      }
      return '';
    }
    function referencedUuid(entry) {
      const own = String(uuidOf(entry) || '').toLowerCase();
      const textEl = bodyEl(entry);
      let other = '';
      const consider = el => {
        if (!el || other || (textEl && el !== textEl && deepContains(textEl, el))) return;
        for (const at of ['data-testid', 'href', 'data-message-id', 'data-reply-to', 'aria-describedby']) {
          const v = el.getAttribute && el.getAttribute(at);
          const m = v && UUID_ANY_RE.exec(v);
          if (m && m[1].toLowerCase() !== own) { other = m[1].toLowerCase(); return; }
        }
      };
      consider(entry);   // X puts the replied-to id on the message row itself; walkElements starts at the children
      walkElements(entry, consider);
      return other;
    }
    function quoteMatchKeys(quoted) {
      const n = normBody(quoted);
      const keys = [];
      if (n.length >= 4) keys.push(n);
      const sp = n.indexOf(' ');
      if (sp > 0 && sp <= 40 && n.length - sp - 1 >= 4) keys.push(n.slice(sp + 1));
      return keys;
    }
    function matchingLabels(entries, idx, keys, exact) {
      const labels = [];
      for (let i = idx - 1; i >= 0; i--) {
        let a;
        try { a = analyze(entries[i]); } catch (err) { continue; }
        if (!a || a.direction === 'outbound') continue;
        const body = normBody(a.text);
        if (body.length < 4 || headerLabelOf(entries[i])) continue;
        const hit = exact ? keys.some(k => body === k) : keys.some(k => k.length >= 12 && body.indexOf(k) === 0);
        if (!hit) continue;
        labels.push(headerBefore(entries, i));
      }
      return labels;
    }
    function oneLabel(labels) {
      const uniq = [];
      labels.forEach(l => { if (uniq.indexOf(l) < 0) uniq.push(l); });
      if (uniq.length === 1 && uniq[0]) return uniq[0];
      return '';
    }
    function quoteLabelFrom(entry, quoted) {
      const keys = quoteMatchKeys(quoted);
      const all = allEntries();
      const idx = all.indexOf(entry);
      if (idx < 0) return '';
      const ref = referencedUuid(entry);
      if (ref) {
        const j = all.findIndex(e => e !== entry && String(uuidOf(e) || '').toLowerCase() === ref);
        if (j < 0) return '';
        return headerBefore(all, j);
      }
      if (!keys.length) return '';
      const exactLabels = matchingLabels(all, idx, keys, true);
      const exact = oneLabel(exactLabels);
      if (exact) return exact;
      if (exactLabels.length) return '';   // matches disagreed, or none had a header
      const longs = keys.filter(k => k.length >= 12);
      if (!longs.length) return '';
      return oneLabel(matchingLabels(all, idx, longs, false));
    }

    // v1.4.24: every typed send in the relay chat must get a host ack. Otherwise the background
    // posts one fixed warning. History and other chats never arm this.
    let ackTimer = null, ackGen = 0;
    function relayChatHere() {
      const st = getState() || {};
      const conv = conversationId();
      const filter = normalizeFilter(st.conversationFilter);
      return !!(conv && filter && filter === conv);
    }
    function armUnacked() {
      if (!relayChatHere()) return;
      const gen = ++ackGen;
      if (ackTimer) win.clearTimeout(ackTimer);
      const ms = Number(cfg.ackMs) > 0 ? Number(cfg.ackMs) : 10000;
      ackTimer = win.setTimeout(() => {
        if (gen !== ackGen) return;
        ackTimer = null;
        try { onStatus({ status: 'unacked', conversationId: conversationId(), id: String(gen) }); } catch (e) { /* ignore */ }
      }, ms);
    }
    function noteHostAck(hold) {
      if (hold) { armUnacked(); return; }
      ackGen++;
      if (ackTimer) { win.clearTimeout(ackTimer); ackTimer = null; }
    }

    function flush() { try { flushInner(); } finally { flushCache = null; } }
    function flushInner() {
      const roots = Array.from(pending);
      pending = new Set();
      for (const r of roots) if (r.isConnected) observeShadowRoots(r);
      const root = chatRoot();
      const prevMode = mode;
      if (mode === 'none') mode = detectMode(root.el || doc.body);
      if (prevMode === 'none' && mode !== 'none') { baseline('first messages rendered'); return; }
      try { scanReactions(roots); } catch (e) { info('reaction scan failed', e); }
      const found = new Set();
      for (const r of roots) {
        if (!r.isConnected) continue;
        if (root.el !== doc.body && !deepContains(root.el, r) && !deepContains(r, root.el)) continue;
        for (const e of collectEntries(r)) if (!processed.has(e)) found.add(e);
      }
      if (!found.size) return;

      const state = getState() || {};
      const conv = conversationId();
      const filter = normalizeFilter(state.conversationFilter);

      // Owner-identity gate (items 1 & 6): no positive owner identity => read NO text, emit nothing,
      // surface a sticky wrong_account. We return before analyze() so no message body is touched.
      const ownerOk = !!ownerCheck();
      if (conv && !ownerOk) { stats.skippedWrongAccount += found.size; reportWrongAccount(); return; }
      if (conv && ownerOk) reportAccountOk();
      let baselineReason = '';
      if (!conv) baselineReason = 'not-a-chat-page';
      else if (state.paused) { baselineReason = 'paused'; stats.skippedPaused += found.size; }
      else if (now() < settleUntil) { baselineReason = 'settling'; stats.skippedSettle += found.size; }
      else if (filter && filter !== conv) { baselineReason = 'filter'; stats.skippedFilter += found.size; }
      else if (found.size > cfg.maxNewPerBatch) { baselineReason = 'bulk(' + found.size + ')'; stats.skippedBulk += found.size; }

      const all = allEntries();
      flushCache = all;
      const ordered = Array.from(found).sort((a, b) => all.indexOf(a) - all.indexOf(b));
      const topNewest = newestAtTop();
      for (const e of ordered) {
        const a = analyze(e);
        let cardUrls = [];
        const rawText = a.text;
        if (a.direction === 'outbound') {
          // v1.4.6: a shared X post / link card goes out as its URL, after anything he typed ("Jordan:" + post)
          try { cardUrls = cardInfo(e).urls; } catch (err) { cardUrls = []; }
          if (cardUrls.length) a.text = withCards(a.text, cardUrls);
        }
        const media = a.direction === 'outbound' ? mediaOf(e) : [];
        if (!a.text && !media.length || (a.direction === 'outbound' && mediaShell(e))) {
          const n = retryCounts.get(e) || 0;
          if (n < cfg.emptyTextRetries && !baselineReason) {
            retryCounts.set(e, n + 1);
            win.setTimeout(() => { pending.add(e); schedule(0); }, cfg.emptyRetryDelayMs);
            continue;
          }
        }
        // v1.4.26: a fresh self-send during startup settle used to be marked done and dropped.
        // Hold that one until settle ends and arm the watchdog. Older bubbles stay history.
        if (baselineReason === 'settling' && holdsSelfSend(a)) {
          if (a.direction === 'outbound') armUnacked();
          const wait = Math.max(0, settleUntil - now());
          win.setTimeout(() => { pending.add(e); schedule(0); }, Math.max(cfg.batchDelayMs, wait));
          continue;
        }
        processed.add(e);
        if (seenKeys.has(a.key)) { stats.skippedDup++; continue; }
        seenKeys.add(a.key);
        if (baselineReason) continue;
        if (UI_TAG_RE.test(a.text)) {                 // v1.4.6: a prompt the relay itself typed: never input
          seen.add(a.uuid); stats.skippedUi = (stats.skippedUi || 0) + 1; continue;
        }

        // Freshness (item 1): a bubble whose time[datetime] is older than freshMs is history.
        let ts = new Date(now()).toISOString();
        if (a.timeIso) {
          const parsed = Date.parse(a.timeIso);
          if (!isNaN(parsed)) {
            if (now() - parsed > cfg.freshMs) { stats.skippedOld++; continue; }
            ts = new Date(parsed).toISOString();
          }
        }

        // Must be at the newest end.
        const idx = all.indexOf(e);
        if (idx >= 0) {
          const after = topNewest ? all.slice(0, idx) : all.slice(idx + 1);
          const blocked = after.some(o => !found.has(o) && entryText(o).text);
          if (blocked) { stats.skippedHistory++; continue; }
        } else a.signals.push('position:unknown');

        // Cross-session dedupe on the uuid (keys only; persisted by the boot layer).
        if (seen.has(a.uuid)) { stats.skippedDup++; continue; }

        const t = now();
        recentEmits = recentEmits.filter(r => t - r.t < cfg.dupWindowMs);
        const dupText = a.text + (media.length ? '\u0000' + media.join('|') : '');
        if (recentEmits.some(r => r.text === dupText && r.direction === a.direction)) { stats.skippedDup++; continue; }
        recentEmits.push({ text: dupText, direction: a.direction, t });

        lastEls[a.direction] = e;
        lastEls.any = e;

        // Item 10: refuse an over-limit reply (owner notice) instead of truncating.
        if (a.text.length > cfg.maxTextLength) {
          stats.refusedTooLong++;
          seen.add(a.uuid);
          const ref = { ts, conversationId: conv, direction: a.direction, textLength: a.text.length, key: a.key, source, refuse: 'too-long', limit: cfg.maxTextLength };
          try { emit(ref); } catch (err) { info('emit failed', err); }
          continue;
        }

        stats.emitted++;
        seen.add(a.uuid);
        const typedBad = a.direction === 'outbound' ? typedMismatch(rawText) : false;
        const item = { ts, conversationId: conv, direction: a.direction, text: a.text, signals: a.signals, score: a.score, key: a.key, source };
        if (a.quotedText) item.quotedText = a.quotedText;
        if (a.youSent) item.youSent = true;
        if (a.direction === 'outbound' && a.quotedText) {
          try {
            const lab = quoteLabelFrom(e, a.quotedText);
            if (lab) item.quoteLabel = lab;
          } catch (err) { /* a missed label asks "Who is this for?" rather than guessing */ }
        }
        if (a.direction === 'outbound' && !UI_TAG_RE.test(a.text || '')) armUnacked();
        if (typedBad) item.typedMismatch = true;      // v1.4.14: what we read is not what he typed: never sent
        if (a.direction !== 'inbound') { try { item.quoteProbe = structureOf(e); } catch (err) { /* diagnostics only */ } }
        if (cardUrls.length) item.cards = cardUrls.length;
        if (media.length) {             // v1.4.6: his photos ride along (bytes read only when the send goes ahead)
          pruneMedia();
          mediaRefs.set(String(a.uuid).toLowerCase(), { srcs: media, at: realNow() });
          item.mediaCount = media.length;
        }
        try { emit(item); } catch (err) { info('emit failed', err); }
      }
    }

    // v1.4.14: what he typed, held in memory only (never stored, never logged) for up to 30 s, so a bubble
    // read back differently (X markup change) is refused instead of sent. Our own 🤖 prompts are skipped.
    let typed = [];
    const typedNorm = v => normSpaces(v).replace(/[\s\uFE0E\uFE0F\u200d]+/g, '');
    function captureTyped() {
      try {
        const box = safeAll(doc, '[data-testid="dm-composer-textarea"]')[0];
        const v = box ? ((box.tagName === 'TEXTAREA' || box.tagName === 'INPUT') ? String(box.value || '') : String(box.textContent || '')) : '';
        if (!v.trim() || UI_TAG_RE.test(v)) return;
        const t = realNow();
        typed = typed.filter(x => t - x.at < 30000).slice(-4);
        typed.push({ n: typedNorm(v), at: t });
        armUnacked();   // v1.4.24: even if this bubble is never emitted, the owner hears why
      } catch (e) { /* never block his send */ }
    }
    function typedMismatch(text) {
      const t = realNow();
      typed = typed.filter(x => t - x.at < 30000);
      if (!typed.length) return false;               // nothing captured (other device, older page): as before
      const n = typedNorm(text);
      const i = typed.findIndex(x => x.n === n);
      if (i >= 0) { typed.splice(i, 1); return false; }
      typed.shift();
      return true;
    }
    function onTypedKey(ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && ev.target && ev.target.closest &&
          ev.target.closest('[data-testid="dm-composer-textarea"]')) captureTyped();
    }
    function onTypedClick(ev) {
      if (ev.isTrusted !== false && ev.target && ev.target.closest && ev.target.closest('[data-testid="dm-composer-send-button"]')) captureTyped();
    }

    function reportWrongAccount() {
      accountOkReported = false;
      if (wrongAccountReported) return;
      wrongAccountReported = true;
      try { onStatus({ status: 'wrong_account', conversationId: conversationId() }); } catch (e) { /* ignore */ }
      info('owner identity not confirmed on this page: relay is off (wrong_account). No message text read.');
    }

    // A positive owner check clears the sticky wrong_account flag (sent once until it flips back).
    function reportAccountOk() {
      wrongAccountReported = false;
      if (accountOkReported) return;
      accountOkReported = true;
      try { onStatus({ status: 'account_ok', conversationId: conversationId() }); } catch (e) { /* ignore */ }
    }

    function checkUrl() {
      const href = getContextUrl();
      if (href !== lastUrl) {
        const first = !lastUrl;
        lastUrl = href;
        wrongAccountReported = false;
        accountOkReported = false;
        if (!first) baseline('url change');
      }
    }

    // ---------- debug snapshot (text replaced by placeholder) ----------
    function sanitize(el, text) {
      const clone = el.cloneNode(true);
      const walker = doc.createTreeWalker(clone, 4);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(n => { if (n.nodeValue.trim()) n.nodeValue = '[text]'; });
      const all = [clone, ...clone.querySelectorAll('*')];
      for (const n of all) {
        for (const at of ['aria-label', 'title', 'alt', 'aria-description', 'placeholder', 'data-text']) {
          const v = n.getAttribute(at);
          if (v && text) n.setAttribute(at, v.split(text).join('[text]'));
        }
        if (n.hasAttribute('src')) n.setAttribute('src', '[src]');
        if (n.hasAttribute('srcset')) n.setAttribute('srcset', '[srcset]');
      }
      let html = clone.outerHTML;
      const shadowHosts = [];
      walkElements(el, n => { if (n.shadowRoot) shadowHosts.push(n.tagName); });
      if (shadowHosts.length) html += '\n<!-- open shadow roots not serialized, hosts: ' + shadowHosts.slice(0, 10).join(',') + ' -->';
      if (html.length > cfg.debugHtmlMaxLength) html = html.slice(0, cfg.debugHtmlMaxLength) + '…[truncated]';
      return html;
    }
    function describe(el, text) {
      const s = style(el) || {};
      const lab = el.getAttribute('aria-label');
      return {
        tag: el.tagName, testid: el.getAttribute('data-testid'), role: el.getAttribute('role'),
        ariaLabel: lab ? (text ? lab.split(text).join('[text]') : lab).slice(0, 120) : null,
        display: s.display, flexDirection: s.flexDirection, justifyContent: s.justifyContent,
        alignItems: s.alignItems, alignSelf: s.alignSelf, overflowY: s.overflowY, rect: rect(el),
      };
    }
    function snap(el) {
      const a = analyze(el);
      const chain = [];
      for (let n = parentOf(el), i = 0; n && i < 8 && n !== doc.body; n = parentOf(n), i++) chain.push(describe(n, a.text));
      return {
        direction: a.direction, score: a.score, signals: a.signals, uuid: a.uuid,
        textLength: a.text.length, hasTime: !!a.timeIso,
        entry: describe(el, a.text),
        bubble: a.bubbleEl ? describe(a.bubbleEl, a.text) : null,
        reference: Object.assign({ how: a.ref.how }, describe(a.ref.el, a.text)),
        ancestors: chain,
        outerHTML: sanitize(el, a.text),
      };
    }
    // ---------- v1.4.3: text-free quote diagnostics ----------
    // A structural fingerprint of a bubble and its ~3 preceding siblings (and those of up to 2 wrappers
    // that hold only this bubble): tag, role, a sanitized data-testid, attribute NAMES (never values),
    // and yes/no flags: RA = an aria-label contains "Replied to", RT = the element's own text contains
    // "Replied to", REF = an attribute references ANOTHER message uuid, t = has its own text. No text,
    // no labels, no handles, no uuids.
    const UUID_G = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
    const PROBE_REPLIED_RE = /replied to/i;
    function cleanTid(tid, ownUuid) {
      let t = String(tid || '').replace(UUID_G, m => (ownUuid && m.toLowerCase() === ownUuid ? '<self>' : '<uuid>'));
      const names = cfg.ownerNames.concat(cfg.botNames).map(lc).filter(Boolean);
      t = t.split(/([-_])/).map((seg, i, arr) => {
        if (seg === '-' || seg === '_' || /^<(self|uuid)>$/.test(seg)) return seg;
        if (/[0-9]/.test(seg) || names.includes(lc(seg).replace(/^@/, ''))) return '<x>';
        if (i >= 2 && /container$/i.test(arr[i - 2] || '')) return '<x>';   // UserAvatar-Container-<handle>
        return seg.replace(/[^A-Za-z]/g, '').slice(0, 40);
      }).join('');
      return t.slice(0, 80);
    }
    function nodeSig(el, depth, ownUuid) {
      let sig = depth + '.' + String(el.tagName || '?').toLowerCase().replace(/[^a-z0-9-]/g, '').slice(0, 30);
      const role = el.getAttribute('role');
      if (role) sig += '#' + role.replace(/[^A-Za-z-]/g, '').slice(0, 30);
      const tid = el.getAttribute('data-testid');
      if (tid) sig += '@' + cleanTid(tid, ownUuid);
      const names = [];
      let ra = false, ref = false;
      for (const a of Array.from(el.attributes || [])) {
        const n = a.name;
        if (n === 'class' || n === 'style') continue;
        if (n !== 'data-testid' && n !== 'role' && names.length < 12) names.push(n.replace(/[^a-z0-9-]/gi, '').slice(0, 30));
        const v = String(a.value || '');
        if (n === 'aria-label' && PROBE_REPLIED_RE.test(v)) ra = true;
        const ids = v.match(UUID_G);
        if (ids && ids.some(u => u.toLowerCase() !== ownUuid)) ref = true;
      }
      if (names.length) sig += '{' + names.join(',') + '}';
      const ot = ownText(el);
      if (ra) sig += '!RA';
      if (PROBE_REPLIED_RE.test(ot)) sig += '!RT';
      if (ref) sig += '!REF';
      if (ot.trim()) sig += '+t';
      return sig;
    }
    function treeSig(root, ownUuid, maxNodes) {
      const out = [];
      const visit = (el, depth) => {
        if (out.length >= maxNodes || depth > 9) return;
        out.push(nodeSig(el, depth, ownUuid));
        for (let c = el.firstElementChild; c && out.length < maxNodes; c = c.nextElementSibling) visit(c, depth + 1);
      };
      visit(root, 0);
      if (out.length >= maxNodes) out.push('...');
      return out.join(' ');
    }
    function structureOf(entry) {
      const own = uuidOf(entry);
      const parts = ['E=' + treeSig(entry, own, 40)];
      const sibs = (start, tag) => {
        let sib = start && start.previousElementSibling;
        for (let i = 1; sib && i <= 3; i++, sib = sib.previousElementSibling) {
          parts.push(tag + '-' + i + (isUuidEntry(sib) ? '(msg)' : '') + '=' + treeSig(sib, own, 25));
        }
      };
      sibs(entry, 'S');
      let up = parentOf(entry);
      for (let lvl = 1; lvl <= 2 && up && up !== doc.body && collectEntries(up).length === 1; lvl++, up = parentOf(up)) {
        parts.push('P' + lvl + '=' + nodeSig(up, 0, own));
        sibs(up, 'P' + lvl + 'S');
      }
      return parts.join(' | ').slice(0, 3800);
    }
    function pageStructure() {
      const all = allEntries().slice(-6);
      return {
        v: 1, generatedAt: new Date(now()).toISOString(), mode, chatRoot: chatRoot().selector,
        entriesOnPage: allEntries().length, newestAtTop: newestAtTop(),
        entries: all.map(e => { const a = analyze(e); return { direction: a.direction, quoteSeen: !!a.quotedText, structure: structureOf(e) }; }),
      };
    }

    function debugSnapshot() {
      const all = allEntries();
      const primary = (lastEls.any && lastEls.any.isConnected) ? lastEls.any : all[all.length - 1];
      const other = primary && ['outbound', 'inbound'].map(d => lastEls[d]).find(e => e && e !== primary && e.isConnected);
      return {
        generatedAt: new Date(now()).toISOString(),
        source, frameHref: (doc.location && doc.location.href) || '', contextPath: contextPath(),
        frameScan: frameScan(), ownerConfirmed: !!ownerCheck(),
        conversationId: conversationId(),
        mode, chatRoot: chatRoot().selector, entriesOnPage: all.length, newestAtTop: newestAtTop(),
        state: getState(), stats: Object.assign({}, stats),
        primary: primary ? snap(primary) : null,
        otherDirection: other ? snap(other) : null,
        note: primary ? undefined : 'No message-<uuid> entries matched. Check uuidEntryTestIdRegex in CONFIG.',
      };
    }

    function frameScan() {
      const testids = new Set(), roles = new Set();
      let hasShadow = false, iframes = 0;
      walkElements(doc.documentElement ? doc : null, el => {
        const t = el.getAttribute('data-testid'); if (t && testids.size < 60) testids.add(t);
        const r = el.getAttribute('role'); if (r) roles.add(r);
        if (el.shadowRoot) hasShadow = true;
        if (el.tagName === 'IFRAME') iframes++;
      });
      return { href: (doc.location && doc.location.href) || '', contextPath: contextPath(), testids: Array.from(testids), roles: Array.from(roles), hasShadow, iframes, mode, entriesMatched: allEntries().length };
    }
    function logFrameScan() {
      const f = frameScan();
      info('frame scan: href=' + f.href + ', context=' + f.contextPath + ', testids=' + JSON.stringify(f.testids) +
        ', roles=' + JSON.stringify(f.roles) + ', hasShadow=' + f.hasShadow + ', iframes=' + f.iframes + ', mode=' + f.mode + ', entries=' + f.entriesMatched);
    }

    // ---------- v1.4.6: fast feedback, done in the owner's OWN tab as the owner ----------
    // The extension now acts in X Chat's web UI, but only in these narrow ways, all requested by the Mac
    // helper and all on the relay chat: (1) put ONE emoji reaction on ONE of the owner's own messages
    // (found by its message-<uuid>), (2) type a relay prompt that starts with UI_TAG into an EMPTY
    // composer and press send. Anything it typed, and every reaction it placed, is ignored as input.
    const UI_TAG = '\u{1F916}';
    const UI_TAG_RE = /^\s*\u{1F916}/u;
    const realNow = () => Date.now();
    const uiPromptUuids = new Map();         // uuid -> at: the owner's message carrying a relay ❓ (2 min)
    const REACT_LABEL_RE = /\b(add\s+)?(react|reacts|reaction|reactions|emoji)\b/i;
    const EXPAND_RE = /^(?:\+|(?:more|all|other|choose|pick|add|open|show(?:\s+(?:more|all))?)\s+(?:an?\s+)?(?:emojis?|reactions?)|emoji\s+(?:picker|keyboard|menu)|(?:open\s+)?emoji\s+picker)$/i;
    const EXPAND_BAD_RE = /delete|unsend|remove|report|block|mute|leave|forward|copy|edit|reply/i;
    const DANGER_RE = /delete|unsend|remove|report|copy|forward|edit|reply|pin|share|block|mute|leave|more|download|translate/i;
    const VOCAB = ['react', 'reaction', 'emoji', 'reply', 'more', 'copy', 'delete', 'edit', 'add', 'search', 'pin',
                   'forward', 'unsend', 'options', 'actions', 'close', 'menu', 'like', 'heart'];
    const EMOJI_NAMES = {
      '\u2705': ['check mark button', 'white heavy check mark', 'white check mark', 'check mark', 'green check mark'],
      '\u{1F440}': ['eyes'],
      '\u2753': ['question mark', 'red question mark', 'red question mark ornament', 'black question mark ornament', 'question'],
      '\u2764': ['red heart', 'heart', 'heavy black heart'],
      '\u{1F44D}': ['thumbs up', 'thumbs up sign'],
      '\u{1F44E}': ['thumbs down', 'thumbs down sign'],
      '\u{1F602}': ['face with tears of joy'],
      '\u203C': ['double exclamation mark'],
      '\u26A0': ['warning', 'warning sign'],
    };
    const SEARCH_WORD = { '\u2705': 'check', '\u{1F440}': 'eyes', '\u2753': 'question', '\u2764': 'heart',
                          '\u{1F44D}': 'thumbs up', '\u{1F44E}': 'thumbs down', '\u{1F602}': 'joy', '\u203C': 'exclamation', '\u26A0': 'warning' };
    const SEARCH_SEL = 'input[type="search"],input[placeholder*="earch"],input[aria-label*="earch"],input[placeholder*="moji"],input[aria-label*="moji"],[role="searchbox"],[role="combobox"]';
    const OPTION_SEL = 'button,[role="button"],[role="menuitem"],[role="menuitemradio"],[role="option"],[role="gridcell"],[role="img"],img[alt]';
    let uiChain = Promise.resolve();
    function uiSerial(fn) { const p = uiChain.then(fn, fn); uiChain = p.catch(() => {}); return p; }
    function ems(s) { return String(s || '').replace(/[\uFE0E\uFE0F\u200d]/g, '').trim(); }
    function sleep(ms) { return new Promise(r => win.setTimeout(r, ms)); }
    async function waitFor(fn, ms, step) {
      const end = realNow() + ms;
      for (;;) {
        let v = null; try { v = fn(); } catch (e) { v = null; }
        if (v) return v;
        if (realNow() >= end) return null;
        await sleep(step || 60);
      }
    }
    function labelOf(el) { return String((el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title'))) || ''); }
    function entryByUuid(u) {
      const want = String(u || '').toLowerCase();
      if (!/^[0-9a-f-]{8,64}$/.test(want)) return null;
      const all = allEntries();
      return all.find(e => uuidOf(e) === want) || null;
    }
    function pointOf(el) {
      const r = rect(el);
      if (r && (r.width > 0 || r.height > 0)) return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      return { x: 1, y: 1 };   // a zero-size box still has to look like a real pointer (CSS :hover will not)
    }
    function fire(el, types) {
      const W = (el.ownerDocument && el.ownerDocument.defaultView) || win;
      const p = pointOf(el);
      for (const t of types) {
        const init = { bubbles: true, cancelable: true, clientX: p.x, clientY: p.y, screenX: p.x, screenY: p.y,
                       button: 0, buttons: /down$/.test(t) ? 1 : 0, view: W };
        let ev;
        try {
          ev = /^pointer/.test(t) && W.PointerEvent ? new W.PointerEvent(t, init) : new W.MouseEvent(t, init);
        } catch (e) {
          try { ev = new W.MouseEvent(t, init); } catch (e2) { ev = new W.Event(t, { bubbles: true, cancelable: true }); }
        }
        try { el.dispatchEvent(ev); } catch (e) { /* detached */ }
      }
    }
    // v1.4.26: X ignores some untrusted clicks. A MAIN-world helper (installed by the background)
    // reads data-imr-op and calls the React handler. The attribute is the bridge; event detail is not.
    function pageAct(el, op) {
      if (!el || !el.setAttribute) return;
      let id = String(el.getAttribute('data-imr-act') || '');
      if (!/^[a-z0-9]+$/.test(id)) {
        id = 'a' + Math.random().toString(36).slice(2, 10);
        try { el.setAttribute('data-imr-act', id); } catch (e) { return; }
      }
      try { el.setAttribute('data-imr-op', op === 'press' ? 'press' : 'hover'); } catch (e) { return; }
      const W = (el.ownerDocument && el.ownerDocument.defaultView) || win;
      try { el.dispatchEvent(new W.CustomEvent('imr-act', { bubbles: true, composed: true })); } catch (e) { /* no helper */ }
    }
    function hover(entry) {
      const b = bodyEl(entry) || entry;
      const chain = [];
      for (const el of [b, entry]) if (el && chain.indexOf(el) < 0) chain.push(el);
      for (let a = parentOf(entry), i = 0; a && i < 4; a = parentOf(a), i++) chain.push(a);
      for (const el of chain) pageAct(el, 'hover');
      for (const el of [entry, b]) fire(el, ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointermove', 'mousemove']);
    }
    function press(el) {
      try { el.setAttribute('data-imr-did', ''); } catch (e) { /* detached */ }
      pageAct(el, 'press');
      if (el.getAttribute && el.getAttribute('data-imr-did') === '1') return;   // React handler already ran
      fire(el, ['pointerdown', 'mousedown', 'pointerup', 'mouseup']);
      try { el.click(); } catch (e) { fire(el, ['click']); }
    }
    function escape() {
      const W = doc.defaultView || win;
      for (const t of [doc.activeElement, doc.body, doc]) {
        if (!t) continue;
        try { t.dispatchEvent(new W.KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true })); } catch (e) { /* ignore */ }
      }
    }
    function otherEntry(el, entry) { const e = closestEntry(el); return !!(e && e !== entry); }
    function inBody(el, entry) { const b = bodyEl(entry); const q = findQuote(entry); return !!((b && deepContains(b, el)) || (q && deepContains(q, el))); }
    function isOpener(el) {
      const lab = labelOf(el);
      const tid = String((el.getAttribute && el.getAttribute('data-testid')) || '');
      const text = ems(el.textContent || '');
      const name = ems(lab || text);
      if (EXPAND_BAD_RE.test(lab) || EXPAND_BAD_RE.test(tid)) return false;
      // v1.4.26: the hover tray has no "React" button. "+" / "Add reaction" opens the picker.
      if (name === '+' || text === '+' || EXPAND_RE.test(name)) return true;
      if (DANGER_RE.test(lab) || DANGER_RE.test(tid)) return false;
      return REACT_LABEL_RE.test(lab) || /react|emoji|reaction/i.test(tid);
    }
    function isComposerControl(el) {
      return !!(el && el.closest && el.closest('[data-testid*="omposer"],[data-testid*="nbox"],nav'));
    }
    function boxOf(el) {
      const own = rect(el);
      if (own && (own.width || own.height)) return own;
      const kids = el && el.children ? el.children : [];
      for (let i = 0; i < kids.length && i < 6; i++) {
        const r = rect(kids[i]);
        if (r && (r.width || r.height)) return r;
      }
      return own || { left: 0, top: 0, width: 0, height: 0 };
    }
    function findReactButton(entry, appeared) {
      const scopes = [entry];
      for (let a = parentOf(entry), i = 0; a && i < 3; a = parentOf(a), i++) scopes.push(a);
      const seenEls = new Set();
      const er = boxOf(bodyEl(entry) || entry);
      const nearTray = el => {
        if (deepContains(entry, el)) return true;
        if (el.closest && el.closest('#layers,[role="dialog"],[role="menu"],[role="toolbar"]')) return true;
        // v1.4.26: a background tab has no layout. A control that appeared on hover is the tray.
        if (appeared && !appeared.has(el)) return true;
        const br = rect(el);
        if ((er.width || er.height) && (br.width || br.height)) {
          const dx = (br.left + br.width / 2) - (er.left + er.width / 2);
          const dy = (br.top + br.height / 2) - (er.top + er.height / 2);
          if (Math.abs(dx) < 420 && Math.abs(dy) < 320) return true;
        }
        return false;
      };
      const pick = list => list.find(el => {
        if (seenEls.has(el)) return false; seenEls.add(el);
        if (otherEntry(el, entry) || inBody(el, entry) || isExcluded(el) || isComposerControl(el) || !nearTray(el)) return false;
        return isOpener(el);
      });
      for (const s of scopes) { const b = pick(safeAll(s, 'button,[role="button"]')); if (b) return b; }
      const portal = pick(safeAll(doc, '#layers button,#layers [role="button"],[role="dialog"] button,[role="dialog"] [role="button"],[role="toolbar"] button,[role="toolbar"] [role="button"],[role="menu"] [role="menuitem"],[role="menu"] button'));
      if (portal) return portal;
      return pick(safeAll(doc, 'button,[role="button"]'));
    }
    function emojiCp(emoji) {
      const want = ems(emoji);
      return Array.from(want).map(c => c.codePointAt(0).toString(16)).join('-');
    }
    function nameHit(lab, emoji) {
      const want = ems(emoji);
      const raw = ems(lab).toLowerCase();
      if (!raw) return false;
      if (raw === want) return true;
      const names = EMOJI_NAMES[want] || [];
      if (names.includes(raw)) return true;
      const norm = raw.replace(/[:_\-]+/g, ' ').replace(/\s+/g, ' ').trim();
      if (names.includes(norm)) return true;
      return names.some(nm => raw === nm || raw.startsWith(nm + ' ') || raw.startsWith(nm + ',') || raw.startsWith(nm + ':'));
    }
    function emojiArt(el, emoji) {
      const cp = emojiCp(emoji);
      if (!cp || !el.getAttribute) return false;
      const art = String(el.getAttribute('src') || '') + ' ' + String(el.getAttribute('style') || '');
      return new RegExp('/' + cp + '\\.(svg|png)', 'i').test(art);
    }
    function matchesEmoji(el, emoji) {
      const want = ems(emoji);
      if (nameHit(labelOf(el), emoji)) return true;
      if (el.tagName === 'IMG' && ems(el.getAttribute('alt')) === want) return true;
      const own = ems(el.textContent || '');
      if (own === want) return true;
      // v1.4.16: X draws picker emoji as twemoji (img src / background-image .../2705.svg)
      if (emojiArt(el, emoji)) return true;
      const cp = emojiCp(emoji);
      const ims = el.querySelectorAll ? el.querySelectorAll('img') : [], im = ims.length === 1 ? ims[0] : null;
      return !!(im && im !== el && (ems(im.getAttribute('alt')) === want || (cp && new RegExp('/' + cp + '\\.(svg|png)', 'i').test(String(im.getAttribute('src') || '')))) &&
                !ems(el.textContent || ''));
    }
    function findEmojiOption(entry, emoji, exclude) {
      const list = safeAll(doc, OPTION_SEL);
      for (const el of list) {
        if (exclude && exclude.has(el)) continue;
        if (otherEntry(el, entry) || inBody(el, entry) || isExcluded(el) || isComposerControl(el)) continue;
        if (!matchesEmoji(el, emoji)) continue;
        const btn = el.tagName === 'IMG' || el.getAttribute('role') === 'img'
          ? (el.closest ? el.closest('button,[role="button"],[role="menuitem"],[role="option"],[role="gridcell"]') : null) || el : el;
        return btn;
      }
      return null;
    }
    function inActionTray(el, entry) {
      // The open tray (quick reactions + Reply/Delete, or the picker) is not a placed reaction.
      // A page shell that also wraps the bubble (a grid, or #layers around the app) is not the tray.
      if (el.closest) {
        const portal = el.closest('#layers,[role="dialog"],[role="menu"],[role="listbox"],[role="grid"]');
        if (portal && (!entry || !portal.contains(entry))) return true;
      }
      const box = el.parentElement;
      if (!box || box === entry) return false;
      if (!box.querySelectorAll) return false;
      const nodes = box.querySelectorAll('button,[role="button"],[role="menuitem"]');
      for (let i = 0; i < nodes.length && i < 24; i++) {
        const lab = ems(labelOf(nodes[i]) || nodes[i].textContent || '');
        if (/^(reply|delete|forward|copy|pin|report|info)$/i.test(lab)) return true;
      }
      return false;
    }
    function reactionRoots(entry) {
      const roots = [entry];
      for (const dir of ['previousElementSibling', 'nextElementSibling']) {
        let s = entry[dir];
        for (let i = 0; s && i < 2; i++, s = s[dir]) {
          if (isUuidEntry(s)) break;
          if (s.querySelector && s.querySelector('[data-testid^="message-"]')) break;
          roots.push(s);
        }
      }
      return roots;
    }
    function reactionCount(entry, emoji) {
      // v1.4.26: a placed ✅ is often only a twemoji img (empty alt) or an aria-label, not a text node.
      const want = ems(emoji);
      let n = 0;
      const seen = new Set();
      const hit = el => ems(el.getAttribute && el.getAttribute('alt')) === want || nameHit(labelOf(el), emoji) || emojiArt(el, emoji);
      for (const root of reactionRoots(entry)) {
        walkElements(root, el => {
          if (seen.has(el)) return;
          seen.add(el);
          if (root !== entry) {
            const owner = closestEntry(el);
            if (owner && owner !== entry) return;
          }
          if (inActionTray(el, entry)) return;
          if (inBody(el, entry)) {
            // Message text is not a reaction. A chip image drawn inside the bubble is.
            if (el.tagName === 'IMG' && hit(el)) n++;
            return;
          }
          for (let c = el.firstChild; c; c = c.nextSibling) if (c.nodeType === 3 && ems(c.nodeValue).includes(want)) n++;
          if (hit(el)) n++;
        });
      }
      return n;
    }
    function vocabSeen(entry) {
      const words = new Set();
      const els = [entry];
      for (let a = parentOf(entry), i = 0; a && i < 3; a = parentOf(a), i++) els.push(a);
      for (const s of els) for (const el of safeAll(s, 'button,[role="button"],[role="menuitem"]').slice(0, 60)) {
        if (inBody(el, entry) || otherEntry(el, entry)) continue;
        const toks = tokens(labelOf(el) + ' ' + (el.getAttribute('data-testid') || ''));
        for (const w of VOCAB) if (toks.includes(w)) words.add(w);
      }
      return Array.from(words).sort();
    }
    function setNativeValue(el, value) {
      const W = (el.ownerDocument && el.ownerDocument.defaultView) || win;
      if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
        const proto = el.tagName === 'TEXTAREA' ? W.HTMLTextAreaElement.prototype : W.HTMLInputElement.prototype;
        const d = Object.getOwnPropertyDescriptor(proto, 'value');
        if (d && d.set) d.set.call(el, value); else el.value = value;
        el.dispatchEvent(new W.Event('input', { bubbles: true }));
        el.dispatchEvent(new W.Event('change', { bubbles: true }));
        return;
      }
      try { el.focus(); } catch (e) { /* ignore */ }
      let done = false;
      try { if (doc.execCommand) { doc.execCommand('selectAll', false, null); done = doc.execCommand('insertText', false, value); } } catch (e) { done = false; }
      if (!done) {
        el.textContent = value;
        try { el.dispatchEvent(new W.InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' })); }
        catch (e) { el.dispatchEvent(new W.Event('input', { bubbles: true })); }
      }
    }
    function composerValue(el) { return (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') ? String(el.value || '') : String(el.textContent || ''); }

    async function uiReactNow(uuid, emoji, opt) {
      const t0 = realNow();
      const out = (ok, step, extra) => Object.assign({ ok, step, ms: realNow() - t0 }, extra || {});
      const entry = entryByUuid(uuid);
      if (!entry) return out(false, 'no-entry');
      const a = analyze(entry);
      if (a.direction !== 'outbound') return out(false, 'not-mine');     // only ever the owner's own message
      const want = ems(emoji);
      if (!want) return out(false, 'bad-emoji');
      if (want !== ems('\u2753')) clearPrompt(uuid);     // a sent / read / mirrored message is no longer a prompt
      const before = reactionCount(entry, want);
      if (before > 0) { if (opt && opt.prompt) watchPrompt(uuid, entry); return out(true, 'already'); }
      // v1.4.16: the chat list's own "Search" box exists before the picker opens. Only a search box
      // that appears with the picker is used. Stale emoji nodes are pills already on the page.
      const preInputs = new Set(safeAll(doc, 'input,textarea,[role="searchbox"],[role="combobox"]'));
      const stale = new Set(safeAll(doc, OPTION_SEL).filter(el => matchesEmoji(el, want)));
      const resting = new Set(safeAll(doc, 'button,[role="button"]'));
      const grab = () => {
        hover(entry);
        const direct = findEmojiOption(entry, want, stale);
        if (direct && !inBody(direct, entry)) return { kind: 'option', el: direct };
        const opener = findReactButton(entry, resting);
        if (opener) return { kind: 'open', el: opener };
        return null;
      };
      const first = await waitFor(grab, 1200, 40);
      if (!first) return out(false, 'no-react-button', { vocab: vocabSeen(entry) });
      let typed = null, expanded = false;
      if (first.kind === 'option') press(first.el);
      else {
        // Snapshot buttons once the tray is open so its "+" is not pressed again as "more".
        const preBtns = new Set(safeAll(doc, 'button,[role="button"]'));
        press(first.el);
        const step = () => {
          const o = findEmojiOption(entry, want, stale);
          if (o) return o;
          const search = safeAll(doc, SEARCH_SEL).find(el => !preInputs.has(el) && el !== typed && !otherEntry(el, entry) &&
            !/composer|inbox|conversation/i.test(String(el.getAttribute('data-testid') || '')) &&
            !(el.closest && el.closest('[data-testid*="omposer"],[data-testid*="nbox"],nav')));
          if (search) { typed = search; setNativeValue(search, SEARCH_WORD[want] || ''); return null; }
          if (!expanded && !typed && realNow() - t0 > 350) {
            // Quick row has no ✅: open the full picker with a NEW button, never Reply/Delete.
            const more = safeAll(doc, 'button,[role="button"]').find(el => !preBtns.has(el) && !otherEntry(el, entry) && !inBody(el, entry) &&
              !isExcluded(el) && EXPAND_RE.test(ems(labelOf(el) || el.textContent || '')) && !EXPAND_BAD_RE.test(labelOf(el)));
            if (more) { expanded = true; press(more); }
          }
          return null;
        };
        const option = await waitFor(step, 3000, 40);
        if (!option) { escape(); return out(false, 'no-emoji', { vocab: vocabSeen(entry), searched: !!typed, expanded }); }
        press(option);
      }
      const ok = await waitFor(() => reactionCount(entry, want) > before, 2500, 40);
      if (!ok) { escape(); return out(false, 'not-placed'); }
      if (opt && opt.prompt) watchPrompt(uuid, entry);
      return out(true, 'placed');
    }

    async function uiTypeNow(text) {
      const t0 = realNow();
      const out = (ok, step) => ({ ok, step, ms: realNow() - t0 });
      const s = String(text || '');
      if (!UI_TAG_RE.test(s) || s.length > 600) return out(false, 'untagged');      // only relay prompts, ever
      const box = safeAll(doc, '[data-testid="dm-composer-textarea"]')[0];
      if (!box) return out(false, 'no-composer');
      if (composerValue(box).trim()) return out(false, 'composer-busy');            // never touch his draft
      const norm = v => normSpaces(v).replace(/\s+/g, ' ').trim();
      const countMine = () => allEntries().filter(e => norm(entryText(e).text) === norm(s)).length;
      const before = countMine();
      setNativeValue(box, s);
      const btn = await waitFor(() => {
        const b = safeAll(doc, '[data-testid="dm-composer-send-button"]')[0];
        return b && !b.disabled && b.getAttribute('aria-disabled') !== 'true' ? b : null;
      }, 1500);
      if (!btn || norm(composerValue(box)) !== norm(s)) {
        if (norm(composerValue(box)) === norm(s)) setNativeValue(box, '');
        return out(false, btn ? 'composer-changed' : 'no-send-button');
      }
      press(btn);
      const ok = await waitFor(() => countMine() > before, 5000, 100);
      if (ok) return out(true, 'sent');
      if (!composerValue(box).trim()) return out(true, 'cleared');                 // X took it; bubble not seen yet
      if (norm(composerValue(box)) === norm(s)) setNativeValue(box, '');
      return out(false, 'not-sent');
    }

    // ---------- v1.4.6 attachments the owner sends (photos/files in HIS outbound bubbles) ----------
    // Only image elements inside his own bubble; the bytes are read in-page from the image the X client
    // already decrypted and shown, in 512 KiB chunks the service worker forwards to the Mac. Nothing is
    // stored: the buffer is released as soon as the send is built (or after MEDIA_HOLD_MS).
    const MEDIA_MAX_BYTES = 25 * 1024 * 1024, MEDIA_CHUNK = 512 * 1024, MEDIA_MAX = 10, MEDIA_HOLD_MS = 5 * 60 * 1000;
    const mediaRefs = new Map();       // uuid -> {srcs, at}
    const mediaBufs = new Map();       // uuid#i -> {bytes, mime, at}
    const MEDIA_SRC_RE = /^(blob:|data:image\/|https:\/\/(ton|pbs)\.twimg\.com\/)/i;
    function isEmojiImg(img) {
      const src = String(img.getAttribute('src') || '');
      const alt = String(img.getAttribute('alt') || '');
      if (/\/emoji\/|twemoji|abs(-\d)?\.twimg\.com\/(emoji|hashflags)|profile_images|\/avatar/i.test(src)) return true;
      if (alt && alt.length <= 4 && !/[A-Za-z0-9]/.test(alt)) return true;   // an emoji glyph image
      const w = Number(img.getAttribute('width') || img.naturalWidth || 0);
      return w > 0 && w < 40;
    }
    function mediaOf(entry) {
      const out = [];
      let q = null;
      try { q = findQuote(entry); } catch (e) { q = null; }
      const cards = cardInfo(entry);
      const pv = previewInfo(entry);
      for (const img of safeAll(entry, 'img')) {
        const src = String(img.getAttribute('src') || '');
        if (!src || !MEDIA_SRC_RE.test(src) || isEmojiImg(img) || isExcluded(img)) continue;
        if (q && deepContains(q, img)) continue;      // a photo in the quoted message is theirs, never re-sent
        if (inPreview(entry, img, pv)) continue;      // v1.4.10: so is the thumbnail in a native Reply preview
        if (inCard(entry, img, cards)) continue;      // a shared post / link thumbnail: the URL goes instead
        if (!out.includes(src)) out.push(src);
        if (out.length >= MEDIA_MAX) break;
      }
      return out;
    }
    function mediaShell(entry) {        // a photo bubble whose image has not loaded yet
      if (mediaOf(entry).length) return false;
      const pv = previewInfo(entry);
      return safeAll(entry, '[data-testid*="image" i],[data-testid*="photo" i],[data-testid*="media" i],[aria-label*="image" i],[aria-label*="photo" i]')
        .some(el => !inPreview(entry, el, pv));     // v1.4.10: a quoted photo's placeholder is not his photo
    }
    function pruneMedia() {
      const t = realNow();
      for (const [k, v] of mediaRefs) if (t - v.at > MEDIA_HOLD_MS) mediaRefs.delete(k);
      for (const [k, v] of mediaBufs) if (t - v.at > MEDIA_HOLD_MS) mediaBufs.delete(k);
    }
    function hex(buf) { return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join(''); }
    function b64(bytes) {
      let s = '';
      for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      return (win.btoa || btoa)(s);
    }
    function sniffMime(b) {
      if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
      if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return 'image/png';
      if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return 'image/gif';
      if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 && b[8] === 0x57 && b[9] === 0x45) return 'image/webp';
      return '';
    }
    async function uiMediaInfo(uuid, index) {
      pruneMedia();
      const ref = mediaRefs.get(String(uuid).toLowerCase());
      const src = ref && ref.srcs[Number(index) || 0];
      if (!src) return { ok: false, step: 'no-media' };
      try {
        const f = win.fetch || (typeof fetch !== 'undefined' ? fetch : null);
        const r = await f(src, { credentials: 'include' });
        if (!r || !r.ok) return { ok: false, step: 'fetch-failed' };
        const len = Number((r.headers && r.headers.get && r.headers.get('content-length')) || 0);
        if (len > MEDIA_MAX_BYTES) return { ok: false, step: 'too-large', size: len };
        const buf = await r.arrayBuffer();
        if (buf.byteLength > MEDIA_MAX_BYTES) return { ok: false, step: 'too-large', size: buf.byteLength };
        if (!buf.byteLength) return { ok: false, step: 'empty' };
        const bytes = new Uint8Array(buf);
        const mime = sniffMime(bytes) || String((r.headers && r.headers.get && r.headers.get('content-type')) || '').split(';')[0].slice(0, 60);
        const sub = (win.crypto && win.crypto.subtle) || (typeof crypto !== 'undefined' && crypto.subtle);
        const sha256 = hex(await sub.digest('SHA-256', bytes));
        mediaBufs.set(String(uuid).toLowerCase() + '#' + index, { bytes, mime, at: realNow() });
        const ext = { 'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp' }[mime] || '';
        return { ok: true, size: bytes.length, mime, sha256, name: (mime.indexOf('image/') === 0 ? 'photo' : 'attachment') + (ext ? '.' + ext : ''),
                 chunks: Math.ceil(bytes.length / MEDIA_CHUNK) };
      } catch (e) { return { ok: false, step: 'read-failed' }; }
    }
    function uiMediaChunk(uuid, index, seq) {
      const b = mediaBufs.get(String(uuid).toLowerCase() + '#' + index);
      if (!b) return { ok: false, step: 'expired' };
      const start = (Number(seq) || 0) * MEDIA_CHUNK;
      if (start >= b.bytes.length) return { ok: false, step: 'past-end' };
      return { ok: true, data: b64(b.bytes.subarray(start, start + MEDIA_CHUNK)) };
    }
    function uiMediaRelease(uuid) {
      const u = String(uuid).toLowerCase();
      mediaRefs.delete(u);
      for (const k of Array.from(mediaBufs.keys())) if (k.indexOf(u + '#') === 0) mediaBufs.delete(k);
      return { ok: true };
    }

    async function uiAwaitUndoNow(uuid, ms) {
      const end = realNow() + Math.max(0, Math.min(10000, Number(ms) || 0));
      for (;;) {
        const e = entryByUuid(uuid);
        if (e && analyze(e).direction === 'outbound' && reactionState(e).trash) return { trashed: true };
        if (realNow() >= end) return { trashed: false, seen: !!e };
        await sleep(150);
      }
    }
    function watchPrompt(uuid, entry) {
      const u = String(uuid).toLowerCase();
      uiPromptUuids.set(u, now());
      const st = reactionState(entry);
      promptMarks.set(u, { at: now(), up: st.up, down: st.down, fired: false });
    }
    function clearPrompt(uuid) {
      const u = String(uuid || '').toLowerCase();
      if (uiPromptUuids.delete(u)) { const mk = promptMarks.get(u); if (mk) mk.fired = true; }
    }
    function isUiPrompt(u) {
      const at = uiPromptUuids.get(u);
      if (at === undefined) return false;
      if (now() - at > PROMPT_WINDOW_MS) { uiPromptUuids.delete(u); return false; }
      return true;
    }

    const OBS_OPTS = { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['aria-label', 'alt'] };
    return {
      config: cfg,
      doc,
      extractText,                 // v1.3.9: exposed for unit tests (pure, no side effects)
      stripTrailingChips,
      start() {
        lastUrl = getContextUrl();
        observer = new MO(onMutations);
        observer.observe(doc.body || doc.documentElement, OBS_OPTS);
        observeShadowRoots(doc.documentElement);
        try { doc.addEventListener('keydown', onTypedKey, true); doc.addEventListener('click', onTypedClick, true); } catch (e) { /* tests */ }
        baseline('start');
        urlTimer = win.setInterval(checkUrl, cfg.urlPollMs);
        shadowTimer = win.setInterval(() => { try { observeShadowRoots(doc.documentElement); } catch (e) { /* doc gone */ } }, cfg.shadowRescanMs);
        if (opts.frameScanLog !== false) scanTimer = win.setTimeout(() => { scanTimer = null; try { logFrameScan(); } catch (e) { /* doc gone */ } }, cfg.frameScanDelayMs);
      },
      stop() {
        if (observer) observer.disconnect();
        noteHostAck(false);
        [urlTimer, shadowTimer].forEach(t => t && win.clearInterval(t));
        [flushTimer, scanTimer].forEach(t => t && win.clearTimeout(t));
        observer = null; urlTimer = null; flushTimer = null; shadowTimer = null; scanTimer = null;
      },
      frameScan,
      hasEntries() { return allEntries().length > 0 || !!lastEls.any; },
      rebaseline: baseline,
      flushNow() { if (flushTimer) { win.clearTimeout(flushTimer); flushTimer = null; } flush(); },
      nudge() { try { const root = chatRoot().el; if (root) pending.add(root); schedule(0); } catch (e) { /* doc gone */ } },
      debugSnapshot,
      pageStructure,               // v1.4.3: popup "Copy page structure" (text-free)
      structureOf,
      status() { return { conversationId: conversationId(), mode, chatRoot: chatRoot().selector, entriesOnPage: allEntries().length, ownerConfirmed: !!ownerCheck(), stats: Object.assign({}, stats) }; },
      _analyze: analyze,
      uiReact: (uuid, emoji, opt) => uiSerial(() => uiReactNow(uuid, emoji, opt)),   // v1.4.6
      uiType: text => uiSerial(() => uiTypeNow(text)),
      uiAwaitUndo: (uuid, ms) => uiAwaitUndoNow(uuid, ms),
      noteHostAck,                     // v1.4.24: background ack cancels the unacked warning
      uiClearPrompt: clearPrompt,
      uiMediaInfo, uiMediaChunk, uiMediaRelease,  // v1.4.6 attachments
      _mediaOf: mediaOf,
      _cardInfo: cardInfo,
      _canonicalCardUrl: canonicalCardUrl,
    };
  }

  // ---------- Node (tests) export ----------
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { CONFIG, createRelayDetector };
    return;
  }

  // ---------- Chrome boot ----------
  // Injected into the top frame of an x.com page AND into same-origin about:srcdoc/about:blank
  // child frames (manifest all_frames + match_about_blank). Every injected frame runs ONE detector
  // on its OWN document and takes the conversation id from the top URL. There is no cross-document
  // fallback. A second injection into the same frame is a no-op.
  if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.id) return;
  if (window.__imessageRelayLoaded) return;
  window.__imessageRelayLoaded = true;
  // v1.4.26: a background tab must answer before storage resolves. A ping that waited on
  // storage looked like a dead script, and the worker then reloaded the tab it had just injected.
  try {
    chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
      if (!msg || msg.type !== 'relay-ping') return false;
      try { sendResponse({ ok: true, pong: true }); } catch (e) { /* port closed */ }
      return false;
    });
  } catch (e) { /* runtime gone */ }
  try {
    const arm = chrome.runtime.sendMessage({ type: 'relay-arm-act' });
    if (arm && arm.catch) arm.catch(() => {});
  } catch (e) { /* extension reloading */ }

  const isTop = window === window.top;
  const role = isTop ? 'top' : 'frame';

  function topDoc() { try { return window.top.document; } catch (e) { return document; } }
  function contextUrl() {
    if (isTop) return location.href;
    try { return window.top.location.href; } catch (e) { /* cross-origin */ }
    try { return window.parent.location.href; } catch (e) { /* cross-origin */ }
    return document.referrer || '';
  }
  function isChatPath(href) {
    try { const p = new URL(href, 'https://x.com').pathname; return CONFIG.conversationUrlPatterns.some(re => re.test(p)); } catch (e) { return false; }
  }

  const state = { paused: false, conversationFilter: '' };

  // Positive owner-identity check (item 1). twid cookie u%3D<id> proves the owner's session; else
  // the owner's account-switcher avatar in the TOP document. No identity configured => not confirmed.
  function ownerConfirmed() {
    const uid = String(CONFIG.ownerXUserId || '').replace(/[^0-9]/g, '');
    let cookie = '';
    try { cookie = topDoc().cookie || ''; } catch (e) { cookie = ''; }
    if (!cookie) { try { cookie = document.cookie || ''; } catch (e) { cookie = ''; } }
    // twid cookie proves the logged-in user id. Accept the url-encoded form (twid=u%3D<id>) and the
    // quoted / plain legacy forms (twid="u=<id>" or twid=u=<id>). A readable twid is FINAL: equality
    // decides and there is no avatar fallback (item 1, T9).
    const tw = /(?:^|;\s*)twid=(?:")?u(?:%3[dD]|=)(\d+)/i.exec(cookie);
    if (uid) {
      if (tw) return tw[1] === uid;                 // twid present => equality is final; no fallback
      // no readable twid cookie (HttpOnly / absent): fall through to the account-switcher avatar
    }
    const h = String(CONFIG.ownerXHandle || '').replace(/^@/, '').toLowerCase();
    if (h) {
      try {
        const want = 'useravatar-container-' + h;
        const matches = a => (a.getAttribute('data-testid') || '').toLowerCase() === want;
        // Prefer the account switcher: if one exists, ONLY an avatar inside it proves the logged-in
        // account. An owner avatar drawn in the conversation header must never count.
        let scope = null;
        const cands = topDoc().querySelectorAll('[data-testid]');
        for (let i = 0; i < cands.length; i++) { if (/accountswitcher/i.test(cands[i].getAttribute('data-testid') || '')) { scope = cands[i]; break; } }
        if (scope) {
          const avatars = scope.querySelectorAll('[data-testid^="UserAvatar-Container-"]');
          for (let j = 0; j < avatars.length; j++) { if (matches(avatars[j])) return true; }
          return false;                               // switcher present but owner not in it
        }
        // No account-switcher element AND no readable twid: FAIL CLOSED. Only the account-nav avatar
        // counts -- an owner avatar that sits OUTSIDE every conversation / inbox / header / main
        // region. An avatar drawn inside the DM thread, the inbox list, a header, or the main column
        // is not proof of the logged-in account, so it cannot pass (item 1, T7-T9).
        const EXCLUDE = '[data-testid="dm-conversation-panel"],[data-testid="dm-inbox-panel"],header,[role="main"]';
        const avatars = topDoc().querySelectorAll('[data-testid^="UserAvatar-Container-"]');
        for (let j = 0; j < avatars.length; j++) {
          if (matches(avatars[j]) && !avatars[j].closest(EXCLUDE)) return true;
        }
      } catch (e) { /* ignore */ }
    }
    return false;
  }


  function send(msg) {
    try { const p = chrome.runtime.sendMessage(msg); if (p && p.catch) p.catch(() => {}); return p; }
    catch (e) { console.warn(CONFIG.logPrefix, 'extension context gone (reloaded?). Refresh the tab.', e); return null; }
  }
  function onEmit(item) {
    if (item.refuse) console.log(CONFIG.logPrefix, '(' + item.source + ') REFUSED', item.refuse, '(' + item.textLength + ' chars)');
    else console.log(CONFIG.logPrefix, '(' + item.source + ')', item.direction.toUpperCase(), '[' + item.textLength + ' chars]');
    send({ type: 'relay-log', item });
  }
  function onStatus(sig) { send({ type: 'relay-signal', signal: sig }); }
  function onReaction(r) { console.log(CONFIG.logPrefix, 'reaction on a prompt:', r.word); send({ type: 'relay-reaction', item: r }); }

  // Cross-session dedupe of already-relayed uuids, persisted in chrome.storage.local (keys only,
  // 24h TTL). Loaded once; writes are best-effort and debounced.
  const SENT_TTL_MS = 24 * 60 * 60 * 1000;
  const sentUuids = new Map();   // uuid -> ts
  let sentDirty = false, sentWriteTimer = null;
  const seenStore = {
    has: u => sentUuids.has(u),
    add: u => { if (!u) return; sentUuids.set(u, Date.now()); sentDirty = true; scheduleSentWrite(); },
  };
  function scheduleSentWrite() {
    if (sentWriteTimer) return;
    sentWriteTimer = setTimeout(() => {
      sentWriteTimer = null;
      if (!sentDirty) return;
      sentDirty = false;
      const obj = {};
      const cut = Date.now() - SENT_TTL_MS;
      for (const [u, t] of sentUuids) if (t >= cut) obj[u] = t;
      try { chrome.storage.local.set({ sentUuids: obj }); } catch (e) { /* ignore */ }
    }, 1500);
  }
  function loadSentUuids() {
    return chrome.storage.local.get('sentUuids').then(v => {
      const obj = v.sentUuids || {};
      const cut = Date.now() - SENT_TTL_MS;
      for (const u in obj) if (obj[u] >= cut) sentUuids.set(u, obj[u]);
    }).catch(() => {});
  }

  let mainDetector = null;
  function startMain() {
    if (mainDetector) return;
    mainDetector = createRelayDetector({
      window, document, getState: () => state, getContextUrl: contextUrl, source: role,
      onEmit, onStatus, onReaction, ownerCheck: ownerConfirmed, seen: seenStore,
    });
    mainDetector.start();
    console.log(CONFIG.logPrefix, '(' + role + ') started (read-only).', { frameHref: location.href, context: contextUrl(), paused: state.paused });
    if (role === 'frame') send({ type: 'relay-hello', role: 'frame' });
  }

  function setNames(v) {
    const clean = a => (Array.isArray(a) ? a : []).map(String).filter(Boolean).slice(0, 10);
    CONFIG.ownerNames.splice(0, CONFIG.ownerNames.length, ...clean(v.ownerNames));
    CONFIG.botNames.splice(0, CONFIG.botNames.length, ...clean(v.senderNames));
    CONFIG.ownerXUserId = String(v.ownerXUserId || '');
    CONFIG.ownerXHandle = (clean(v.ownerNames)[0] || '');
  }

  Promise.all([
    chrome.storage.local.get(['paused', 'ownerNames', 'senderNames', 'relayConversationId', 'ownerXUserId']),
    loadSentUuids(),
  ]).then(([v]) => {
    state.paused = !!v.paused;
    state.conversationFilter = String(v.relayConversationId || '');
    setNames(v);
    const here = () => (role === 'frame' ? contextUrl() : location.href);
    const tick = () => { if (isChatPath(here())) startMain(); };   // frame polls the top URL like the top frame (item 7)
    tick();
    setInterval(tick, 1000);
    // v1.4.6: while the relay chat is open, nudge the background every 15 s so queued UI actions (a
    // mirrored tapback, a read receipt's 👀) are picked up. Ids only travel this way, never text.
    setInterval(() => {
      if (mainDetector && !state.paused && mainDetector.hasEntries()) send({ type: 'relay-ui-tick' });
    }, 15000);
    // v1.4.26: a background tab can delay mutation delivery. Rescan even while hidden.
    setInterval(() => { if (mainDetector) mainDetector.nudge(); }, 2000);
    try { document.addEventListener('visibilitychange', () => { if (mainDetector) mainDetector.nudge(); }); } catch (e) { /* tests */ }
    send({ type: 'relay-arm-act' });   // MAIN-world click helper for the reaction tray
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes.paused) state.paused = !!changes.paused.newValue;
    if (changes.relayConversationId) state.conversationFilter = String(changes.relayConversationId.newValue || '');
    if (changes.ownerNames || changes.senderNames || changes.ownerXUserId) {
      chrome.storage.local.get(['ownerNames', 'senderNames', 'ownerXUserId']).then(setNames);
    }
  });

  // Troubleshooting: a "relay-debug" message answered by the detector in this frame (if it has
  // messages). No cross-frame fallback any more; each frame answers for itself.
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === 'relay-structure') {          // v1.4.3 popup button; text-free
      if (!mainDetector || !mainDetector.hasEntries()) return false;
      try { sendResponse({ ok: true, from: role, structure: mainDetector.pageStructure() }); } catch (e) { sendResponse({ ok: false, error: String(e) }); }
      return false;
    }
    if (msg && msg.type === 'relay-ack') {                // v1.4.24: the host answered this send
      if (mainDetector) mainDetector.noteHostAck(!!msg.hold);
      return false;
    }
    if (msg && msg.type === 'relay-ui') {                 // v1.4.6: background asks for one UI action
      if (!mainDetector || !mainDetector.hasEntries()) { sendResponse({ ok: false, step: 'no-entry' }); return false; }
      if (state.paused) { sendResponse({ ok: false, step: 'paused' }); return false; }
      let p;
      if (msg.action === 'react') p = mainDetector.uiReact(String(msg.uuid || ''), String(msg.emoji || ''), { prompt: !!msg.prompt });
      else if (msg.action === 'type') p = mainDetector.uiType(String(msg.text || ''));
      else if (msg.action === 'await-undo') p = mainDetector.uiAwaitUndo(String(msg.uuid || ''), Number(msg.ms) || 0);
      else if (msg.action === 'clear-prompt') { mainDetector.uiClearPrompt(String(msg.uuid || '')); sendResponse({ ok: true }); return false; }
      else if (msg.action === 'media-info') p = mainDetector.uiMediaInfo(String(msg.uuid || ''), Number(msg.index) || 0);
      else if (msg.action === 'media-chunk') { sendResponse(mainDetector.uiMediaChunk(String(msg.uuid || ''), Number(msg.index) || 0, Number(msg.seq) || 0)); return false; }
      else if (msg.action === 'media-release') { sendResponse(mainDetector.uiMediaRelease(String(msg.uuid || ''))); return false; }
      else { sendResponse({ ok: false, step: 'unknown-action' }); return false; }
      p.then(r => sendResponse(r)).catch(e => sendResponse({ ok: false, step: 'error' }));
      return true;
    }
    if (!msg || msg.type !== 'relay-debug') return false;
    if (!mainDetector || !mainDetector.hasEntries()) return false;
    try { sendResponse({ ok: true, from: role, snapshot: mainDetector.debugSnapshot() }); } catch (e) { sendResponse({ ok: false, error: String(e) }); }
    return false;
  });
})();
