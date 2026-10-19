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
    // is the ONLY element the reply body is read from.
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
    function extractText(textEl) {
      const parts = [];   // strings, or {chip: text} for chip-only elements (kept only if not trailing)
      function walk(node) {
        for (let c = node.firstChild; c; c = c.nextSibling) {
          if (c.nodeType === 3) { parts.push(c.nodeValue); continue; }
          if (c.nodeType !== 1) continue;
          if (c.tagName === 'BR') { parts.push('\n'); continue; }
          if (isStripEl(c)) continue;
          const block = cfg.blockTags.includes(c.tagName);
          if (!c.shadowRoot && isChipText(c.textContent)) {
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
    function entryText(entry) {
      const el = bodyEl(entry);
      return { textEl: el, text: el ? extractText(el) : '' };
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
    function previewQuote(entry) {
      const body = bodyEl(entry);
      if (!body || body === entry) return '';
      let art = null;
      for (let n = body; n && n !== entry; n = parentOf(n)) if (n.getAttribute && n.getAttribute('role') === 'article') { art = n; break; }
      const start = art || body;
      const parts = [];
      for (let n = start; n && n !== entry; n = parentOf(n)) {
        const sibs = [];
        for (let sib = n.previousElementSibling; sib; sib = sib.previousElementSibling) {
          if (isUuidEntry(sib) || safeAll(sib, cfg.uuidEntryPrefixSelector).some(isUuidEntry)) break;   // another message
          if (isHiddenEl(sib)) continue;
          const t = visibleText(sib);
          if (t && !isChipText(t)) sibs.unshift(t);
        }
        if (sibs.length) parts.unshift(sibs.join(' '));
        if (parts.length) break;            // the nearest level with a preview wins
      }
      return parts.join(' ').trim();
    }
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

      const direction = score >= cfg.decisionThreshold ? 'outbound' : score <= -cfg.decisionThreshold ? 'inbound' : 'unknown';
      signals.push('score:' + score);
      const key = conv + '|id:uuid=' + uuid;
      const quotedText = entryQuote(entry);
      if (quotedText) signals.push('quote:' + quotedText.length);
      return { conv, uuid, text, quotedText, timeIso: entryTimeIso(entry), key, direction, score, signals, textEl, bubbleEl: bubble && bubble.el, ref };
    }

    // ---------- baseline / flush ----------
    function observeShadowRoots(root) {
      if (!observer) return;
      const add = el => { if (el.shadowRoot && !observedRoots.has(el.shadowRoot)) { observedRoots.add(el.shadowRoot); observer.observe(el.shadowRoot, OBS_OPTS); } };
      if (root && root.nodeType === 1) add(root);
      walkElements(root, add);
    }

    function baseline(reason) {
      rootMemo = null;
      mode = detectMode(chatRoot().el || doc.body);
      const list = allEntries();
      for (const e of list) { processed.add(e); seenKeys.add(conversationId() + '|id:uuid=' + uuidOf(e)); }
      stats.baselined += list.length;
      try { scanReactions(list); } catch (e) { /* prompts get marked on their next change */ }
      settleUntil = now() + cfg.settleMs;
      recentEmits = [];
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
        if (analyze(e).direction === 'outbound') continue;   // the owner's own message is never a prompt
        mk.fired = true;
        try { onReaction({ conversationId: conv, key: 'reaction:' + u, word, ts: new Date(now()).toISOString() }); } catch (err) { info('reaction emit failed', err); }
      }
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
        if (!a.text) {
          const n = retryCounts.get(e) || 0;
          if (n < cfg.emptyTextRetries && !baselineReason) {
            retryCounts.set(e, n + 1);
            win.setTimeout(() => { pending.add(e); schedule(0); }, cfg.emptyRetryDelayMs);
            continue;
          }
        }
        processed.add(e);
        if (seenKeys.has(a.key)) { stats.skippedDup++; continue; }
        seenKeys.add(a.key);
        if (baselineReason) continue;

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
        if (recentEmits.some(r => r.text === a.text && r.direction === a.direction)) { stats.skippedDup++; continue; }
        recentEmits.push({ text: a.text, direction: a.direction, t });

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
        const item = { ts, conversationId: conv, direction: a.direction, text: a.text, signals: a.signals, score: a.score, key: a.key, source };
        if (a.quotedText) item.quotedText = a.quotedText;
        if (a.direction !== 'inbound') { try { item.quoteProbe = structureOf(e); } catch (err) { /* diagnostics only */ } }
        try { emit(item); } catch (err) { info('emit failed', err); }
      }
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
        baseline('start');
        urlTimer = win.setInterval(checkUrl, cfg.urlPollMs);
        shadowTimer = win.setInterval(() => { try { observeShadowRoots(doc.documentElement); } catch (e) { /* doc gone */ } }, cfg.shadowRescanMs);
        if (opts.frameScanLog !== false) scanTimer = win.setTimeout(() => { scanTimer = null; try { logFrameScan(); } catch (e) { /* doc gone */ } }, cfg.frameScanDelayMs);
      },
      stop() {
        if (observer) observer.disconnect();
        [urlTimer, shadowTimer].forEach(t => t && win.clearInterval(t));
        [flushTimer, scanTimer].forEach(t => t && win.clearTimeout(t));
        observer = null; urlTimer = null; flushTimer = null; shadowTimer = null; scanTimer = null;
      },
      frameScan,
      hasEntries() { return allEntries().length > 0 || !!lastEls.any; },
      rebaseline: baseline,
      flushNow() { if (flushTimer) { win.clearTimeout(flushTimer); flushTimer = null; } flush(); },
      debugSnapshot,
      pageStructure,               // v1.4.3: popup "Copy page structure" (text-free)
      structureOf,
      status() { return { conversationId: conversationId(), mode, chatRoot: chatRoot().selector, entriesOnPage: allEntries().length, ownerConfirmed: !!ownerCheck(), stats: Object.assign({}, stats) }; },
      _analyze: analyze,
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
    if (!msg || msg.type !== 'relay-debug') return false;
    if (!mainDetector || !mainDetector.hasEntries()) return false;
    try { sendResponse({ ok: true, from: role, snapshot: mainDetector.debugSnapshot() }); } catch (e) { sendResponse({ ok: false, error: String(e) }); }
    return false;
  });
})();
