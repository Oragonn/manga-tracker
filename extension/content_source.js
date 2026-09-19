// Kenmei Import Helper - runs on the 5 source sites (MangaDex/Atsumaru/
// AsuraScans/Kagane/HiveToons). Plain, unmodified single-key shortcuts:
//   K - jump into the first result on a search-results page
//   Y - capture this tab's URL for the row being matched, close the tab
//   U - no match here, just close the tab
//   I - start the next pending row (same as clicking "All" on it)
// All are ignored while typing in a field, so they don't clobber normal
// use of the site's own search boxes.
//
// Shared by both flows (CSV import row-matching and a single kenmei.co
// series lookup - see content_import.js / content_kenmei.js). Y always just
// reports the capture to the background worker; for the kenmei flow, the
// worker is the one that copies the running list to the clipboard (via an
// offscreen document - see background.js), since none of these 5 tabs can
// be relied on to actually have document focus.
//
// For the kenmei flow only, a red/green dot shows up next to the first
// result (the same one K would jump into) once background.js relays
// whether Kenmei's own source list already includes this tab's site - see
// kenmeiSourceKnown below. Never appears for the CSV import flow, since
// that message is only ever sent for kenmei-mode rows.

(function () {
  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  // Matched by URL shape (the same one the add-series pipeline itself
  // trusts - see detectSourceType() in import_kenmei.html and the
  // is_mangadex/is_kagane/is_atsu/is_asura/is_hive checks in backend/api.py)
  // rather than a site's CSS classes, which redesigns break constantly.
  const FIRST_RESULT_PATTERNS = [
    // Requires a real UUID after /title/ - MangaDex's own sidebar has a
    // "Random" nav link at /title/random that a plain /title/ substring
    // check would wrongly match (and it sits earlier in the DOM than the
    // actual results grid).
    { hostRe: /(^|\.)mangadex\.org$/, hrefRe: /\/title\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i },
    { hostRe: /(^|\.)atsu\.moe$/, hrefRe: /\/(manga|read)\// },
    { hostRe: /(^|\.)asurascans\.com$/, hrefRe: /\/comics\// },
    { hostRe: /(^|\.)kagane\.(to|org)$/, hrefRe: /\/series\// },
    // Requires an actual slug after /series/ - HiveToons' own nav has a bare
    // "/series/" link (its all-series browse page) that a plain substring
    // check would wrongly match if it sits earlier in the DOM than the
    // actual results grid, same trap MangaDex's UUID check above avoids.
    { hostRe: /(^|\.)hivetoons\.org$/, hrefRe: /\/series\/[a-z0-9-]+\/?$/i }
  ];

  function isVisible(el) {
    // Filters out hidden-menu duplicates / off-screen widgets that can sit
    // earlier in the DOM than the actual results grid (zero-size when
    // display:none or detached, unlike a real visible result card).
    return el.getClientRects().length > 0;
  }

  function findFirstResultAnchor() {
    const site = FIRST_RESULT_PATTERNS.find((p) => p.hostRe.test(location.hostname));
    if (!site) return null;
    const anchors = document.querySelectorAll('a[href]');
    for (const a of anchors) {
      if (!site.hrefRe.test(a.getAttribute('href') || '')) continue;
      if (!isVisible(a)) continue;
      return a;
    }
    return null;
  }

  function findFirstResultHref() {
    const a = findFirstResultAnchor();
    return a ? a.href : null;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function waitFor(check, { timeout = 3000, interval = 150 } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const result = check();
      if (result) return result;
      await sleep(interval);
    }
    return null;
  }

  let sourceDot = null;

  function ensureSourceDot() {
    if (sourceDot) return sourceDot;
    sourceDot = document.createElement('div');
    sourceDot.id = 'kenmei-ext-source-dot';
    sourceDot.style.cssText = [
      'position:fixed', 'z-index:999999', 'pointer-events:none',
      'width:14px', 'height:14px', 'border-radius:50%',
      'border:2px solid rgba(255,255,255,.85)',
      'box-shadow:0 1px 4px rgba(0,0,0,.6)'
    ].join(';');
    document.body.appendChild(sourceDot);
    return sourceDot;
  }

  // Anchored to the first result's own position (recomputed each call, so
  // it tracks layout shifts from images/content still loading in) rather
  // than a fixed page corner - much easier to actually notice since it's
  // sitting right by the same result K would jump into. Placed just outside
  // its left edge, not overlapping the box itself - the dot means "Kenmei
  // says this SITE has the series", not "this specific search result is the
  // confirmed match" (the search can turn up nothing, or the wrong thing,
  // purely from a title mismatch, even when the site does have the series),
  // so it shouldn't visually read as being stamped onto that particular
  // result card.
  function positionDotNear(el) {
    const rect = el.getBoundingClientRect();
    const dotSize = 14;
    const gap = 6;
    sourceDot.style.top = `${Math.max(4, rect.top)}px`;
    sourceDot.style.left = `${Math.max(4, rect.left - dotSize - gap)}px`;
  }

  async function showSourceDot(hasSource) {
    const el = ensureSourceDot();
    el.style.background = hasSource ? '#22c55e' : '#ef4444';
    el.title = hasSource ? "Kenmei lists this source for this series" : "Kenmei doesn't list this source for this series";

    // The search-results grid may still be loading when this arrives -
    // give it a few seconds before falling back to a fixed corner dot.
    const anchor = await waitFor(findFirstResultAnchor);
    if (anchor) {
      positionDotNear(anchor);
      // One more pass shortly after - images/lazy content settling in can
      // still shift the result's position right after it first appears.
      setTimeout(() => positionDotNear(anchor), 800);
    } else {
      el.style.top = '12px';
      el.style.left = '';
      el.style.right = '12px';
    }
  }

  document.addEventListener(
    'keydown',
    (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTypingTarget(document.activeElement)) return;

      const key = e.key.toLowerCase();
      if (key === 'k') {
        e.preventDefault();
        const href = findFirstResultHref();
        if (href) location.assign(href);
      } else if (key === 'y') {
        e.preventDefault();
        chrome.runtime.sendMessage({ type: 'capture', url: location.href });
      } else if (key === 'u') {
        e.preventDefault();
        chrome.runtime.sendMessage({ type: 'skip' });
      } else if (key === 'i') {
        e.preventDefault();
        chrome.runtime.sendMessage({ type: 'startNextRow' });
      }
    },
    true
  );

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'kenmeiSourceKnown') showSourceDot(msg.hasSource);
  });

  // Also pull on load, in case background.js already pushed (or only ever
  // will) kenmeiSourceKnown before this particular content script instance
  // was around to receive it - e.g. this tab reloaded into a different page
  // shortly after opening (Kagane's Cloudflare Turnstile challenge page
  // does this), which tears down whatever was listening and starts fresh
  // with no memory of an already-delivered push.
  chrome.runtime.sendMessage({ type: 'getKenmeiSourceInfo' }, (response) => {
    if (response) showSourceDot(response.hasSource);
  });
})();
