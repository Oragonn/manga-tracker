// Kenmei Import Helper - background service worker.
//
// Owns the one-row-at-a-time state machine: which of the row's 5
// source-search tabs are still open, and which URLs have been captured
// (Y) so far. Content scripts never talk to each other directly -
// everything routes through here.
//
// Two independent flows share this same state machine, told apart by
// state.mode:
//   'import' - content_import.js, one row per line of an uploaded Kenmei
//              CSV. Captured URLs fill the row's URL box; the last one
//              also clicks Add.
//   'kenmei' - content_kenmei.js, a single series looked up directly on a
//              kenmei.co series page. Captured URLs are copied to the
//              clipboard instead, via a hidden offscreen document (see
//              copyToClipboard() below and offscreen.js) - a service worker
//              has no clipboard access itself, and writing from the source
//              tab that captured the match doesn't work either, since only
//              the first of the row's 5 tabs is ever made active/focused.
//   'dashboard' - content_dashboard.js, the tracker dashboard's own "search
//              all 5 sources" buttons (Add Series / Series Settings). Same
//              clipboard behavior as 'kenmei', just started from a
//              different page.
//
// importTabId/kenmeiTabId/dashboardTabId are tracked independently of
// `state` (registered as soon as their content script loads) so "I" pressed on a source tab can
// still reach the right page even before any row has been started yet.

let importTabId = null;
let kenmeiTabId = null;
let dashboardTabId = null;
let state = null; // { mode, title, total, sourceTabs: [{tabId, site}], capturedUrls: [] }

// A snapshot of the current row's tabs that, unlike state.sourceTabs, is
// NOT removed from as each tab resolves (Y/U) - kept around so the kenmei
// source-dot lookup (which finishes well after the row started, sometimes
// after every tab has already been resolved by a fast K/Y/U user) still has
// somewhere to send its answer. Closed tabs just no-op on sendMessage.
let lastRowTabs = [];
let lastRowMode = null;
// The answer itself, kept (not just relayed and dropped) so a tab that
// missed the one-shot push below - e.g. Kagane's Cloudflare Turnstile page
// reloading into the real page right as the push arrived, tearing down the
// content script mid-flight - can instead just ask for it on load. null
// until known for the current row.
let lastRowKenmeiSites = null;

