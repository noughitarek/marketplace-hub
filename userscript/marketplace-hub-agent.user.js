// ==UserScript==
// @name         Marketplace Hub Agent
// @namespace    marketplace-hub
// @version      1.0.0
// @description  Collects unanswered Facebook Marketplace conversations, sends them to your Marketplace Hub server and (optionally) types/sends the AI-generated answers.
// @match        https://www.facebook.com/marketplace/inbox*
// @match        https://www.facebook.com/messages/*
// @match        https://web.facebook.com/messages/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      *
// @noframes
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  const VERSION = "1.0.0";

  /* ------------------------------------------------------------------ *
   *  SETTINGS YOU MAY NEED TO TUNE
   *  Facebook changes its markup often. If conversations are not found,
   *  use the menu command "Test scrape" and adjust the selectors below.
   * ------------------------------------------------------------------ */
  const CFG = {
    MAX_ROWS_SCANNED: 30, // newest N Marketplace chats looked at per cycle
    MAX_THREADS_PER_CYCLE: 8, // chats actually opened per cycle
    FAILED_RETRY_MS: 1 * 1000, // retry failed threads after 15s (no 10min block)
    // After clicking the "Marketplace" folder, wait this long (ms) for Facebook to load the Marketplace chat list.
    AFTER_FOLDER_CLICK_MS: 3500,
    // Then keep waiting up to this long (ms) until at least one chat row appears.
    LIST_LOAD_TIMEOUT_MS: 10000,
    // Facebook shows "Marketplace" as a folder row in the chat list; clicking it opens the Marketplace-only list.
    // Add your language here if your Facebook is not English/French/Arabic.
    MARKETPLACE_RE: /marketplace|marché|ماركت|السوق/i,
    // "You: ..." preview = you already answered. Add translations if needed.
    PREVIEW_ME_RE: /^(you|vous|tu|usted|أنت)\s*(:|sent\b)/i,
    // Heading above your own messages ("You sent"). Anything else is the buyer.
    OWN_RE: /^(you|vous|tu|usted)(\s|$)|^أنت(\s|$)/i,
    SELECTORS: {
      gridAny: 'div[role="grid"]',
      threadLink: 'a[href*="/t/"]',
      log: 'div[role="log"]', // the open conversation
      message: "[data-message-id]",
      composer: 'div[role="textbox"][data-lexical-editor="true"]',
      sendButtons: [
        '[aria-label="Press enter to send"]',
        '[aria-label="Press Enter to send"]',
        '[aria-label="Send"]',
      ],
      buyerProfile: 'a[href*="/marketplace/profile/"]',
      itemBanner: 'a[href*="/marketplace/item/"]', // listing banner shown at the top of a Marketplace chat
    },
    // Return false to skip a conversation (e.g. where you are the buyer). Default: answer every Marketplace chat.
    shouldHandle: (/* ctx: {customerName, itemTitle} */) => true,
    DEBUG: false,
  };

  /* ------------------------------ helpers ----------------------------- */
  const TAB_ID =
    Date.now().toString(36) + Math.random().toString(36).slice(2, 6); // only used to label this tab
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);
  const log = (...a) => CFG.DEBUG && console.log("[MarketplaceHub]", ...a);
  const server = () =>
    (GM_getValue("server_url", "") || "").replace(/\/+$/, "");
  const apiKey = () => GM_getValue("api_key", "");
  const fbId = () =>
    (document.cookie.match(/(?:^|;\s*)c_user=(\d+)/) || [])[1] || "";
  const state = {
    cfg: null,
    running: false,
    note: "",
    failedAt: {},
    sigs: GM_getValue("sigs", {}),
  };
  CFG.DEBUG = !!GM_getValue("debug", false);

  // Memory of chats already handled: { sig, at, final }
  const isFresh = (id, sig) => {
    const e = state.sigs[id];
    if (!e || typeof e !== "object" || e.sig !== sig) return false;
    return !!e.final;
  };
  const remember = (id, sig, final, note) => {
    state.sigs[id] = {
      sig,
      at: Date.now(),
      final: !!final,
      note: (note || "").slice(0, 300),
    };
  };

  function hash(str) {
    // cyrb53, enough for message ids
    let h1 = 0xdeadbeef,
      h2 = 0x41c6ce57;
    for (let i = 0; i < str.length; i++) {
      const c = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 2654435761);
      h2 = Math.imul(h2 ^ c, 1597334677);
    }
    h1 =
      Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^
      Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 =
      Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^
      Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
  }

  async function waitFor(fn, timeout = 4000, step = 150) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const v = fn();
      if (v) return v;
      await sleep(step);
    }
    return null;
  }

  /* ------------------------------ status badge ------------------------ */
  const badge = document.createElement("div");
  badge.style.cssText =
    "position:fixed;left:12px;bottom:12px;z-index:2147483647;max-width:320px;padding:6px 10px;border-radius:8px;font:12px/1.35 system-ui,sans-serif;color:#fff;background:#445;box-shadow:0 2px 8px #0005;pointer-events:none";
  document.documentElement.appendChild(badge);
  function setStatus(text, level = "info") {
    badge.textContent = "Hub: " + text;
    badge.style.background =
      { ok: "#1f7a4d", info: "#35506b", warn: "#b7791f", error: "#b3261e" }[
        level
      ] || "#445";
    log(level, text);
  }

  /* ------------------------------ server API -------------------------- */
  function api(method, path, body) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method,
        url: server() + "/api/v1" + path,
        timeout: 70000,
        headers: { "Content-Type": "application/json", "X-API-Key": apiKey() },
        data: body ? JSON.stringify(body) : undefined,
        onload: (r) => {
          let j = {};
          try {
            j = JSON.parse(r.responseText);
          } catch (e) {
            j = { error: "bad_response" };
          }
          j.status = r.status;
          if (r.status >= 400) j.ok = false;
          resolve(j);
        },
        onerror: () =>
          resolve({ ok: false, error: "network_error", status: 0 }),
        ontimeout: () => resolve({ ok: false, error: "timeout", status: 0 }),
      });
    });
  }

  function fetchBlob(url) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "GET",
        url,
        responseType: "blob",
        timeout: 30000,
        headers: { "X-API-Key": apiKey() },
        onload: (r) => {
          if (r.status >= 200 && r.status < 300 && r.response) {
            resolve(r.response);
          } else {
            resolve(null);
          }
        },
        onerror: () => resolve(null),
        ontimeout: () => resolve(null),
      });
    });
  }

  const mediaCache = new Map();
  function fetchBase64(url) {
    if (!url) return Promise.resolve(null);
    if (mediaCache.has(url)) return Promise.resolve(mediaCache.get(url));
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "GET",
        url,
        responseType: "blob",
        timeout: 25000,
        onload: (r) => {
          if (r.status >= 200 && r.status < 300 && r.response) {
            const reader = new FileReader();
            reader.onloadend = () => {
              const res = { data: reader.result, mime: r.response.type || "" };
              mediaCache.set(url, res);
              resolve(res);
            };
            reader.onerror = () => resolve(null);
            reader.readAsDataURL(r.response);
          } else {
            resolve(null);
          }
        },
        onerror: () => resolve(null),
        ontimeout: () => resolve(null),
      });
    });
  }

  /* ------------------------------ Facebook DOM ------------------------ */
  const norm = (str) =>
    (str || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  const onInboxPage = () => location.pathname.startsWith("/marketplace/inbox");

  // innerText that keeps emoji (<img alt>) and line breaks
  function textOf(node) {
    let out = "";
    (function walk(n) {
      if (n.nodeType === 3) {
        out += n.nodeValue;
        return;
      }
      if (n.nodeType !== 1) return;
      if (n.tagName === "IMG") {
        out += n.alt || "";
        return;
      }
      if (n.tagName === "BR") {
        out += "\n";
        return;
      }
      n.childNodes.forEach(walk);
    })(node);
    return out
      .replace(/[\u200e\u200f]/g, "")
      .replace(/\u00a0/g, " ")
      .replace(/[ \t]+\n/g, "\n")
      .trim();
  }

  function realClick(el) {
    ["pointerdown", "mousedown", "pointerup", "mouseup", "click"].forEach(
      (type) =>
        el.dispatchEvent(
          new MouseEvent(type, { bubbles: true, cancelable: true }),
        ),
    );
  }

  /* --- Voice Message & Image extraction helpers --- */
  function injectMainWorldScript() {
    if (document.getElementById("mh-audio-hook")) return;
    try {
      const nonce =
        document.querySelector("script[nonce]")?.getAttribute("nonce") || "";
      const s = document.createElement("script");
      s.id = "mh-audio-hook";
      if (nonce) s.setAttribute("nonce", nonce);
      s.textContent = `
(function() {
  window.__MH_AUDIO_CACHE = window.__MH_AUDIO_CACHE || new Map();
  window.__MH_LAST_AUDIO = null;
  window.__MH_SILENT_CAPTURE = false;

  const origAudio = window.Audio;
  if (origAudio) {
    window.Audio = function(src) {
      const a = new origAudio(src);
      if (src && typeof src === "string") window.__MH_LAST_AUDIO = src;
      return a;
    };
    window.Audio.prototype = origAudio.prototype;
  }

  if (window.HTMLAudioElement && HTMLAudioElement.prototype) {
    const origPlay = HTMLAudioElement.prototype.play;
    HTMLAudioElement.prototype.play = function() {
      const src = this.currentSrc || this.src;
      if (src) window.__MH_LAST_AUDIO = src;
      if (window.__MH_SILENT_CAPTURE) {
        try { this.muted = true; this.pause(); } catch(e){}
        return Promise.resolve();
      }
      return origPlay.apply(this, arguments);
    };
  }

  function isLikelyAudio(str) {
    if (typeof str !== "string" || !str) return false;
    const s = str.toLowerCase();
    if (!s.startsWith("http://") && !s.startsWith("https://") && !s.startsWith("blob:")) return false;
    if (s.includes("audioclip")) return true;
    if (/\\.(mp4|m4a|aac|mp3|ogg)(\\?|$)/.test(s)) return true;
    if (s.includes("fbsbx.com") && (s.includes("audio") || s.includes("/v/t") || s.includes("bytestream"))) return true;
    if (s.includes("fbcdn.net") && (s.includes("audio") || s.includes("audioclip"))) return true;
    return false;
  }

  function crawl(obj, visited = new Set(), depth = 0) {
    if (!obj || depth > 8 || visited.has(obj)) return null;
    if (typeof obj === "string") return isLikelyAudio(obj) ? obj : null;
    if (typeof obj !== "object" && typeof obj !== "function") return null;
    visited.add(obj);

    const keys = ["playable_url", "playableUrl", "playable_url_quality_hd", "audio_src", "audioSrc", "audio_url", "audioUrl", "src", "uri", "url"];
    for (const k of keys) {
      try {
        const v = obj[k];
        if (typeof v === "string" && isLikelyAudio(v)) return v;
      } catch(e) {}
    }

    if (obj.memoizedProps) {
      const r = crawl(obj.memoizedProps, visited, depth + 1);
      if (r) return r;
    }
    if (obj.pendingProps) {
      const r = crawl(obj.pendingProps, visited, depth + 1);
      if (r) return r;
    }
    if (obj.stateNode && typeof obj.stateNode === "object") {
      const r = crawl(obj.stateNode, visited, depth + 1);
      if (r) return r;
    }
    if (obj.memoizedState) {
      const r = crawl(obj.memoizedState, visited, depth + 1);
      if (r) return r;
    }

    try {
      for (const k of Object.keys(obj)) {
        if (k === "stateNode" || k === "window" || k === "document" || k === "children") continue;
        const v = obj[k];
        if (typeof v === "string" && isLikelyAudio(v)) return v;
        if (typeof v === "object" && v !== null && depth < 5) {
          const r = crawl(v, visited, depth + 1);
          if (r) return r;
        }
      }
    } catch(e) {}
    return null;
  }

  window.__MH_GET_AUDIO = function(msgId) {
    if (!msgId) return null;
    if (window.__MH_AUDIO_CACHE.has(msgId)) return window.__MH_AUDIO_CACHE.get(msgId);
    const msgEl = document.querySelector('[data-message-id="' + msgId + '"]');
    if (!msgEl) return null;

    const aud = msgEl.querySelector("audio");
    if (aud && (aud.currentSrc || aud.src)) {
      const src = aud.currentSrc || aud.src;
      window.__MH_AUDIO_CACHE.set(msgId, src);
      return src;
    }
    const alink = msgEl.querySelector('a[href*=".mp4"], a[href*=".m4a"], a[href*=".aac"], a[href*="audioclip"]');
    if (alink && alink.href) {
      window.__MH_AUDIO_CACHE.set(msgId, alink.href);
      return alink.href;
    }

    const targets = [
      msgEl.querySelector('[aria-label="Play"], [aria-label="Pause"], [aria-label*="Audio scrubber"], [role="slider"], [role="timer"]'),
      msgEl
    ].filter(Boolean);

    for (const t of targets) {
      for (const prop in t) {
        if (prop.startsWith("__reactFiber") || prop.startsWith("__reactProps") || prop.startsWith("__reactInternalInstance")) {
          let curr = t[prop];
          let steps = 0;
          while (curr && steps < 15) {
            const found = crawl(curr.memoizedProps) || crawl(curr.memoizedState);
            if (found) {
              window.__MH_AUDIO_CACHE.set(msgId, found);
              return found;
            }
            if (curr.child) {
              const cf = crawl(curr.child.memoizedProps);
              if (cf) {
                window.__MH_AUDIO_CACHE.set(msgId, cf);
                return cf;
              }
            }
            curr = curr.return;
            steps++;
          }
        }
      }
    }
    return null;
  };

  window.__MH_TRIGGER_AUDIO = async function(msgId) {
    const msgEl = document.querySelector('[data-message-id="' + msgId + '"]');
    if (!msgEl) return null;
    const playBtn = msgEl.querySelector('[aria-label="Play"], div[role="button"][aria-label*="Play"]');
    if (!playBtn) return null;

    window.__MH_SILENT_CAPTURE = true;
    window.__MH_LAST_AUDIO = null;
    try {
      ["pointerdown", "mousedown", "pointerup", "mouseup", "click"].forEach(type =>
        playBtn.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }))
      );
      await new Promise(r => setTimeout(r, 250));
    } finally {
      window.__MH_SILENT_CAPTURE = false;
    }

    const audios = document.querySelectorAll("audio");
    for (const a of audios) {
      try { a.muted = true; a.pause(); } catch(e){}
    }

    const captured = window.__MH_LAST_AUDIO || window.__MH_GET_AUDIO(msgId);
    if (captured) window.__MH_AUDIO_CACHE.set(msgId, captured);
    return captured;
  };
})();
`;
      (document.head || document.documentElement).appendChild(s);
    } catch (e) {}
  }

  function isVoiceMessageElement(el) {
    if (!el) return false;
    if (el.querySelector("audio")) return true;
    if (
      el.querySelector(
        '[aria-label="Play"], [aria-label="Pause"], [aria-label*="Audio scrubber"], [role="slider"][aria-label*="Audio"], [role="timer"]',
      )
    )
      return true;
    if (el.querySelector('svg path[d*="M10 25.5v-15"]')) return true;
    return false;
  }

  function extractChatImage(el, customerName) {
    if (!el) return null;

    // 1. Messenger media link wrapper (<a href="/messenger_media/..." or attachment_id)
    const mediaLink = el.querySelector(
      'a[href*="/messenger_media/"], a[href*="attachment_id="], a[aria-label*="photo" i], a[aria-label*="image" i]',
    );
    if (mediaLink) {
      const img = mediaLink.querySelector("img");
      if (img && img.src && !img.src.includes("/emoji.php")) {
        return img.src;
      }
    }

    // 2. Explicit photo attachment images (alt starts with "Open photo" or "Photo")
    const photoImgs = el.querySelectorAll(
      'img[alt^="Open photo" i], img[alt^="Photo" i], img[alt^="Image" i]',
    );
    for (const img of photoImgs) {
      if (img.src && !img.src.includes("/emoji.php")) {
        return img.src;
      }
    }

    // 3. Fallback: check all images, strictly rejecting avatars, icons, receipts
    const allImgs = el.querySelectorAll("img");
    const cName = (customerName || "").trim().toLowerCase();
    for (const img of allImgs) {
      const src = img.src || "";
      const alt = (img.alt || "").trim().toLowerCase();
      const style = (img.getAttribute("style") || "").toLowerCase();

      // Skip emojis, icons, loading gifs
      if (!src || src.includes("/emoji.php") || src.includes("rsrc.php"))
        continue;

      // Skip profile pictures / avatars (FB uses /t39.30808-1/ and circular border-radius)
      if (
        src.includes("/t39.30808-1/") ||
        src.includes("dst-jpg_s100x100") ||
        src.includes("dst-jpg_s50x50") ||
        src.includes("dst-jpg_s60x60") ||
        src.includes("dst-jpg_s75x75") ||
        src.includes("dst-jpg_s32x32")
      )
        continue;
      if (
        style.includes("border-radius: 50%") ||
        style.includes("border-radius:50%") ||
        style.includes("border-radius: 9999px")
      )
        continue;
      if (alt.startsWith("seen by") || (cName && alt === cName)) continue;
      if (
        img.closest(
          '.xgd8bvy, [aria-label*="seen by" i], [aria-label*="profile" i]',
        )
      )
        continue;

      const w = img.naturalWidth || img.width || 0;
      if (w > 0 && w <= 50 && !src.includes("/t1.15752-")) continue;

      return src;
    }

    return null;
  }

  /* --- step 1: get into the Marketplace-only list --- */
  function marketplaceGrid() {
    for (const g of document.querySelectorAll(CFG.SELECTORS.gridAny)) {
      if (CFG.MARKETPLACE_RE.test(g.getAttribute("aria-label") || "")) return g;
    }
    return null;
  }

  // The "Marketplace" folder row: a row with a button but no thread link, whose first text is "Marketplace"
  function findFolderButton() {
    for (const g of document.querySelectorAll(CFG.SELECTORS.gridAny)) {
      if (g === marketplaceGrid()) continue;
      for (const row of g.querySelectorAll('div[role="row"]')) {
        if (row.querySelector(CFG.SELECTORS.threadLink)) continue;
        const first =
          [...row.querySelectorAll('span[dir="auto"]')]
            .map(textOf)
            .find(Boolean) || "";
        if (!CFG.MARKETPLACE_RE.test(first)) continue;
        const btn = [...row.querySelectorAll('div[role="button"]')].find(
          (b) => !b.getAttribute("aria-label"),
        );
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
    if (!(await waitFor(marketplaceGrid, 7000))) return false;
    setStatus("Marketplace opened, waiting for the chat list to load…", "info");
    await sleep(CFG.AFTER_FOLDER_CLICK_MS + rand(0, 800)); // let Facebook render the list
    await waitFor(() => threadRows().length > 0, CFG.LIST_LOAD_TIMEOUT_MS, 300); // rows may still be loading
    return true;
  }

  /* --- step 2: rows of the Marketplace list --- */
  function threadRows() {
    const scope = marketplaceGrid() || document;
    const map = new Map();
    scope.querySelectorAll(CFG.SELECTORS.threadLink).forEach((a) => {
      const m = (a.getAttribute("href") || "").match(/\/t\/([^/?#]+)/);
      if (!m || map.has(m[1])) return;
      const spans = [...a.querySelectorAll('span[dir="auto"]')]
        .map(textOf)
        .filter((t) => t && t !== "·");
      const title = spans[0] || ""; // "Buyer · Listing title"
      const preview = spans[1] || ""; // "You: ..." when you answered last
      map.set(m[1], {
        id: m[1],
        el: a,
        title,
        preview,
        answered: CFG.PREVIEW_ME_RE.test(preview),
        sig: (title + "|" + preview)
          .replace(/\d+/g, "#")
          .replace(/\s+/g, " ")
          .slice(0, 300),
      });
    });
    return [...map.values()];
  }

  /* --- step 3: read the open conversation --- */
  function senderOf(el, customerName, logRect) {
    const h4 = el.querySelector("h4");
    let who = h4 ? textOf(h4) : "";
    if (!who) {
      // aria-label: "At 8:16 AM, You: text"
      const m = (el.getAttribute("aria-label") || "")
        .split("\n")[0]
        .match(/,\s*([^,:]+):\s/);
      who = m ? m[1].trim() : "";
    }
    const nw = norm(who),
      nc = norm(customerName);
    if (nw && nc && (nw === nc || nc.startsWith(nw) || nw.startsWith(nc)))
      return "customer";
    if (who && CFG.OWN_RE.test(who)) return "me";
    const bubble = el.querySelector('div[dir="auto"]'); // last resort: own bubbles sit on the "end" side
    if (bubble && logRect) {
      const r = bubble.getBoundingClientRect(),
        cx = r.left + r.width / 2,
        mid = logRect.left + logRect.width / 2;
      const rtl =
        getComputedStyle(document.documentElement).direction === "rtl";
      return (rtl ? cx < mid : cx > mid) ? "me" : "customer";
    }
    return "customer";
  }

  function bodyOf(el) {
    if (el.querySelector(CFG.SELECTORS.buyerProfile)) return null; // "X started this chat" system line
    const nodes = [...el.querySelectorAll('div[dir="auto"]')].filter(
      (n) => !n.querySelector('div[dir="auto"]'),
    );
    const texts = nodes.map(textOf).filter(Boolean);
    if (texts.length) return texts[texts.length - 1]; // last = the message (earlier = quoted reply)
    const m = (el.getAttribute("aria-label") || "").match(
      /,\s*[^,:]+:\s([\s\S]+)/,
    ); // attachments, voice notes...
    return m ? m[1].trim() : null;
  }

  async function scrapeOpenThread(row) {
    const log = document.querySelector(CFG.SELECTORS.log);
    if (!log) return null;
    const sepAt = row.title.indexOf(" · ");
    const customerName = (
      sepAt > 0 ? row.title.slice(0, sepAt) : row.title
    ).trim();
    const itemTitle = sepAt > 0 ? row.title.slice(sepAt + 3).trim() : "";
    const logRect = log.getBoundingClientRect();
    const seen = new Set(),
      messages = [];
    const msgEls = [...log.querySelectorAll(CFG.SELECTORS.message)];

    for (const el of msgEls) {
      const uid = (el.getAttribute("data-message-id") || "").slice(0, 80);
      if (!uid || seen.has(uid)) continue;
      seen.add(uid);

      const text = bodyOf(el);
      const sender = senderOf(el, customerName, logRect);
      const isVoice = isVoiceMessageElement(el);

      // 1. Detect Audio / Voice Message
      let audioSrc = null;
      if (isVoice) {
        if (
          typeof unsafeWindow !== "undefined" &&
          unsafeWindow.__MH_GET_AUDIO
        ) {
          try {
            audioSrc = unsafeWindow.__MH_GET_AUDIO(uid);
          } catch (e) {}
        }
        if (
          !audioSrc &&
          typeof unsafeWindow !== "undefined" &&
          unsafeWindow.__MH_TRIGGER_AUDIO
        ) {
          try {
            audioSrc = await unsafeWindow.__MH_TRIGGER_AUDIO(uid);
          } catch (e) {}
        }
        if (!audioSrc) {
          const audioEl = el.querySelector(
            "audio, [data-testid*='audio'] audio",
          );
          if (audioEl) audioSrc = audioEl.currentSrc || audioEl.src;
        }
        if (!audioSrc) {
          const audioLink = el.querySelector(
            "a[href*='.mp4'], a[href*='.m4a'], a[href*='.ogg'], a[href*='.aac'], a[href*='.mp3'], a[href*='audioclip']",
          );
          if (audioLink) audioSrc = audioLink.href;
        }
      }

      // 2. Detect Image (strictly excluding avatars)
      const imageSrc = extractChatImage(el, customerName);

      if (!text && !audioSrc && !imageSrc && !isVoice) continue;

      let audioData = null,
        audioMime = null;
      if (audioSrc && sender === "customer") {
        const res = await fetchBase64(audioSrc);
        if (res) {
          audioData = res.data;
          audioMime = res.mime || "audio/mp4";
        }
      }

      let imageData = null,
        imageMime = null;
      if (imageSrc && sender === "customer") {
        const res = await fetchBase64(imageSrc);
        if (res) {
          imageData = res.data;
          imageMime = res.mime || "image/jpeg";
        }
      }

      const finalText =
        text ||
        (isVoice || audioSrc ? "(Voice message)" : imageSrc ? "(Image)" : "");

      messages.push({
        uid,
        sender,
        text: finalText,
        audio_data: audioData,
        audio_mime: audioMime,
        audio_url: audioSrc,
        image_data: imageData,
        image_mime: imageMime,
        image_url: imageSrc,
      });
    }

    const prof = log.querySelector(CFG.SELECTORS.buyerProfile);
    const customerId = prof
      ? ((prof.getAttribute("href") || "").match(/profile\/(\d+)/) || [])[1] ||
        ""
      : "";
    // The listing banner sits above the conversation (outside role="log"), so search the whole page.
    const banner = document.querySelector(CFG.SELECTORS.itemBanner);
    return {
      customerName,
      itemTitle,
      customerId,
      messages,
      // came from the Marketplace list, has the buyer-profile line, or shows the Marketplace listing banner
      marketplace: !!marketplaceGrid() || !!prof || !!banner || onInboxPage(),
    };
  }

  async function openThread(row) {
    realClick(row.el);
    const want = norm(row.title).slice(0, 20);
    const matches = () => {
      const log = document.querySelector(CFG.SELECTORS.log);
      return log && norm(log.getAttribute("aria-label")).includes(want);
    };
    if (!(await waitFor(matches, 7000))) return null;
    let last = -1,
      stable = 0,
      snap = null; // wait until the message count stops changing
    const end = Date.now() + 5000;
    while (Date.now() < end && stable < 2) {
      await sleep(450);
      snap = await scrapeOpenThread(row);
      const n = snap ? snap.messages.length : -1;
      stable = n === last ? stable + 1 : 0;
      last = n;
    }
    return snap;
  }

  /* ------------------------------ writing replies --------------------- */
  const composer = () => document.querySelector(CFG.SELECTORS.composer);
  const hasComposerAttachment = () => {
    return !!document.querySelector(
      'div[aria-label*="Remove" i], div[aria-label*="remove" i], button[aria-label*="remove" i], [aria-label*="Supprimer" i], [aria-label*="إزالة" i], div[role="presentation"] img[src^="blob:"], [data-testid*="media_preview"]',
    );
  };
  const composerEmpty = () => {
    const c = composer();
    return !c || (!(c.innerText || "").trim() && !hasComposerAttachment());
  };

  async function attachImageToComposer(blob, filename) {
    const box = composer();
    if (!box) return false;
    const file = new File([blob], filename || "image.jpg", {
      type: blob.type || "image/jpeg",
    });
    let attached = false;

    // 1. Try file input element first
    const fileInputs = document.querySelectorAll(
      'input[type="file"][accept*="image"], input[type="file"]',
    );
    for (const inp of fileInputs) {
      try {
        const dt = new DataTransfer();
        dt.items.add(file);
        inp.files = dt.files;
        inp.dispatchEvent(new Event("change", { bubbles: true }));
        attached = true;
        break;
      } catch (e) {
        log("fileInput dispatch error", e);
      }
    }

    if (attached) {
      // Allow Facebook a moment to process the file input change event
      await sleep(800);
    }

    // 2. Only fallback to clipboard paste if file input was missing or failed to show preview
    if (!attached || !hasComposerAttachment()) {
      try {
        const dt = new DataTransfer();
        dt.items.add(file);
        box.focus();
        const pasteEv = new ClipboardEvent("paste", {
          clipboardData: dt,
          bubbles: true,
          cancelable: true,
        });
        box.dispatchEvent(pasteEv);
        attached = true;
      } catch (e) {
        log("paste dispatch error", e);
      }
      await sleep(800);
    }

    await sleep(1000);
    return attached;
  }

  async function typeReply(text) {
    const box = composer();
    if (!box) return "no_composer";
    if (!composerEmpty()) return "composer_busy"; // a draft or the user's own typing
    box.focus();
    await sleep(250);
    text.split("\n").forEach((line, i) => {
      if (i) document.execCommand("insertLineBreak");
      if (line) document.execCommand("insertText", false, line);
    });
    await sleep(300);
    if (composerEmpty()) {
      // fallback: simulate a paste (Lexical handles it)
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      box.dispatchEvent(
        new ClipboardEvent("paste", {
          clipboardData: dt,
          bubbles: true,
          cancelable: true,
        }),
      );
      await sleep(400);
    }
    return composerEmpty() ? "type_failed" : "ok";
  }

  async function pressSend() {
    const box = composer();
    if (!box) return false;
    box.focus();
    ["keydown", "keypress", "keyup"].forEach((type) =>
      box.dispatchEvent(
        new KeyboardEvent(type, {
          key: "Enter",
          code: "Enter",
          keyCode: 13,
          which: 13,
          bubbles: true,
          cancelable: true,
        }),
      ),
    );
    await sleep(1800);
    if (composerEmpty()) return true;
    for (const sel of CFG.SELECTORS.sendButtons) {
      // fallback: the send button
      const b = document.querySelector(sel);
      if (b) {
        realClick(b);
        await sleep(1800);
        if (composerEmpty()) return true;
      }
    }
    return false;
  }

  async function answerThread(row, snap) {
    const cfg = state.cfg;
    const g = await api("POST", "/answers/generate", {
      fb_id: fbId(),
      thread_id: row.id,
    });
    if (!g.ok) {
      state.failedAt[row.id] = Date.now();
      state.note = "answer generation failed: " + (g.error || g.status);
      setStatus(
        g.error === "hourly_limit_reached"
          ? "hourly reply limit reached"
          : "answer failed: " + (g.error || g.status),
        "warn",
      );
      return false;
    }
    const seq =
      g.sequence && g.sequence.length
        ? g.sequence
        : g.reply
          ? [{ type: "text", text: g.reply }]
          : [];
    if (g.skip || !seq.length) {
      await api("POST", `/answers/${g.response_id}/result`, {
        status: "skipped",
      });
      return "skipped";
    }
    setStatus(
      `answer ready for ${snap.customerName} (${g.cached ? "cache" : "new"})`,
      "info",
    );
    // Wait configured delay before sending
    await sleep(rand(cfg.delay_min, cfg.delay_max) * 1000);

    const now = await scrapeOpenThread(row);
    const lastNow = now && now.messages[now.messages.length - 1];
    if (!lastNow || lastNow.sender !== "customer") {
      state.note =
        "the last message is no longer from the buyer when the reply was ready";
      return false;
    }

    let allSent = true;
    for (let i = 0; i < seq.length; i++) {
      const item = seq[i];
      if (item.type === "image") {
        setStatus(`Sending image ${item.keyword || item.filename}…`, "info");
        const blob = await fetchBlob(server() + item.url);
        if (blob) {
          await attachImageToComposer(blob, item.filename);
          if (cfg.reply_mode === "auto") {
            await sleep(rand(1200, 2000));
            const sent = await pressSend();
            if (!sent) {
              allSent = false;
              break;
            }
            await sleep(rand(1000, 2000));
          }
        } else {
          log("failed to load image blob", item.url);
        }
      } else if (item.type === "text" && item.text && item.text.trim()) {
        setStatus(`Typing reply (${i + 1}/${seq.length})…`, "info");
        const typed = await typeReply(item.text.trim());
        if (typed !== "ok") {
          state.failedAt[row.id] = Date.now();
          state.note = "typing reply failed: " + typed;
          await api("POST", `/answers/${g.response_id}/result`, {
            status: "failed",
            error: typed,
          });
          return false;
        }
        if (cfg.reply_mode === "auto") {
          await sleep(rand(700, 1600));
          const sent = await pressSend();
          if (!sent) {
            allSent = false;
            state.failedAt[row.id] = Date.now();
            state.note = "reply typed but sending was not confirmed";
            await api("POST", `/answers/${g.response_id}/result`, {
              status: "failed",
              error: "send_not_confirmed",
            });
            return false;
          }
          await sleep(rand(1000, 2000));
        }
      }
    }

    if (cfg.reply_mode === "auto") {
      await api("POST", `/answers/${g.response_id}/result`, {
        status: allSent ? "sent" : "failed",
        error: allSent ? null : "send_part_failed",
      });
      return allSent ? "replied" : false;
    } else {
      await api("POST", `/answers/${g.response_id}/result`, {
        status: "drafted",
      });
      return "replied";
    }
  }

  /* ------------------------------ main loop --------------------------- */
  async function processInbox() {
    if (!(await ensureMarketplaceView())) {
      setStatus(
        "Marketplace folder not found in the chat list (check CFG.MARKETPLACE_RE)",
        "warn",
      );
      return;
    }
    const rows = threadRows().slice(0, CFG.MAX_ROWS_SCANNED);
    if (!rows.length) {
      setStatus("Marketplace list is empty or still loading", "info");
      return;
    }

    // Decide for every chat whether it is opened this cycle, and keep the reason (menu: "Download last decisions")
    const decisions = {};
    const todo = [];
    for (const r of rows) {
      const waitLeft = state.failedAt[r.id]
        ? CFG.FAILED_RETRY_MS - (Date.now() - state.failedAt[r.id])
        : 0;
      let why;
      if (r.answered) why = "skip: sidebar preview says you answered last";
      else if (isFresh(r.id, r.sig)) {
        const e = state.sigs[r.id];
        why =
          "skip: already handled for this message" +
          (e && e.note ? " | " + e.note : "");
      } else if (waitLeft > 0)
        why =
          "skip: failed earlier, retry in " + Math.ceil(waitLeft / 1000) + "s";
      else if (todo.length >= CFG.MAX_THREADS_PER_CYCLE)
        why =
          "skip: over the per-cycle limit (" + CFG.MAX_THREADS_PER_CYCLE + ")";
      else {
        why = "open: unanswered conversation";
        todo.push(r);
      }
      decisions[r.id] = {
        buyer: r.title.split(" · ")[0],
        preview: r.preview.slice(0, 40),
        why,
      };
    }
    const saveDecisions = () => {
      GM_setValue("last_decisions", decisions);
      GM_setValue("sigs", state.sigs);
      if (CFG.DEBUG) console.table(decisions);
    };

    let synced = 0,
      answered = 0,
      skipped = 0;
    for (const t of todo) {
      const d = decisions[t.id];
      const row = threadRows().find((r) => r.id === t.id); // the list re-renders: fetch a live element
      if (!row) {
        d.why = "skip: row vanished from the list";
        continue;
      }
      const snap = await openThread(row);
      if (!snap || !snap.messages.length) {
        state.failedAt[t.id] = Date.now();
        d.why =
          "failed: conversation did not open (title mismatch?) or no messages read, retrying shortly";
        continue;
      }
      const last = snap.messages[snap.messages.length - 1];
      const lastTxt = `last message is from ${last.sender}: "${last.text.slice(0, 30)}"`;
      if (!snap.marketplace) {
        // not recognised as Marketplace: retry later, do not cache
        skipped++;
        state.failedAt[t.id] = Date.now();
        d.why =
          "skip: not recognised as Marketplace (no list / banner / profile line); " +
          lastTxt;
        log("not marketplace, skipped", t.id, snap);
        continue;
      }
      if (!CFG.shouldHandle(snap)) {
        skipped++;
        remember(t.id, t.sig, true);
        d.why = "skip: shouldHandle() returned false";
        continue;
      }
      if (last.sender !== "customer") {
        remember(t.id, t.sig, true);
        d.why = "skip: " + lastTxt;
        continue;
      } // last message is yours: nothing to do
      const res = await api("POST", "/agent/sync", {
        fb_id: fbId(),
        conversations: [
          {
            thread_id: t.id,
            customer_name: snap.customerName,
            customer_id: snap.customerId,
            url: `https://www.facebook.com/messages/t/${t.id}/`,
            item_title: snap.itemTitle,
            messages: snap.messages,
          },
        ],
      });
      if (!res.ok) {
        d.why = "sync failed: " + (res.error || res.status);
        saveDecisions();
        setStatus("sync failed: " + (res.error || res.status), "error");
        return;
      }
      if (res.stored === false) {
        d.why = "server did not store it (collecting is off for this account)";
        saveDecisions();
        setStatus(
          "collecting is off for this account (Message workers)",
          "info",
        );
        return;
      }
      synced++;
      const r = (res.results || [])[0];
      const srv =
        "synced (" +
        lastTxt +
        "); server said " +
        JSON.stringify(r || {}).slice(0, 120);
      if (r && r.needs_reply && state.cfg.reply_mode !== "off") {
        state.note = "";
        const outcome = await answerThread(row, snap); // 'replied' | 'skipped' | false
        if (outcome === "replied") {
          answered++;
          remember(t.id, t.sig, true, "replied");
          d.why = "replied (" + state.cfg.reply_mode + ")";
        } else if (outcome === "skipped") {
          d.why = "server chose not to reply; " + srv;
          remember(t.id, t.sig, true, d.why);
        } else {
          d.why =
            "reply failed or aborted: " +
            (state.note || "unknown") +
            " (retrying shortly)";
          state.failedAt[t.id] = Date.now();
        }
      } else {
        d.why =
          (r && r.needs_reply
            ? "replies are off in the dashboard; "
            : "server says no reply needed; ") + srv;
        remember(t.id, t.sig, true, d.why);
      }
    }
    saveDecisions();
    setStatus(
      `ok · ${rows.length} chats, ${synced} unanswered synced, ${answered} answered, ${skipped} skipped (${state.cfg.reply_mode}) · menu: Download last decisions`,
      "ok",
    );
  }

  function checkSingleTabLock() {
    try {
      const raw = localStorage.getItem("mhub_active_tab");
      if (raw) {
        const parsed = JSON.parse(raw);
        if (
          parsed &&
          parsed.id !== TAB_ID &&
          Date.now() - parsed.time < 15000
        ) {
          return false; // Another tab is active
        }
      }
      localStorage.setItem(
        "mhub_active_tab",
        JSON.stringify({ id: TAB_ID, time: Date.now() }),
      );
      return true;
    } catch (e) {
      return true;
    }
  }

  async function cycle() {
    if (!checkSingleTabLock()) {
      setStatus(
        "standby: another tab is already running Marketplace Hub Agent",
        "info",
      );
      return;
    }
    if (!server() || !apiKey()) {
      setStatus(
        "not configured - Tampermonkey menu → Configure server",
        "warn",
      );
      return;
    }
    if (!fbId()) {
      setStatus("Facebook user id not found (are you logged in?)", "error");
      return;
    }
    const reg = await api("POST", "/agent/register", {
      fb_id: fbId(),
      tab_id: TAB_ID,
      version: VERSION,
      page: location.pathname,
    });
    if (!reg.ok) {
      setStatus("server: " + (reg.error || reg.status), "error");
      return;
    }
    state.cfg = reg.config;
    if (!state.cfg.enabled) {
      setStatus(
        "registered - enable this account in the dashboard (Message workers)",
        "info",
      );
      return;
    }
    if (!state.cfg.collect && state.cfg.reply_mode === "off") {
      setStatus("enabled, but collecting and replies are off", "info");
      return;
    }
    await processInbox();
  }

  async function loop() {
    if (state.running) return;
    state.running = true;
    await sleep(4000); // let Facebook finish rendering
    for (;;) {
      try {
        await cycle();
      } catch (e) {
        console.error("[MarketplaceHub]", e);
        setStatus("error: " + e.message, "error");
      }
      await sleep(
        Math.max(20, (state.cfg && state.cfg.poll_interval) || 60) *
          1000 *
          rand(0.9, 1.15),
      );
    }
  }

  /* ------------------------------ menu commands ----------------------- */
  GM_registerMenuCommand("Configure server", () => {
    const url = prompt(
      "Marketplace Hub server URL (e.g. https://hub.example.com):",
      server(),
    );
    if (url === null) return;
    const key = prompt("Script key (Settings → Script connection):", apiKey());
    if (key === null) return;
    GM_setValue("server_url", url.trim());
    GM_setValue("api_key", key.trim());
    location.reload();
  });
  GM_registerMenuCommand("Test scrape (see console)", () => {
    const inMp = !!marketplaceGrid(),
      folder = !!findFolderButton(),
      rows = threadRows();
    console.table(
      rows.map((r) => ({
        id: r.id,
        title: r.title,
        preview: r.preview,
        answeredByMe: r.answered,
      })),
    );
    const log = document.querySelector(CFG.SELECTORS.log);
    let snap = null;
    if (log) {
      const active = rows.find((r) =>
        norm(log.getAttribute("aria-label")).includes(
          norm(r.title).slice(0, 20),
        ),
      );
      snap = scrapeOpenThread(
        active || {
          title: (log.getAttribute("aria-label") || "").replace(
            /^.*?titled\s+/i,
            "",
          ),
        },
      );
    }
    console.log("[MarketplaceHub] open conversation:", snap);
    if (snap)
      console.table(
        snap.messages.map((m) => ({
          sender: m.sender,
          text: m.text.slice(0, 80),
        })),
      );
    alert(
      `Marketplace list open: ${inMp}\nMarketplace folder row found: ${folder}\nChats in list: ${rows.length} (answered by you: ${rows.filter((r) => r.answered).length})\n` +
        `Open conversation: ${snap ? snap.messages.length + " message(s), last from " + (snap.messages.length ? snap.messages[snap.messages.length - 1].sender : "-") + ", marketplace=" + snap.marketplace : "none"}\nDetails in the console (F12).`,
    );
  });
  GM_registerMenuCommand("Download last decisions (.txt)", () => {
    const d = GM_getValue("last_decisions", {});
    const mem = GM_getValue("sigs", {});
    const out = [];
    out.push(`Marketplace Hub Agent v${VERSION} - decision log`);
    out.push(`Created: ${new Date().toLocaleString()}`);
    out.push(`Page: ${location.href}`);
    out.push(`Badge: ${badge.textContent}`);
    if (state.cfg)
      out.push(
        `Server config: enabled=${state.cfg.enabled}, collect=${state.cfg.collect}, reply_mode=${state.cfg.reply_mode}, poll_interval=${state.cfg.poll_interval}`,
      );
    out.push("");
    out.push("=== LAST CYCLE (one entry per chat) ===");
    const ids = Object.keys(d);
    if (!ids.length) out.push("No cycle has completed yet.");
    ids.forEach((id) => {
      const x = d[id];
      out.push("");
      out.push(`Buyer:   ${x.buyer}`);
      out.push(`Thread:  ${id}`);
      out.push(`Preview: ${x.preview}`);
      out.push(`Result:  ${x.why}`);
    });
    out.push("");
    out.push("=== MEMORY (chats already looked at) ===");
    const mids = Object.keys(mem);
    if (!mids.length) out.push("Empty.");
    mids.forEach((id) => {
      const e = mem[id];
      if (typeof e !== "object") {
        out.push(`${id}: old-format entry (will be re-checked)`);
        return;
      }
      out.push(
        `${id}: ${e.final ? "FINAL" : "unanswered"}, checked ${new Date(e.at).toLocaleTimeString()}${e.note ? " | " + e.note : ""}`,
      );
    });
    const blob = new Blob([out.join("\n") + "\n"], {
      type: "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    a.href = url;
    a.download = `marketplace-hub-decisions-${stamp}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    setStatus("decision log downloaded", "info");
  });
  GM_registerMenuCommand("Toggle debug logging", () => {
    GM_setValue("debug", !GM_getValue("debug", false));
    location.reload();
  });
  GM_registerMenuCommand("Forget seen conversations", () => {
    GM_setValue("sigs", {});
    state.sigs = {};
    state.failedAt = {};
    setStatus("memory cleared", "info");
  });

  setStatus("starting…");
  injectMainWorldScript();
  loop();
})();
