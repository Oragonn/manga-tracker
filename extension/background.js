// Kenmei Import Helper - background service worker.
//
// Owns the one-row-at-a-time state machine: which of the row's 4
// source-search tabs are still open, and which URLs have been captured
// (Y) so far. Content scripts never talk to each other directly -
// everything routes through here.
//
// importTabId is tracked independently of `state` (registered as soon as
// content_import.js loads) so "I" pressed on a source tab can still reach
// the import page even before any row has been started yet.

let importTabId = null;
let state = null; // { title, total, sourceTabs: [{tabId, site}], capturedUrls: [] }

const SITE_PATTERNS = [
  { re: /^https:\/\/mangadex\.org\//, site: 'mangadex' },
  { re: /^https:\/\/atsu\.moe\//, site: 'atsu' },
  { re: /^https:\/\/asurascans\.com\//, site: 'asura' },
  { re: /^https:\/\/kagane\.(to|org)\//, site: 'kagane' }
];

function siteFor(url) {
  const match = SITE_PATTERNS.find((p) => p.re.test(url));
  return match ? match.site : 'unknown';
}

function broadcastState() {
  if (!state || importTabId == null) return;
  chrome.tabs
    .sendMessage(importTabId, {
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

async function startRow(title, urls, importTab) {
  importTabId = importTab.id;
  // A previous row that never fully resolved (e.g. abandoned via a fresh
  // "I") - best-effort close its leftover tabs before starting the new one.
  if (state) {
    for (const t of state.sourceTabs) chrome.tabs.remove(t.tabId).catch(() => {});
  }
  state = { title, total: urls.length, sourceTabs: [], capturedUrls: [] };

  for (let i = 0; i < urls.length; i++) {
    const tab = await chrome.tabs.create({
      url: urls[i],
      windowId: importTab.windowId,
      index: importTab.index + 1 + i,
      active: i === 0
    });
    state.sourceTabs.push({ tabId: tab.id, site: siteFor(urls[i]) });
  }
  broadcastState();
}

// Handles both Y (capturedUrl set) and U (capturedUrl null), plus a tab
// closed by hand (Ctrl+W) via the onRemoved safety net below. Once the
// 4th tab resolves, tells the import tab to submit with whatever was
// accumulated - no separate confirm step.
function resolveTab(tabId, capturedUrl) {
  if (!state) return;
  const idx = state.sourceTabs.findIndex((t) => t.tabId === tabId);
  if (idx === -1) return;
  state.sourceTabs.splice(idx, 1);
  if (capturedUrl) state.capturedUrls.push(capturedUrl);
  chrome.tabs.remove(tabId).catch(() => {});

  const urls = state.capturedUrls.slice();
  const allResolved = state.sourceTabs.length === 0;
  if (allResolved) {
    state = null;
  } else {
    broadcastState();
  }
  if (importTabId != null) {
    chrome.tabs.sendMessage(importTabId, { type: 'updateUrls', urls, submit: allResolved }).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  switch (msg.type) {
    case 'registerImportTab':
      if (sender.tab) importTabId = sender.tab.id;
      return;
    case 'startRow':
      if (sender.tab) startRow(msg.title, msg.urls, sender.tab);
      return;
    case 'startNextRow':
      // "I" pressed on a source tab - relay to the import page, which
      // knows how to find/start the next pending row.
      if (importTabId != null) chrome.tabs.sendMessage(importTabId, { type: 'startNextRow' }).catch(() => {});
      return;
    case 'capture':
      if (sender.tab) resolveTab(sender.tab.id, msg.url);
      return;
    case 'skip':
      if (sender.tab) resolveTab(sender.tab.id, null);
      return;
    default:
      return;
  }
});

// Safety net: closing a source tab by hand (Ctrl+W) counts as a skip.
chrome.tabs.onRemoved.addListener((tabId) => {
  if (!state) return;
  if (state.sourceTabs.some((t) => t.tabId === tabId)) resolveTab(tabId, null);
});
