// Dashboard Search Helper - runs on the tracker's own dashboard. Takes over
// the two "search this title on all 7 sources" buttons:
//   - Add Series modal -> search view -> "Search" (click, middle-click, or
//     Enter in the "Search title" box)
//   - Series Settings modal -> the source section's search button
// and opens the 7 tabs through the background worker instead of the page's
// own synthetic <a> clicks, so the same K/Y/U keys as the kenmei.co lookup
// flow work on them (content_source.js):
//   K - jump into the first result on a source tab
//   Y - copy the running "url, url, ..." list to the clipboard, close the tab
//   U - no match on a source tab, just close it
//   I - on a source tab, re-run the last search (start over)
// Same clipboard result as the kenmei.co flow; on top of that, once every
// tab of an Add Series search is closed, the links are handed to the page
// (a 'manga-tracker:source-links' event) and land in the Add modal's link
// box. Nothing is ever submitted. Series Settings searches stay
// clipboard-only.
//
// Intercepted with window-level capture listeners, which run before the
// page's own handlers on the buttons/input themselves - the page and
// dashboard.js are never modified, and without the extension the buttons
// keep their plain open-7-tabs behavior.

(function () {
  // Mirrors addSeriesSearchUrl() in dashboard.js.
  function searchUrl(site, title) {
    const q = encodeURIComponent(title);
    switch (site) {
      case 'mangadex': return `https://mangadex.org/search?q=${q}`;
      case 'atsu': return `https://atsu.moe/explore?search=${q}`;
      case 'kagane': return `https://kagane.to/search?q=${q}&size=99`;
      case 'asura': return `https://asurascans.com/browse?q=${q}`;
      case 'hive': return `https://hivetoons.org/series/?searchTerm=${q}`;
      case 'thunder': return `https://en-thunderscans.com/?s=${q}`;
      case 'comix': return `https://comix.to/browse?q=${q}&sort=relevance%3Adesc`;
      default: return null;
    }
  }
  // Same opening order as the other flows - Comix and Kagane last, their
  // Cloudflare challenges make them the slowest tabs to load.
  const SEARCH_SITES = ['atsu', 'asura', 'mangadex', 'hive', 'thunder', 'comix', 'kagane'];

  let lastTitle = null;
  // Which button started the search: 'add' (the Add Series search, also run
  // by the dashboard's "Search the sources" offer) or 'settings'
  let lastFrom = null;
  let badgeEl = null;
  let hideTimer = null;

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
    const el = ensureBadge();
    clearTimeout(hideTimer);
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
      `<span style="color:#64748b">K first result &middot; Y copy &middot; U skip</span>`;
    // Unlike the kenmei.co page, the dashboard is somewhere you keep
    // working - don't leave a finished badge parked in the corner.
    if (state.done) hideTimer = setTimeout(() => renderBadge(null), 8000);
  }

  function startSearch(title, from) {
    title = (title || '').trim();
    if (!title) return;
    lastTitle = title;
    if (from) lastFrom = from;
    const urls = SEARCH_SITES.map((site) => searchUrl(site, title));
    chrome.runtime.sendMessage({ type: 'startRow', title, urls, mode: 'dashboard' });
  }

  function addSeriesTitle() {
    return document.getElementById('add-series-search-title')?.value;
  }

  function settingsTitle() {
    return document.getElementById('edit-series-title-heading')?.textContent;
  }

  function intercept(e, title, from) {
    e.preventDefault();
    e.stopImmediatePropagation();
    startSearch(title, from);
  }

  window.addEventListener(
    'click',
    (e) => {
      if (e.button !== 0) return;
      if (e.target.closest?.('#btn-add-series-search-submit')) intercept(e, addSeriesTitle(), 'add');
      else if (e.target.closest?.('#settings-source-search-btn')) intercept(e, settingsTitle(), 'settings');
    },
    true
  );

  // The page also opens the searches on a middle-click of the Add Series
  // "Search" button.
  window.addEventListener(
    'auxclick',
    (e) => {
      if (e.button !== 1) return;
      if (e.target.closest?.('#btn-add-series-search-submit')) intercept(e, addSeriesTitle(), 'add');
    },
    true
  );

  window.addEventListener(
    'keydown',
    (e) => {
      if (e.key !== 'Enter' || e.isComposing) return;
      if (e.target.id === 'add-series-search-title') intercept(e, addSeriesTitle(), 'add');
    },
    true
  );

  chrome.runtime.sendMessage({ type: 'registerDashboardTab' });

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'updateUrls') {
      if (msg.submit) {
        renderBadge({ title: msg.title, capturedCount: msg.urls.length, total: msg.total, openCount: 0, done: true });
        // Every tab closed: an Add Series search's links go into the Add
        // modal's link box (dashboard.js listens for this event)
        if (lastFrom === 'add' && msg.urls.length) {
          window.dispatchEvent(new CustomEvent('manga-tracker:source-links', { detail: msg.urls.join(', ') }));
        }
      }
    } else if (msg.type === 'stateUpdate') {
      renderBadge(msg.state);
    } else if (msg.type === 'startNextRow') {
      // I pressed on a source tab - start this search over.
      if (lastTitle) startSearch(lastTitle);
    }
  });
})();
