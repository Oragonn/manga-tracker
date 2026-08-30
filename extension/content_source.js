// Kenmei Import Helper - runs on the 4 source sites (MangaDex/Atsumaru/
// AsuraScans/Kagane). Plain, unmodified single-key shortcuts:
//   K - jump into the first result on a search-results page
//   Y - capture this tab's URL for the row being matched, close the tab
//   U - no match here, just close the tab
//   I - start the next pending row (same as clicking "All" on it)
// All are ignored while typing in a field, so they don't clobber normal
// use of the site's own search boxes.

(function () {
  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  // Matched by URL shape (the same one the add-series pipeline itself
  // trusts - see detectSourceType() in import_kenmei.html and the
  // is_mangadex/is_kagane/is_atsu/is_asura checks in backend/api.py)
  // rather than a site's CSS classes, which redesigns break constantly.
  const FIRST_RESULT_PATTERNS = [
    // Requires a real UUID after /title/ - MangaDex's own sidebar has a
    // "Random" nav link at /title/random that a plain /title/ substring
    // check would wrongly match (and it sits earlier in the DOM than the
    // actual results grid).
    { hostRe: /(^|\.)mangadex\.org$/, hrefRe: /\/title\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i },
    { hostRe: /(^|\.)atsu\.moe$/, hrefRe: /\/(manga|read)\// },
    { hostRe: /(^|\.)asurascans\.com$/, hrefRe: /\/comics\// },
    { hostRe: /(^|\.)kagane\.(to|org)$/, hrefRe: /\/series\// }
  ];

  function isVisible(el) {
    // Filters out hidden-menu duplicates / off-screen widgets that can sit
    // earlier in the DOM than the actual results grid (zero-size when
    // display:none or detached, unlike a real visible result card).
    return el.getClientRects().length > 0;
  }

  function findFirstResultHref() {
    const site = FIRST_RESULT_PATTERNS.find((p) => p.hostRe.test(location.hostname));
    if (!site) return null;
    const anchors = document.querySelectorAll('a[href]');
    for (const a of anchors) {
      if (!site.hrefRe.test(a.getAttribute('href') || '')) continue;
      if (!isVisible(a)) continue;
      return a.href;
    }
    return null;
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
})();
