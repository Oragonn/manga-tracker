// Kenmei Import Helper - runs on /import-kenmei. Intercepts the page's own
// "All" button (capturing-phase click on the tbody, ahead of the page's own
// bubble-phase handler) so it opens the 5 search tabs through the
// background service worker instead of window.open()/synthetic anchors -
// no popup-permission dance needed. Reads everything it needs straight off
// the already-rendered row: title text, the 4 search anchors' hrefs, the
// URL input, and the Add button - the page itself is never modified.
//
// As each source tab is captured (Y) or skipped (U), the background worker
// pushes the running list of URLs here and this fills the URL input live.
// Once all 4 tabs are resolved, the last push also carries submit:true,
// which clicks Add for real - the page's own addRow() logic (validation,
// the add/poll pipeline, toasts) runs completely untouched. "I" (here or
// relayed from a source tab) starts the next pending row on demand - there
// is no automatic chaining, you decide when to move on.

(function () {
  let currentRow = null; // { title, urlInputEl, addBtnEl }
  let badgeEl = null;

  chrome.runtime.sendMessage({ type: 'registerImportTab' });

  // Tab-opening order, independent of the page's own MD/AT/AS/KG/HT button
  // layout (left untouched). Kagane goes last since its Cloudflare Turnstile
  // challenge makes it the slowest tab to load.
  const SITE_OPEN_ORDER = ['atsu', 'asura', 'mangadex', 'hive', 'kagane'];
  const HREF_SITE_PATTERNS = [
    { re: /^https:\/\/mangadex\.org\//, site: 'mangadex' },
    { re: /^https:\/\/atsu\.moe\//, site: 'atsu' },
    { re: /^https:\/\/asurascans\.com\//, site: 'asura' },
    { re: /^https:\/\/kagane\.(to|org)\//, site: 'kagane' },
    { re: /^https:\/\/hivetoons\.org\//, site: 'hive' }
  ];

  function siteForHref(href) {
    const match = HREF_SITE_PATTERNS.find((p) => p.re.test(href));
    return match ? match.site : 'unknown';
  }

  function orderByOpeningPreference(hrefs) {
    const ordered = SITE_OPEN_ORDER.map((site) => hrefs.find((h) => siteForHref(h) === site)).filter(Boolean);
    // Anything unrecognized still opens, just after the known ones.
    const leftover = hrefs.filter((h) => !ordered.includes(h));
    return ordered.concat(leftover);
  }

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

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
    if (!state) {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'block';
    if (state.errorText) {
      el.innerHTML =
        `<strong>${escapeHtmlLocal(state.title)}</strong><br>` +
        `<span style="color:#f56565">${escapeHtmlLocal(state.errorText)}</span>`;
      return;
    }
    el.innerHTML =
      `<strong>${escapeHtmlLocal(state.title)}</strong><br>` +
      `captured ${state.capturedCount}/${state.total} &middot; ${state.openCount} tab(s) open<br>` +
      `<span style="color:#64748b">Y capture &middot; U skip &middot; I next row</span>`;
  }

  function findNextPendingRow() {
    const rows = document.querySelectorAll('#import-tbody tr');
    for (const tr of rows) {
      if (tr.classList.contains('row-imported')) continue;
      const addBtn = tr.querySelector('.row-add-btn');
      if (!addBtn || addBtn.disabled) continue;
      const anchors = tr.querySelectorAll('.search-btn-group a');
      if (anchors.length < 5) continue;
      return tr;
    }
    return null; // none left on this page of results - next page needs a manual click
  }

  function startRowFromTr(tr) {
    if (!tr) return;
    const titleEl = tr.querySelector('.row-title');
    const urlInput = tr.querySelector('.row-url-input');
    const addBtn = tr.querySelector('.row-add-btn');
    const anchors = Array.from(tr.querySelectorAll('.search-btn-group a')).map((a) => a.href);
    if (!titleEl || !urlInput || !addBtn || anchors.length < 5) return;

    currentRow = { title: titleEl.textContent.trim(), urlInputEl: urlInput, addBtnEl: addBtn };
    chrome.runtime.sendMessage({
      type: 'startRow',
      title: currentRow.title,
      urls: orderByOpeningPreference(anchors)
    });
  }

  // Matches by title text since the page fully rebuilds <tr> nodes on every
  // render() (filter change, page change, addRow's own state updates) - a
  // held DOM reference would go stale, so we re-query. Rows sharing an
  // identical title would be ambiguous here; acceptable for this workflow.
  function pollForCompletion(title, attempt) {
    attempt = attempt || 0;
    if (attempt > 130) { // ~65s, matching the page's own 60x1s add-status poll
      renderBadge({ title, errorText: 'Timed out waiting for the add to finish' });
      return;
    }
    const rows = document.querySelectorAll('#import-tbody tr');
    for (const tr of rows) {
      const t = tr.querySelector('.row-title');
      if (!t || t.textContent.trim() !== title) continue;
      if (tr.classList.contains('row-imported')) {
        renderBadge(null);
        return;
      }
      const status = tr.querySelector('.row-status-text');
      if (status && status.classList.contains('err')) {
        renderBadge({ title, errorText: status.textContent.trim() || 'Failed to add' });
        return;
      }
      break;
    }
    setTimeout(() => pollForCompletion(title, attempt + 1), 500);
  }

  document.getElementById('import-tbody')?.addEventListener(
    'click',
    (e) => {
      const btn = e.target.closest('.btn-open-all');
      if (!btn) return;
      e.stopImmediatePropagation();
      e.preventDefault();
      startRowFromTr(btn.closest('tr'));
    },
    true
  );

  document.addEventListener(
    'keydown',
    (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTypingTarget(document.activeElement)) return;
      if (e.key.toLowerCase() === 'i') {
        e.preventDefault();
        startRowFromTr(findNextPendingRow());
      }
    },
    true
  );

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'updateUrls') {
      if (!currentRow) return;
      currentRow.urlInputEl.value = msg.urls.join(',');
      currentRow.urlInputEl.dispatchEvent(new Event('input', { bubbles: true }));
      if (msg.submit) {
        const title = currentRow.title;
        currentRow.addBtnEl.click();
        currentRow = null;
        pollForCompletion(title);
      }
    } else if (msg.type === 'stateUpdate') {
      renderBadge(msg.state);
    } else if (msg.type === 'startNextRow') {
      startRowFromTr(findNextPendingRow());
    }
  });
})();
