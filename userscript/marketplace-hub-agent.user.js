// ==UserScript==
// @name         Marketplace Hub Agent
// @namespace    marketplace-hub
// @version      1.1.2
// @description  Collects unanswered Facebook Marketplace conversations, sends them to your Marketplace Hub server and (optionally) types/sends the AI-generated answers.
// @match        https://www.facebook.com/marketplace/inbox*
// @match        https://www.facebook.com/messages/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      *
// @noframes
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const VERSION = '1.1.2';

  /* ------------------------------------------------------------------ *
   *  SETTINGS YOU MAY NEED TO TUNE
   *  Facebook changes its markup often. If conversations are not found,
   *  use the menu command "Test scrape" and adjust the selectors below.
   * ------------------------------------------------------------------ */
  const CFG = {
    MAX_ROWS_SCANNED: 30,      // newest N Marketplace chats looked at per cycle
    MAX_THREADS_PER_CYCLE: 8,  // chats actually opened per cycle
    FAILED_RETRY_MS: 10 * 60 * 1000,
    // Facebook shows "Marketplace" as a folder row in the chat list; clicking it opens the Marketplace-only list.
    // Add your language here if your Facebook is not English/French/Arabic.
    MARKETPLACE_RE: /marketplace|marché|ماركت|السوق/i,
    // "You: ..." preview = you already answered. Add translations if needed.
    PREVIEW_ME_RE: /^(you|vous|tu|usted|أنت)\s*:/i,
    // Heading above your own messages ("You sent"). Anything else is the buyer.
    OWN_RE: /^(you|vous|tu|usted)(\s|$)|^أنت(\s|$)/i,
    SELECTORS: {
      gridAny: 'div[role="grid"]',
      threadLink: 'a[href*="/t/"]',
      log: 'div[role="log"]',                                   // the open conversation
      message: '[data-message-id]',
      composer: 'div[role="textbox"][data-lexical-editor="true"]',
      sendButtons: ['[aria-label="Press enter to send"]', '[aria-label="Press Enter to send"]', '[aria-label="Send"]'],
      buyerProfile: 'a[href*="/marketplace/profile/"]',
    },
    // Return false to skip a conversation (e.g. where you are the buyer). Default: answer every Marketplace chat.
    shouldHandle: (/* ctx: {customerName, itemTitle} */) => true,
    DEBUG: false,
  };

  /* ------------------------------ helpers ----------------------------- */
  const TAB_ID = Date.now().toString(36) + Math.random().toString(36).slice(2, 6); // only used to label this tab
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);
  const log = (...a) => CFG.DEBUG && console.log('[MarketplaceHub]', ...a);
  const server = () => (GM_getValue('server_url', '') || '').replace(/\/+$/, '');
  const apiKey = () => GM_getValue('api_key', '');
  const fbId = () => ((document.cookie.match(/(?:^|;\s*)c_user=(\d+)/) || [])[1] || '');
  const state = { cfg: null, running: false, failedAt: {}, sigs: GM_getValue('sigs', {}) };
  CFG.DEBUG = !!GM_getValue('debug', false);

  function hash(str) { // cyrb53, enough for message ids
    let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
    for (let i = 0; i < str.length; i++) {
      const c = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 2654435761); h2 = Math.imul(h2 ^ c, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
  }

  async function waitFor(fn, timeout = 4000, step = 150) {
    const end = Date.now() + timeout;
    while (Date.now() < end) { const v = fn(); if (v) return v; await sleep(step); }
    return null;
  }

  /* ------------------------------ status badge ------------------------ */
  const badge = document.createElement('div');
  badge.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483647;max-width:320px;padding:6px 10px;border-radius:8px;font:12px/1.35 system-ui,sans-serif;color:#fff;background:#445;box-shadow:0 2px 8px #0005;pointer-events:none';
  document.documentElement.appendChild(badge);
  function setStatus(text, level = 'info') {
    badge.textContent = 'Hub: ' + text;
    badge.style.background = { ok: '#1f7a4d', info: '#35506b', warn: '#b7791f', error: '#b3261e' }[level] || '#445';
    log(level, text);
  }

  /* ------------------------------ server API -------------------------- */
  function api(method, path, body) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method, url: server() + '/api/v1' + path, timeout: 70000,
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey() },
        data: body ? JSON.stringify(body) : undefined,
        onload: (r) => {
          let j = {}; try { j = JSON.parse(r.responseText); } catch (e) { j = { error: 'bad_response' }; }
          j.status = r.status; if (r.status >= 400) j.ok = false; resolve(j);
        },
        onerror: () => resolve({ ok: false, error: 'network_error', status: 0 }),
        ontimeout: () => resolve({ ok: false, error: 'timeout', status: 0 }),
      });
    });
  }

  /* ------------------------------ Facebook DOM ------------------------ */
  const norm = (str) => (str || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
  const onInboxPage = () => location.pathname.startsWith('/marketplace/inbox');

  // innerText that keeps emoji (<img alt>) and line breaks
  function textOf(node) {
    let out = '';
    (function walk(n) {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeType !== 1) return;
      if (n.tagName === 'IMG') { out += n.alt || ''; return; }
      if (n.tagName === 'BR') { out += '\n'; return; }
      n.childNodes.forEach(walk);
    })(node);
    return out.replace(/[\u200e\u200f]/g, '').replace(/\u00a0/g, ' ').replace(/[ \t]+\n/g, '\n').trim();
  }

  function realClick(el) {
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach((type) =>
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true })));
  }

  /* --- step 1: get into the Marketplace-only list --- */
  function marketplaceGrid() {
    for (const g of document.querySelectorAll(CFG.SELECTORS.gridAny)) {
      if (CFG.MARKETPLACE_RE.test(g.getAttribute('aria-label') || '')) return g;
    }
    return null;
  }

  // The "Marketplace" folder row: a row with a button but no thread link, whose first text is "Marketplace"
  function findFolderButton() {
    for (const g of document.querySelectorAll(CFG.SELECTORS.gridAny)) {
      if (g === marketplaceGrid()) continue;
      for (const row of g.querySelectorAll('div[role="row"]')) {
        if (row.querySelector(CFG.SELECTORS.threadLink)) continue;
        const first = [...row.querySelectorAll('span[dir="auto"]')].map(textOf).find(Boolean) || '';
        if (!CFG.MARKETPLACE_RE.test(first)) continue;
        const btn = [...row.querySelectorAll('div[role="button"]')].find((b) => !b.getAttribute('aria-label'));
        if (btn) return btn;
      }
    }
    return null;
  }

  async function ensureMarketplaceView() {
    if (marketplaceGrid() || onInboxPage()) return true;
    const btn = findFolderButton();
    if (!btn) return false;
    realClick(btn);
    return !!(await waitFor(marketplaceGrid, 7000));
  }

  /* --- step 2: rows of the Marketplace list --- */
  function threadRows() {
    const scope = marketplaceGrid() || document;
    const map = new Map();
    scope.querySelectorAll(CFG.SELECTORS.threadLink).forEach((a) => {
      const m = (a.getAttribute('href') || '').match(/\/t\/([^/?#]+)/);
      if (!m || map.has(m[1])) return;
      const spans = [...a.querySelectorAll('span[dir="auto"]')].map(textOf).filter((t) => t && t !== '·');
      const title = spans[0] || '';                    // "Buyer · Listing title"
      const preview = spans[1] || '';                  // "You: ..." when you answered last
      map.set(m[1], {
        id: m[1], el: a, title, preview,
        answered: CFG.PREVIEW_ME_RE.test(preview),
        sig: (title + '|' + preview).replace(/\d+/g, '#').replace(/\s+/g, ' ').slice(0, 300),
      });
    });
    return [...map.values()];
  }

  /* --- step 3: read the open conversation --- */
  function senderOf(el, customerName, logRect) {
    const h4 = el.querySelector('h4');
    let who = h4 ? textOf(h4) : '';
    if (!who) {                                          // aria-label: "At 8:16 AM, You: text"
      const m = (el.getAttribute('aria-label') || '').split('\n')[0].match(/,\s*([^,:]+):\s/);
      who = m ? m[1].trim() : '';
    }
    const nw = norm(who), nc = norm(customerName);
    if (nw && nc && (nw === nc || nc.startsWith(nw) || nw.startsWith(nc))) return 'customer';
    if (who && CFG.OWN_RE.test(who)) return 'me';
    const bubble = el.querySelector('div[dir="auto"]');  // last resort: own bubbles sit on the "end" side
    if (bubble && logRect) {
      const r = bubble.getBoundingClientRect(), cx = r.left + r.width / 2, mid = logRect.left + logRect.width / 2;
      const rtl = getComputedStyle(document.documentElement).direction === 'rtl';
      return (rtl ? cx < mid : cx > mid) ? 'me' : 'customer';
    }
    return 'customer';
  }

  function bodyOf(el) {
    if (el.querySelector(CFG.SELECTORS.buyerProfile)) return null;            // "X started this chat" system line
    const nodes = [...el.querySelectorAll('div[dir="auto"]')].filter((n) => !n.querySelector('div[dir="auto"]'));
    const texts = nodes.map(textOf).filter(Boolean);
    if (texts.length) return texts[texts.length - 1];                          // last = the message (earlier = quoted reply)
    const m = (el.getAttribute('aria-label') || '').match(/,\s*[^,:]+:\s([\s\S]+)/);   // attachments, voice notes...
    return m ? m[1].trim() : null;
  }

  function scrapeOpenThread(row) {
    const log = document.querySelector(CFG.SELECTORS.log);
    if (!log) return null;
    const sepAt = row.title.indexOf(' · ');
    const customerName = (sepAt > 0 ? row.title.slice(0, sepAt) : row.title).trim();
    const itemTitle = sepAt > 0 ? row.title.slice(sepAt + 3).trim() : '';
    const logRect = log.getBoundingClientRect();
    const seen = new Set(), messages = [];
    log.querySelectorAll(CFG.SELECTORS.message).forEach((el) => {
      const uid = (el.getAttribute('data-message-id') || '').slice(0, 80);
      if (!uid || seen.has(uid)) return;
      seen.add(uid);
      const text = bodyOf(el);
      if (!text) return;
      messages.push({ uid, sender: senderOf(el, customerName, logRect), text });
    });
    const prof = log.querySelector(CFG.SELECTORS.buyerProfile);
    const customerId = prof ? ((prof.getAttribute('href') || '').match(/profile\/(\d+)/) || [])[1] || '' : '';
    return {
      customerName, itemTitle, customerId, messages,
      marketplace: !!marketplaceGrid() || !!prof || onInboxPage(),   // came from the Marketplace list, or has the buyer-profile line
    };
  }

  async function openThread(row) {
    realClick(row.el);
    const want = norm(row.title).slice(0, 20);
    const matches = () => {
      const log = document.querySelector(CFG.SELECTORS.log);
      return log && norm(log.getAttribute('aria-label')).includes(want);
    };
    if (!(await waitFor(matches, 7000))) return null;
    let last = -1, stable = 0, snap = null;                        // wait until the message count stops changing
    const end = Date.now() + 5000;
    while (Date.now() < end && stable < 2) {
      await sleep(450);
      snap = scrapeOpenThread(row);
      const n = snap ? snap.messages.length : -1;
      stable = n === last ? stable + 1 : 0; last = n;
    }
    return snap;
  }

  /* ------------------------------ writing replies --------------------- */
  const composer = () => document.querySelector(CFG.SELECTORS.composer);
  const composerEmpty = () => { const c = composer(); return !c || !(c.innerText || '').trim(); };

  async function typeReply(text) {
    const box = composer();
    if (!box) return 'no_composer';
    if (!composerEmpty()) return 'composer_busy';                  // a draft or the user's own typing
    box.focus(); await sleep(250);
    text.split('\n').forEach((line, i) => {
      if (i) document.execCommand('insertLineBreak');
      if (line) document.execCommand('insertText', false, line);
    });
    await sleep(300);
    if (composerEmpty()) {                                          // fallback: simulate a paste (Lexical handles it)
      const dt = new DataTransfer(); dt.setData('text/plain', text);
      box.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
      await sleep(400);
    }
    return composerEmpty() ? 'type_failed' : 'ok';
  }

  async function pressSend() {
    const box = composer();
    if (!box) return false;
    box.focus();
    ['keydown', 'keypress', 'keyup'].forEach((type) =>
      box.dispatchEvent(new KeyboardEvent(type, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true })));
    await sleep(1800);
    if (composerEmpty()) return true;
    for (const sel of CFG.SELECTORS.sendButtons) {                  // fallback: the send button
      const b = document.querySelector(sel);
      if (b) { realClick(b); await sleep(1800); if (composerEmpty()) return true; }
    }
    return false;
  }

  async function answerThread(row, snap) {
    const cfg = state.cfg;
    const g = await api('POST', '/answers/generate', { fb_id: fbId(), thread_id: row.id });
    if (!g.ok) {
      state.failedAt[row.id] = Date.now();
      setStatus(g.error === 'hourly_limit_reached' ? 'hourly reply limit reached' : 'answer failed: ' + (g.error || g.status), 'warn');
      return false;
    }
    if (g.skip || !g.reply) { await api('POST', `/answers/${g.response_id}/result`, { status: 'skipped' }); return true; }
    setStatus(`answer ready for ${snap.customerName} (${g.cached ? 'cache' : 'new'})`, 'info');
    await sleep(rand(cfg.delay_min, cfg.delay_max) * 1000);
    // the buyer may have written again, or the view may have changed, while we waited
    const now = scrapeOpenThread(row);
    const lastNow = now && now.messages[now.messages.length - 1];
    const lastThen = snap.messages[snap.messages.length - 1];
    if (!lastNow || lastNow.sender !== 'customer' || lastNow.uid !== lastThen.uid) return false;
    const typed = await typeReply(g.reply);
    if (typed !== 'ok') {
      state.failedAt[row.id] = Date.now();
      await api('POST', `/answers/${g.response_id}/result`, { status: 'failed', error: typed });
      return false;
    }
    if (cfg.reply_mode === 'auto') {
      await sleep(rand(700, 1600));
      const sent = await pressSend();
      await api('POST', `/answers/${g.response_id}/result`, sent ? { status: 'sent' } : { status: 'failed', error: 'send_not_confirmed' });
      if (!sent) { state.failedAt[row.id] = Date.now(); return false; }
    } else {
      await api('POST', `/answers/${g.response_id}/result`, { status: 'drafted' });
    }
    return true;
  }

  /* ------------------------------ main loop --------------------------- */
  async function processInbox() {
    if (!(await ensureMarketplaceView())) {
      setStatus('Marketplace folder not found in the chat list (check CFG.MARKETPLACE_RE)', 'warn');
      return;
    }
    const rows = threadRows().slice(0, CFG.MAX_ROWS_SCANNED);
    if (!rows.length) { setStatus('Marketplace list is empty or still loading', 'info'); return; }
    const todo = rows
      .filter((r) => !r.answered)                                       // preview "You: ..." -> you already replied
      .filter((r) => state.sigs[r.id] !== r.sig)                        // nothing new since last time
      .filter((r) => !(state.failedAt[r.id] && Date.now() - state.failedAt[r.id] < CFG.FAILED_RETRY_MS))
      .slice(0, CFG.MAX_THREADS_PER_CYCLE);
    let synced = 0, answered = 0, skipped = 0;
    for (const t of todo) {
      const row = threadRows().find((r) => r.id === t.id);               // the list re-renders: fetch a live element
      if (!row) continue;
      const snap = await openThread(row);
      if (!snap || !snap.messages.length) { state.failedAt[t.id] = Date.now(); continue; }
      const last = snap.messages[snap.messages.length - 1];
      if (!snap.marketplace || !CFG.shouldHandle(snap)) { skipped++; state.sigs[t.id] = t.sig; continue; }
      if (last.sender !== 'customer') { state.sigs[t.id] = t.sig; continue; }   // last message is yours: nothing to do
      const res = await api('POST', '/agent/sync', {
        fb_id: fbId(),
        conversations: [{ thread_id: t.id, customer_name: snap.customerName, customer_id: snap.customerId,
          url: `https://www.facebook.com/messages/t/${t.id}/`, item_title: snap.itemTitle, messages: snap.messages }],
      });
      if (!res.ok) { setStatus('sync failed: ' + (res.error || res.status), 'error'); return; }
      if (res.stored === false) { setStatus('collecting is off for this account (Message workers)', 'info'); return; }
      synced++;
      const r = (res.results || [])[0];
      let done = true;
      if (r && r.needs_reply && state.cfg.reply_mode !== 'off') { done = await answerThread(row, snap); if (done) answered++; }
      if (done) state.sigs[t.id] = t.sig;
    }
    GM_setValue('sigs', state.sigs);
    setStatus(`ok · ${rows.length} chats, ${synced} unanswered synced, ${answered} answered (${state.cfg.reply_mode})`, 'ok');
  }

  async function cycle() {
    if (!server() || !apiKey()) { setStatus('not configured - Tampermonkey menu → Configure server', 'warn'); return; }
    if (!fbId()) { setStatus('Facebook user id not found (are you logged in?)', 'error'); return; }
    const reg = await api('POST', '/agent/register', { fb_id: fbId(), tab_id: TAB_ID, version: VERSION, page: location.pathname });
    if (!reg.ok) { setStatus('server: ' + (reg.error || reg.status), 'error'); return; }
    state.cfg = reg.config;
    if (!state.cfg.enabled) { setStatus('registered - enable this account in the dashboard (Message workers)', 'info'); return; }
    if (!state.cfg.collect && state.cfg.reply_mode === 'off') { setStatus('enabled, but collecting and replies are off', 'info'); return; }
    await processInbox();
  }

  async function loop() {
    if (state.running) return;
    state.running = true;
    await sleep(4000); // let Facebook finish rendering
    for (;;) {
      try { await cycle(); } catch (e) { console.error('[MarketplaceHub]', e); setStatus('error: ' + e.message, 'error'); }
      await sleep(Math.max(20, (state.cfg && state.cfg.poll_interval) || 60) * 1000 * rand(0.9, 1.15));
    }
  }

  /* ------------------------------ menu commands ----------------------- */
  GM_registerMenuCommand('Configure server', () => {
    const url = prompt('Marketplace Hub server URL (e.g. https://hub.example.com):', server());
    if (url === null) return;
    const key = prompt('Script key (Settings → Script connection):', apiKey());
    if (key === null) return;
    GM_setValue('server_url', url.trim()); GM_setValue('api_key', key.trim());
    location.reload();
  });
  GM_registerMenuCommand('Test scrape (see console)', () => {
    const inMp = !!marketplaceGrid(), folder = !!findFolderButton(), rows = threadRows();
    console.table(rows.map((r) => ({ id: r.id, title: r.title, preview: r.preview, answeredByMe: r.answered })));
    const log = document.querySelector(CFG.SELECTORS.log);
    let snap = null;
    if (log) {
      const active = rows.find((r) => norm(log.getAttribute('aria-label')).includes(norm(r.title).slice(0, 20)));
      snap = scrapeOpenThread(active || { title: (log.getAttribute('aria-label') || '').replace(/^.*?titled\s+/i, '') });
    }
    console.log('[MarketplaceHub] open conversation:', snap);
    if (snap) console.table(snap.messages.map((m) => ({ sender: m.sender, text: m.text.slice(0, 80) })));
    alert(`Marketplace list open: ${inMp}\nMarketplace folder row found: ${folder}\nChats in list: ${rows.length} (answered by you: ${rows.filter((r) => r.answered).length})\n` +
      `Open conversation: ${snap ? snap.messages.length + ' message(s), last from ' + (snap.messages.length ? snap.messages[snap.messages.length - 1].sender : '-') + ', marketplace=' + snap.marketplace : 'none'}\nDetails in the console (F12).`);
  });
  GM_registerMenuCommand('Toggle debug logging', () => { GM_setValue('debug', !GM_getValue('debug', false)); location.reload(); });
  GM_registerMenuCommand('Forget seen conversations', () => { GM_setValue('sigs', {}); state.sigs = {}; setStatus('memory cleared', 'info'); });

  setStatus('starting…');
  loop();
})();