const SITE_PATTERNS = [
  { re: /^https:\/\/mangadex\.org\//, site: 'mangadex' },
  { re: /^https:\/\/atsu\.moe\//, site: 'atsu' },
  { re: /^https:\/\/asurascans\.com\//, site: 'asura' },
  { re: /^https:\/\/kagane\.(to|org)\//, site: 'kagane' },
  { re: /^https:\/\/hivetoons\.org\//, site: 'hive' }
];

function siteFor(url) {
  const match = SITE_PATTERNS.find((p) => p.re.test(url));
  return match ? match.site : 'unknown';
}

function originTabId(mode) {
  if (mode === 'kenmei') return kenmeiTabId;
  if (mode === 'dashboard') return dashboardTabId;
  return importTabId;
}

// Modes whose end result is the clipboard rather than a page's URL box.
function copiesToClipboard(mode) {
  return mode === 'kenmei' || mode === 'dashboard';
}

// Offscreen document setup, lazily created on first copy and reused after
// that. Guarded by a stashed promise so concurrent calls don't race to
// create it twice; createDocument() rejecting because one already exists
// (e.g. the service worker was restarted and forgot offscreenReady, but the
// document itself outlived it) is treated as success, not an error.
let offscreenReady = null;

function ensureOffscreenDocument() {
  if (!offscreenReady) {
    offscreenReady = chrome.offscreen
      .createDocument({
        url: 'offscreen.html',
        reasons: ['CLIPBOARD'],
        justification: 'Copy matched Kenmei source links to the clipboard'
      })
      .catch(() => {});
  }
  return offscreenReady;
}

async function copyToClipboard(text) {
  await ensureOffscreenDocument();
  await chrome.runtime.sendMessage({ target: 'offscreen', type: 'copyToClipboard', text });
}

function broadcastState() {
  if (!state) return;
  const target = originTabId(state.mode);
  if (target == null) return;
  chrome.tabs
    .sendMessage(target, {
      type: 'stateUpdate',
      state: {
        title: state.title,
        capturedCount: state.capturedUrls.length,
        total: state.total,
        openCount: state.sourceTabs.length
      }
    })
    .catch(() => {});
}

async function startRow(title, urls, originTab, mode) {
  if (mode === 'kenmei') kenmeiTabId = originTab.id;
  else if (mode === 'dashboard') dashboardTabId = originTab.id;
  else importTabId = originTab.id;
  // A previous row that never fully resolved (e.g. abandoned via a fresh
  // "I") - best-effort close its leftover tabs before starting the new one.
  if (state) {
    for (const t of state.sourceTabs) chrome.tabs.remove(t.tabId).catch(() => {});
  }
  state = { mode, title, total: urls.length, sourceTabs: [], capturedUrls: [] };
  lastRowTabs = [];
  lastRowMode = mode;
  lastRowKenmeiSites = null;

  // Put the series name on the clipboard so it can be pasted straight into a
  // source's own search box. Not awaited: a slow or failed copy must never
  // hold up opening the row's tabs. In kenmei mode the first Y then replaces
  // it with the captured link list.
  copyToClipboard(title).catch(() => {});

  for (let i = 0; i < urls.length; i++) {
    const tab = await chrome.tabs.create({
      url: urls[i],
      windowId: originTab.windowId,
      index: originTab.index + 1 + i,
      active: i === 0
    });
    const entry = { tabId: tab.id, site: siteFor(urls[i]) };
    state.sourceTabs.push(entry);
    lastRowTabs.push(entry);
  }
  broadcastState();
}

// Handles both Y (capturedUrl set) and U (capturedUrl null), plus a tab
// closed by hand (Ctrl+W) via the onRemoved safety net below. Once the last
// tab resolves, tells the origin tab to submit (import mode) or that the
// list is final (kenmei mode) - no separate confirm step either way.
function resolveTab(tabId, capturedUrl) {
  if (!state) return;
  const idx = state.sourceTabs.findIndex((t) => t.tabId === tabId);
  if (idx === -1) return;
  state.sourceTabs.splice(idx, 1);
  if (capturedUrl) state.capturedUrls.push(capturedUrl);
  chrome.tabs.remove(tabId).catch(() => {});

  const { mode, title, total } = state;
  const urls = state.capturedUrls.slice();
  const allResolved = state.sourceTabs.length === 0;
  if (allResolved) {
    state = null;
  } else {
    broadcastState();
  }

  if (copiesToClipboard(mode) && capturedUrl) {
    copyToClipboard(urls.join(', '));
  }

  const target = originTabId(mode);
  if (target != null) {
    chrome.tabs.sendMessage(target, { type: 'updateUrls', mode, title, total, urls, submit: allResolved }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  switch (msg.type) {
    case 'registerImportTab':
      if (sender.tab) importTabId = sender.tab.id;
      return;
    case 'registerKenmeiTab':
      if (sender.tab) kenmeiTabId = sender.tab.id;
      return;
    case 'registerDashboardTab':
      if (sender.tab) dashboardTabId = sender.tab.id;
      return;
    case 'startRow':
      if (sender.tab) startRow(msg.title, msg.urls, sender.tab, msg.mode || 'import');
      return;
    case 'startNextRow': {
      // "I" pressed on a source tab - relay to whichever page started the
      // row still in progress, else whichever page started the last one
      // (the import page if nothing has run yet).
      const target = originTabId(state ? state.mode : lastRowMode);
      if (target != null) chrome.tabs.sendMessage(target, { type: 'startNextRow' }).catch(() => {});
      return;
    }
    case 'capture':
      if (sender.tab) resolveTab(sender.tab.id, msg.url);
      return;
    case 'skip':
      if (sender.tab) resolveTab(sender.tab.id, null);
      return;
    case 'kenmeiSourcesKnown': {
      // content_kenmei.js learned which of the row's sources Kenmei's own
      // dropdown already lists - pass a per-tab yes/no to each of the row's
      // tabs so it can show a red/green dot. Uses lastRowTabs, not
      // state.sourceTabs - this arrives well after the row started, often
      // after every tab has already been resolved (Y/U) by a fast user, by
      // which point state.sourceTabs is empty or state itself is null.
      // Tabs already closed just no-op on sendMessage. Also stashed in
      // lastRowKenmeiSites so a tab that missed this push can still pull it
      // via 'getKenmeiSourceInfo' below.
      if (lastRowMode !== 'kenmei') return;
      lastRowKenmeiSites = new Set(msg.sites || []);
      for (const t of lastRowTabs) {
        chrome.tabs.sendMessage(t.tabId, { type: 'kenmeiSourceKnown', hasSource: lastRowKenmeiSites.has(t.site) }).catch(() => {});
      }
      return;
    }
    case 'getKenmeiSourceInfo': {
      // Pulled by a source tab on load, in case the one-shot push above
      // already happened (or never will, for this tab) before it was ready
      // to receive it - most notably Kagane, whose Cloudflare Turnstile
      // page reloads into the real page shortly after opening, tearing down
      // whatever content script was there to receive the earlier push.
      if (!sender.tab || lastRowMode !== 'kenmei' || !lastRowKenmeiSites) {
        sendResponse(null);
        return;
      }
      const entry = lastRowTabs.find((t) => t.tabId === sender.tab.id);
      sendResponse(entry ? { hasSource: lastRowKenmeiSites.has(entry.site) } : null);
      return;
    }
    default:
      return;
  }
});

// Safety net: closing a source tab by hand (Ctrl+W) counts as a skip.
chrome.tabs.onRemoved.addListener((tabId) => {
  if (!state) return;
  if (state.sourceTabs.some((t) => t.tabId === tabId)) resolveTab(tabId, null);
});
