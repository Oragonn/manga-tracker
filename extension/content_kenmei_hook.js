// Runs in kenmei.co's own page context ("world": "MAIN"), at document_start,
// before Kenmei's app code. Kenmei's search and discovery cards only show one
// title, but the API responses behind them (api.kenmei.co, e.g.
// /api/v2/series_search) carry every series' slug, title and
// alternativeTitles - far better for spotting a series the tracker already
// has under another name. This wraps XMLHttpRequest (what Kenmei's axios
// uses) and fetch, picks every {slug, title} object out of those JSON
// responses, and hands them to content_kenmei.js (the isolated-world
// script, which can't see the page's requests itself) via postMessage.
// Read-only: the responses reach Kenmei untouched.

(function () {
  const API_HOST = 'api.kenmei.co';
  const MESSAGE_SOURCE = 'manga-tracker-kenmei-hook';

  function collectSeries(node, out, depth) {
    if (!node || typeof node !== 'object' || depth > 8) return;
    if (Array.isArray(node)) {
      for (const item of node) collectSeries(item, out, depth + 1);
      return;
    }
    if (typeof node.slug === 'string' && typeof node.title === 'string') {
      out.push({
        slug: node.slug,
        title: node.title,
        alternativeTitles: Array.isArray(node.alternativeTitles)
          ? node.alternativeTitles.filter((t) => typeof t === 'string')
          : []
      });
    }
    for (const key in node) {
      const value = node[key];
      if (value && typeof value === 'object') collectSeries(value, out, depth + 1);
    }
  }

  function publish(url, text) {
    try {
      if (!url || !String(url).includes(API_HOST) || !text) return;
      const series = [];
      collectSeries(JSON.parse(text), series, 0);
      if (series.length) window.postMessage({ source: MESSAGE_SOURCE, series }, location.origin);
    } catch (_) {
      // not JSON, or not ours to understand - ignore
    }
  }

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__mtUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', () => {
      const url = this.responseURL || this.__mtUrl;
      if (this.responseType === '' || this.responseType === 'text') publish(url, this.responseText);
      else if (this.responseType === 'json' && this.response) publish(url, JSON.stringify(this.response));
    });
    return origSend.apply(this, arguments);
  };

  const origFetch = window.fetch;
  window.fetch = function () {
    const promise = origFetch.apply(this, arguments);
    promise.then((res) => {
      if (res && res.url && res.url.includes(API_HOST)) {
        res.clone().text().then((text) => publish(res.url, text)).catch(() => {});
      }
    }).catch(() => {});
    return promise;
  };
})();
