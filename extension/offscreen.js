// Offscreen document used only to write to the clipboard on behalf of the
// kenmei.co lookup flow. A background service worker has no clipboard
// access at all, and navigator.clipboard.writeText() from a source tab
// throws "Document is not focused" for the 4 tabs opened in the background
// (only the first of the 5 search tabs is made active) - execCommand('copy')
// in a hidden offscreen document sidesteps both, since it doesn't require
// document focus the way the async Clipboard API does.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.target !== 'offscreen' || msg.type !== 'copyToClipboard') return;
  const ta = document.getElementById('clipboard-relay');
  ta.value = msg.text;
  ta.select();
  document.execCommand('copy');
  sendResponse(true);
});
