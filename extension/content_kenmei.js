// Kenmei Series Lookup Helper - runs on an individual kenmei.co series
// page. Same source-matching step as the CSV import flow (content_import.js
// / content_source.js), but for looking one series up directly on kenmei.co
// instead of importing a full CSV export:
//   I - open the same 5 source searches for this page's title, and (in the
//       background) peek at Kenmei's own "Add to your Dashboard" source
//       list to show which of the 5 it already knows about
//   K - jump into the first result on a source tab (content_source.js)
//   Y - capture a source tab's URL, close it (content_source.js)
//   U - no match on a source tab, just close it (content_source.js)
// K/Y/U are handled by content_source.js, shared with the import flow. The
// difference is what Y does with a capture: there's no import page open in
// this flow to fill a URL box (and its box doesn't want a title in it
// anyway), so each Y instead copies the running "url, url, ..." list to the
// clipboard - see content_source.js's Y handler for why that write happens
// on the source tab itself rather than here.

(function () {
  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  // Mirrors searchUrl() in import_kenmei.html so both flows search sources
  // the same way.
  function searchUrl(site, title) {
    const q = encodeURIComponent(title);
    switch (site) {
      case 'mangadex': return `https://mangadex.org/search?q=${q}`;
      case 'atsu': return `https://atsu.moe/explore?search=${q}`;
      case 'kagane': return `https://kagane.to/search?q=${q}&size=99`;
      case 'asura': return `https://asurascans.com/browse?q=${q}`;
      case 'hive': return `https://hivetoons.org/series/?searchTerm=${q}`;
      default: return null;
    }
  }
  // Same opening order as the import flow - Kagane last, its Cloudflare
  // Turnstile challenge makes it the slowest tab to load.
  const SEARCH_SITES = ['atsu', 'asura', 'mangadex', 'hive', 'kagane'];
  const SITE_LABELS = { mangadex: 'MangaDex', atsu: 'Atsumaru', asura: 'AsuraScans', kagane: 'Kagane', hive: 'HiveToons' };
  // How Kenmei's own source-select labels its options, lowercased.
  const KENMEI_NAME_TO_SITE = {
    'mangadex': 'mangadex',
    'atsumaru': 'atsu',
    'asura scans': 'asura',
    'asurascans': 'asura',
    'kagane': 'kagane',
    'hive toon': 'hive',
    'hivetoons': 'hive'
  };

  // Not scoped to Kenmei's Vue `data-v-*` hash - that hash changes on every
  // deploy. The series title is the only h1 on this page.
  function seriesTitle() {
    const h1 = document.querySelector('.min-h-9 h1, h1');
    return h1 ? h1.textContent.trim() : null;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function waitFor(check, { timeout = 2500, interval = 100 } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const result = check();
      if (result) return result;
      await sleep(interval);
    }
    return null;
  }

  // Reka UI's Select trigger (this component library, the Vue port of
  // Radix) opens on pointerdown, not click - a plain el.click() only
  // synthesizes mousedown/mouseup/click with no pointerdown at all, so the
  // trigger's own open handler never runs and the listbox never mounts.
  // Firing the fuller, realistic sequence gets picked up the same way a
  // real mouse click would be.
  function simulateRealClick(el) {
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const base = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, button: 0 };
    const pointerBase = { ...base, pointerId: 1, pointerType: 'mouse', isPrimary: true };
    el.dispatchEvent(new PointerEvent('pointerdown', pointerBase));
    el.dispatchEvent(new MouseEvent('mousedown', base));
    el.dispatchEvent(new PointerEvent('pointerup', pointerBase));
    el.dispatchEvent(new MouseEvent('mouseup', base));
    el.dispatchEvent(new MouseEvent('click', base));
  }

  function findAddToDashboardButton() {
    const buttons = document.querySelectorAll('button[data-slot="button"]');
    for (const b of buttons) {
      if (b.textContent.trim() === 'Add to your Dashboard') return b;
    }
    return null;
  }

  function readSourceOptionNames() {
    const items = document.querySelectorAll('[data-slot="select-item"][role="option"]');
    const names = [];
    items.forEach((item) => {
      // The site name is the first `.truncate` span - the one before it is
      // an icon-only span (no text), the one after is the chapter number.
      const label = item.querySelector('span.truncate');
      if (label) names.push(label.textContent.trim());
    });
    return names.length > 0 ? names : null;
  }

  // Cached once found so a later "I" on the same page reuses the already-
  // expanded form instead of clicking "Add to your Dashboard" again, whose
  // effect on a second click (toggle shut? no-op? something else?) isn't
  // known and isn't worth risking.
  let cachedSelectTrigger = null;

  // Opens Kenmei's own "Add to your Dashboard" source picker just far enough
  // to read which sources it already knows this series is on, then backs
  // out without touching anything else. Confirmed: that button only expands
  // an inline form, nothing is added to the dashboard until a separate Save
  // click - this never makes one. Returns the list of Kenmei's own source
  // labels (e.g. ["Hive Toon", "Atsumaru", ...]), or null if the button/
  // dropdown couldn't be found (layout changed, not logged in, etc).
  async function fetchKenmeiSources() {
    if (!cachedSelectTrigger || !document.contains(cachedSelectTrigger)) {
      const addBtn = findAddToDashboardButton();
      if (!addBtn) return null;
      const before = new Set(document.querySelectorAll('button[data-slot="select-trigger"]'));
      simulateRealClick(addBtn);
      cachedSelectTrigger = await waitFor(() => {
        const triggers = Array.from(document.querySelectorAll('button[data-slot="select-trigger"]'));
        // Normally a newly-created node the form just rendered; fall back to
        // "the only one on the page" in case it existed all along just
        // hidden (e.g. toggled via v-show, not v-if) and so isn't "new".
        return triggers.find((el) => !before.has(el)) || (triggers.length === 1 ? triggers[0] : null);
      });
    }
    if (!cachedSelectTrigger) return null;

    simulateRealClick(cachedSelectTrigger);
    const names = await waitFor(readSourceOptionNames);
    // Close the listbox without changing the selection - Escape, same as a
    // user backing out of any combobox. The surrounding form is left
    // expanded (harmless, nothing commits without a Save click) since how
    // to collapse it isn't known.
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
    return names;
  }

  // Kenmei's own labels -> our site keys, or null if the lookup itself
  // failed outright (not the same as "found it, matched nothing").
  function mapNamesToSites(names) {
    if (!names) return null;
    return new Set(names.map((n) => KENMEI_NAME_TO_SITE[n.toLowerCase()]).filter(Boolean));
  }

  function describeKenmeiSources(haveSites) {
    if (!haveSites) return "couldn't read Kenmei's own source list";
    const have = SEARCH_SITES.filter((s) => haveSites.has(s)).map((s) => SITE_LABELS[s]);
    const missing = SEARCH_SITES.filter((s) => !haveSites.has(s)).map((s) => SITE_LABELS[s]);
    let text = `Kenmei has: ${have.length ? have.join(', ') : 'none of our 5'}`;
    if (missing.length) text += ` (not ${missing.join(', ')})`;
    return text;
  }

  let badgeEl = null;
  let lastState = null; // last state object passed to renderBadge, for refreshBadge()
  let kenmeiNote = '';

  function ensureBadge() {
    if (badgeEl) return badgeEl;
    badgeEl = document.createElement('div');
    badgeEl.id = 'kenmei-ext-badge';
    badgeEl.style.cssText = [
      'position:fixed', 'bottom:16px', 'right:16px', 'z-index:999999',
      'background:#141b2f', 'border:1px solid #334155', 'border-radius:10px',
      'padding:10px 14px', 'color:#e2e8f0', 'font:13px system-ui,sans-serif',
      'line-height:1.5', 'box-shadow:0 4px 16px rgba(0,0,0,.4)',
      'max-width:280px', 'display:none'
    ].join(';');
    document.body.appendChild(badgeEl);
    return badgeEl;
  }

  function escapeHtmlLocal(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function renderBadge(state) {
    lastState = state;
    const el = ensureBadge();
    if (!state) {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'block';
    const status = state.done
      ? `copied ${state.capturedCount}/${state.total} - done`
      : `copied ${state.capturedCount}/${state.total} &middot; ${state.openCount} tab(s) open`;
    el.innerHTML =
      `<strong>${escapeHtmlLocal(state.title)}</strong><br>` +
      `${status}<br>` +
      (kenmeiNote ? `<span style="color:#94a3b8">${escapeHtmlLocal(kenmeiNote)}</span><br>` : '') +
      `<span style="color:#64748b">Y copy &middot; U skip &middot; I ${state.done ? 're' : ''}start</span>`;
  }

  function refreshBadge() {
    if (lastState) renderBadge(lastState);
  }

  function startRow() {
    const title = seriesTitle();
    if (!title) return;
    const urls = SEARCH_SITES.map((site) => searchUrl(site, title));
    chrome.runtime.sendMessage({ type: 'startRow', title, urls, mode: 'kenmei' });

    kenmeiNote = 'checking Kenmei’s own source list…';
    refreshBadge();
    fetchKenmeiSources()
      .catch(() => null)
      .then((names) => {
        const haveSites = mapNamesToSites(names);
        kenmeiNote = describeKenmeiSources(haveSites);
        refreshBadge();
        // Tell each of the 5 tabs this row just opened whether Kenmei
        // already lists its source, so they can show a red/green dot -
        // only once we actually have an answer, so a failed lookup leaves
        // them undotted instead of falsely marking everything red.
        if (haveSites) {
          chrome.runtime.sendMessage({ type: 'kenmeiSourcesKnown', sites: Array.from(haveSites) });
        }
      });
  }

  chrome.runtime.sendMessage({ type: 'registerKenmeiTab' });

  document.addEventListener(
    'keydown',
    (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTypingTarget(document.activeElement)) return;
      if (e.key.toLowerCase() === 'i') {
        e.preventDefault();
        startRow();
      }
    },
    true
  );

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'updateUrls') {
      // Only the final resolution needs rendering here - every
      // in-progress step already arrives as a fuller 'stateUpdate' below.
      if (msg.submit) {
        renderBadge({ title: msg.title, capturedCount: msg.urls.length, total: msg.total, openCount: 0, done: true });
      }
    } else if (msg.type === 'stateUpdate') {
      renderBadge(msg.state);
    } else if (msg.type === 'startNextRow') {
      startRow();
    }
  });
})();
