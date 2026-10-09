// "Already tracked" badges on kenmei.co's search, discovery and series
// pages.
//
// Every series card linking to /series/<slug> (and every slide of
// Discovery's top carousel, and the series a /series/ page is about) is
// checked against the
// tracker's own library (background.js fetches /api/series/title-index from
// the tracker) and, when the tracker already has it, gets a badge on its
// cover with its status and progress there - e.g. "✓ Reading · 45/120".
// Clicking the badge opens the tracker dashboard searched for that series.
//
// Matching is by title, the same way the tracker's Add Series duplicate
// check does it (search_utils.comparable_titles): every title normalised
// (normalizeTitle below mirrors normalize_search_text), and a series
// matches if any of Kenmei's titles for it equals any of the tracker's.
// Kenmei's cards only show one title, so content_kenmei_hook.js (in the
// page's own context) passes on the alternativeTitles from the API
// responses behind them; a card that arrived without those falls back to
// its visible title, then to its slug.
//
// Kenmei is a single-page app, so this script runs on every kenmei.co page
// and checks the route on each pass rather than at load.

(function () {
  const HOOK_SOURCE = 'manga-tracker-kenmei-hook';
  const SERIES_PATH = /^\/series\/([^/?#]+)/;
  const MIN_TITLE_LENGTH = 3; // search_utils.DUPLICATE_MIN_TITLE_LENGTH
  const PLACEHOLDER_TITLES = new Set(['untitled', 'unknown title', 'unknown manga']);
  const STATUS_LABELS = {
    reading: 'Reading',
    plan_to_read: 'Plan to read',
    on_hold: 'On hold',
    dropped: 'Dropped',
    completed: 'Completed'
  };

  function log(...args) {
    console.log('[Kenmei tracked]', ...args);
  }

  function isSupportedPath(path) {
    return /^\/(search|discovery)(\/|$)/.test(path) || SERIES_PATH.test(path);
  }

  function onSupportedPage() {
    return isSupportedPath(location.pathname);
  }

  // The series a /series/<slug> page (or one of its tabs) is about.
  function seriesPageSlug() {
    const m = SERIES_PATH.exec(location.pathname);
    return m ? decodeURIComponent(m[1]) : null;
  }

  // --- title normalising: a port of search_utils.normalize_search_text ---

  const APOSTROPHES = new Set(["'", '’', '‘', '‛', 'ʼ', '´', '`', '′']);
  const COMBINING = /\p{Mn}/u;
  const BREAK = /[\p{P}\p{S}\p{Zs}\p{Zl}\p{Zp}\p{Cc}]/u;
  const INVISIBLE = /\p{C}/u;

  function normalizeTitle(text) {
    if (!text) return '';
    const folded = String(text).normalize('NFKC').toLowerCase().normalize('NFD');
    let out = '';
    for (const ch of folded) {
      if (APOSTROPHES.has(ch) || COMBINING.test(ch)) continue;
      if (BREAK.test(ch)) out += ' ';
      else if (!INVISIBLE.test(ch)) out += ch;
    }
    return out.split(/\s+/).filter(Boolean).join(' ');
  }

  function isComparable(normalised) {
    return Array.from(normalised).length >= MIN_TITLE_LENGTH && !PLACEHOLDER_TITLES.has(normalised);
  }

  // Kenmei's API titles are HTML-escaped ("Love &amp; War"); its own cards
  // decode them before showing them.
  function decodeEntities(text) {
    if (!text || !text.includes('&')) return text;
    return new DOMParser().parseFromString(text, 'text/html').documentElement.textContent;
  }

  // --- the tracker's library ---

  let trackerOrigin = null;
  let byTitle = new Map(); // normalised title -> [tracker series]
  let byDespaced = new Map(); // same, spaces removed - only for slug fallbacks
  let byWords = new Map(); // a title's words, sorted -> [tracker series]
  let byLink = new Map(); // sourceKey() of a source link -> tracker series
  let indexLoaded = false;
  let indexRequest = null;
  let noticeShown = false;
  let noticeEl = null;

  function addTo(map, key, s) {
    if (!map.has(key)) map.set(key, []);
    const list = map.get(key);
    if (!list.includes(s)) list.push(s);
  }

  // "love tears zombies apart" and "love tears apart zombies" -> the same
  // key. Only for titles of 2+ words; a single word is left to the exact
  // comparison.
  function wordsKey(title) {
    const words = Array.from(new Set(title.split(' '))).sort();
    return words.length >= 2 ? words.join(' ') : null;
  }

  function buildIndex(series) {
    byTitle = new Map();
    byDespaced = new Map();
    byWords = new Map();
    byLink = new Map();
    for (const s of series) {
      s.titles = s.titles || [];
      s.mainTitle = s.titles[0];
      s.titleSet = new Set(s.titles);
      for (const t of s.titles) {
        addTo(byTitle, t, s);
        addTo(byDespaced, t.replace(/ /g, ''), s);
        const words = wordsKey(t);
        if (words) addTo(byWords, words, s);
      }
      for (const link of s.links || []) {
        if (!byLink.has(link)) byLink.set(link, s);
      }
    }
  }

  // Asks background.js for the library. The first ask takes its saved copy
  // from the last session if the in-memory one is gone (browser just
  // started), so badges show at once; that answer comes back `stale` and a
  // second ask then waits for the refresh background.js started, and the
  // badges are redrawn from it. A failed load (tracker unreachable, e.g.
  // the network not up yet right after the PC starts) is retried a few
  // times instead of waiting for the page to change.
  const RETRY_DELAYS = [2000, 5000, 15000, 30000, 60000];
  let retryCount = 0;
  let retryTimer = null;

  function requestIndex(fresh, allowStale) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: 'getTrackerIndex', fresh, allowStale }, (res) => {
          if (chrome.runtime.lastError || !res) {
            log('could not reach the extension:', chrome.runtime.lastError && chrome.runtime.lastError.message);
            resolve(null);
            return;
          }
          resolve(res);
        });
      } catch (err) {
        // "Extension context invalidated" - the extension was reloaded
        // after this tab loaded.
        log('extension was reloaded, reload this tab (F5):', err && err.message);
        resolve(null);
      }
    });
  }

  function applyIndex(res) {
    trackerOrigin = res.origin;
    buildIndex(res.series);
    indexLoaded = true;
    retryCount = 0;
    if (noticeEl) noticeEl.remove(); // a "couldn't load, retrying" one
    log(`library loaded: ${res.series.length} series from ${res.origin}${res.stale ? ' (saved copy, refreshing)' : ''}`);
  }

  function retryLater() {
    if (retryTimer || retryCount >= RETRY_DELAYS.length) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      if (onSupportedPage()) loadIndex(false).then((ok) => ok && scheduleScan());
    }, RETRY_DELAYS[retryCount++]);
  }

  function loadIndex(fresh) {
    if (indexRequest) return indexRequest;
    indexRequest = requestIndex(fresh, !indexLoaded).then((res) => {
      if (!res) return false;
      if (!res.ok) {
        log('tracker library unavailable:', res.error, res.origin || '');
        if (res.error === 'no-origin') {
          showNotice('Manga Tracker: open your tracker once in this browser so the extension learns its address.');
        } else {
          if (!indexLoaded) {
            showNotice(`Manga Tracker: couldn’t load your library from ${res.origin} (${res.error}) - retrying.`);
          }
          retryLater();
        }
        return false;
      }
      applyIndex(res);
      if (res.stale) {
        requestIndex(false, false).then((fresher) => {
          if (fresher && fresher.ok) {
            applyIndex(fresher);
            scheduleScan();
          } else {
            retryLater();
          }
        });
      }
      return true;
    }).finally(() => { indexRequest = null; });
    return indexRequest;
  }

  function showNotice(text) {
    if (noticeShown || !onSupportedPage()) return;
    noticeShown = true;
    const el = document.createElement('div');
    el.textContent = text;
    el.style.cssText = [
      'position:fixed', 'bottom:16px', 'left:16px', 'z-index:999999',
      'background:#141b2f', 'border:1px solid #334155', 'border-radius:10px',
      'padding:10px 14px', 'color:#e2e8f0', 'font:13px system-ui,sans-serif',
      'max-width:320px', 'box-shadow:0 4px 16px rgba(0,0,0,.4)', 'cursor:pointer'
    ].join(';');
    el.addEventListener('click', () => el.remove());
    document.body.appendChild(el);
    noticeEl = el;
    setTimeout(() => el.remove(), 10000);
  }

  // --- Kenmei's own data for each card, from content_kenmei_hook.js ---

  // slug -> { titles: [normalised, main first], links: [sourceKey()s] }.
  // Search results carry no source links; a series page's data does, so
  // what's known about a slug is merged rather than replaced.
  const kenmeiSeries = new Map();
  const kenmeiByTitle = new Map(); // normalised main title -> the same entry

  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.source !== HOOK_SOURCE) return;
    for (const item of e.data.series || []) {
      const entry = kenmeiSeries.get(item.slug) || { titles: [], links: [] };
      for (const raw of [item.title, ...(item.alternativeTitles || [])]) {
        const t = normalizeTitle(decodeEntities(raw));
        if (t && !entry.titles.includes(t)) entry.titles.push(t);
      }
      for (const url of item.links || []) {
        const key = sourceKey(url);
        if (key && !entry.links.includes(key)) entry.links.push(key);
      }
      if (entry.titles.length || entry.links.length) {
        kenmeiSeries.set(item.slug, entry);
        if (entry.titles.length) kenmeiByTitle.set(entry.titles[0], entry);
      }
    }
    scheduleScan();
  });

  // A port of source_links.parse_source_link: "site:id" for a link to a
  // series on one of the tracked sites, else null. The tracker sends its
  // series' links in the same form.
  const LINK_PREFIX = '^(?:https?://)?(?:www\\.)?';
  const UUID = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';
  const LINK_PATTERNS = [
    ['mangadex', new RegExp(LINK_PREFIX + 'mangadex\\.org/(?:title|manga)/(' + UUID + ')', 'i')],
    ['kagane', new RegExp(LINK_PREFIX + 'kagane\\.(?:to|org)/series/(' + UUID + ')', 'i')],
    ['atsu', new RegExp(LINK_PREFIX + 'atsu\\.moe/(?:manga|read)/([A-Za-z0-9_-]+)', 'i')],
    ['asura', new RegExp(LINK_PREFIX + 'asurascans\\.com/comics/([A-Za-z0-9-]+)', 'i')],
    ['hive', new RegExp(LINK_PREFIX + 'hivetoons\\.org/series/([A-Za-z0-9-]+)', 'i')],
    ['flame', new RegExp(LINK_PREFIX + 'flamecomics\\.xyz/series/(\\d+)', 'i')],
    ['thunder', new RegExp(LINK_PREFIX + 'en-thunderscans\\.com/comics/([A-Za-z0-9_-]+)', 'i')],
    ['comix', new RegExp(LINK_PREFIX + 'comix\\.to/title/([A-Za-z0-9]+)', 'i')]
  ];
  const CASE_INSENSITIVE_IDS = new Set(['mangadex', 'kagane', 'thunder']);
  const SITE_NAMES = {
    mangadex: 'MangaDex', kagane: 'Kagane', atsu: 'Atsumaru', asura: 'AsuraScans',
    hive: 'HiveToons', flame: 'Flame Comics', thunder: 'Thunderscans', comix: 'Comix'
  };

  function sourceKey(url) {
    const text = String(url || '').trim();
    for (const [site, pattern] of LINK_PATTERNS) {
      const m = pattern.exec(text);
      if (!m) continue;
      let id = m[1];
      if (CASE_INSENSITIVE_IDS.has(site)) id = id.toLowerCase();
      else if (site === 'asura') id = id.replace(/-[0-9a-f]{8}$/i, '');
      return `${site}:${id}`;
    }
    return null;
  }

  // A tracker series sharing a source link: the surest match there is, and
  // the one that still works when the titles have nothing in common.
  function matchLinks(links) {
    for (const link of links || []) {
      const s = byLink.get(link);
      if (s) return { series: s, rank: -1, via: SITE_NAMES[link.split(':')[0]] || link };
    }
    return null;
  }

  // No title in common: one with the same words in another order ("Love
  // Tears Apart Zombies" / "Love Tears Zombies Apart").
  function matchWordSets(titles) {
    for (const t of titles.filter(isComparable)) {
      const key = wordsKey(t);
      const hits = key ? byWords.get(key) : null;
      if (hits) return { series: hits[0], rank: 3, via: t };
    }
    return null;
  }

  function matchKenmei(entry) {
    return matchLinks(entry.links) || matchTitles(entry.titles) || matchWordSets(entry.titles);
  }

  // The tracker series best matching these Kenmei titles (main title
  // first), ranked like search_utils.same_title_rank: same main title, then
  // one's main title among the other's titles, then alternates only.
  function matchTitles(titles) {
    const wanted = titles.filter(isComparable);
    if (!wanted.length) return null;
    const main = wanted[0] === titles[0] ? wanted[0] : null;
    const wantedSet = new Set(wanted);
    let best = null;
    for (const t of wanted) {
      for (const s of byTitle.get(t) || []) {
        let rank = 2;
        if (main && s.mainTitle === main) rank = 0;
        else if ((main && s.titleSet.has(main)) || wantedSet.has(s.mainTitle)) rank = 1;
        if (!best || rank < best.rank) best = { series: s, rank, via: t };
      }
    }
    return best;
  }

  function matchCard(slug, anchor) {
    const known = kenmeiSeries.get(slug);
    if (known) return matchKenmei(known);
    const visible = normalizeTitle((anchor.textContent || '').trim());
    if (visible) {
      const found = matchTitles([visible]) || matchWordSets([visible]);
      if (found) return found;
    }
    // Last resort: Kenmei's slug is its title lower-cased with every other
    // character a dash ("a-returner-s-magic"), which lines up with a
    // tracker title once both lose their spaces.
    const despaced = slug.toLowerCase().replace(/[^a-z0-9]/g, '');
    const hits = despaced.length >= 6 ? byDespaced.get(despaced) : null;
    return hits ? { series: hits[0], rank: 2, via: slug } : null;
  }

  // Discovery's top carousel: its slides aren't links (a click navigates
  // from script), so the series is found by the title they show.
  function matchSlide(slide) {
    const heading = slide.querySelector('h3');
    const img = slide.querySelector('img[alt]');
    const shown = (heading && heading.textContent.trim())
      || (img ? img.getAttribute('alt').replace(/^Cover for\s+/i, '').trim() : '');
    const title = normalizeTitle(shown);
    if (!title) return null;
    const known = kenmeiByTitle.get(title);
    return known ? matchKenmei(known) : (matchTitles([title]) || matchWordSets([title]));
  }

  // --- badges ---

  function injectStyles() {
    if (document.getElementById('mt-tracked-styles')) return;
    const style = document.createElement('style');
    style.id = 'mt-tracked-styles';
    style.textContent = `
      .mt-tracked-ring {
        position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
        box-shadow: inset 0 0 0 3px var(--mt-color); z-index: 4;
      }
      .mt-tracked-badge {
        position: absolute; top: 6px; left: 6px; z-index: 5; max-width: calc(100% - 12px);
        padding: 2px 7px; border-radius: 999px; background: var(--mt-color); color: #fff;
        font: 600 11px/1.5 system-ui, sans-serif; white-space: nowrap; overflow: hidden;
        text-overflow: ellipsis; cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,.45);
      }
      .mt-tracked-badge:hover { filter: brightness(1.12); }
      .mt-tracked-badge.mt-missing { cursor: default; filter: none; }
      .mt-tracked-badge.mt-inline {
        position: static; display: inline-block; margin: 2px 0 2px 6px; vertical-align: middle;
        max-width: 100%;
      }
    `;
    document.head.appendChild(style);
  }

  const STATUS_COLORS = {
    reading: '#16a34a',
    plan_to_read: '#2563eb',
    on_hold: '#d97706',
    dropped: '#dc2626',
    completed: '#7c3aed'
  };

  function chapterText(n) {
    if (n == null || n < 0) return null;
    return String(Math.round(n * 100) / 100);
  }

  function badgeText(s) {
    const status = STATUS_LABELS[s.status] || s.status;
    const read = chapterText(s.current_chapter);
    const latest = chapterText(s.latest_chapter);
    if (!read) return `✓ ${status}`;
    return latest && s.latest_chapter > s.current_chapter
      ? `✓ ${status} · ${read}/${latest}`
      : `✓ ${status} · ${read}`;
  }

  function badgeTooltip(match) {
    const s = match.series;
    const lines = [`In your tracker: ${s.title}`, badgeText(s).replace('✓ ', '')];
    if (match.rank === -1) lines.push(`(matched by the same ${match.via} link)`);
    else if (match.rank === 2) lines.push(`(matched by the alternate title “${match.via}”)`);
    else if (match.rank === 3) lines.push(`(matched by “${match.via}” - same words, another order)`);
    lines.push('Click to open it on the tracker dashboard');
    return lines.join('\n');
  }

  function slugOf(anchor) {
    const href = anchor.getAttribute('href') || '';
    let path;
    try {
      path = new URL(href, location.origin).pathname;
    } catch (_) {
      return null;
    }
    const m = SERIES_PATH.exec(path);
    return m ? decodeURIComponent(m[1]) : null;
  }

  // The element holding this card's cover: walk up from the link until
  // something with a cover image is found, never into a container that also
  // holds other series' links (a whole results list). Grid cards are the
  // link itself; search rows are the <li> around the title link.
  function coverFor(anchor, slug) {
    let el = anchor;
    for (let depth = 0; depth < 5 && el; depth++) {
      const pic = el.querySelector('picture') || el.querySelector('img');
      if (pic) return pic.parentElement;
      const parent = el.parentElement;
      if (!parent || parent === document.body) break;
      const others = Array.from(parent.querySelectorAll('a[href*="/series/"]')).some((a) => slugOf(a) !== slug);
      if (others) break;
      el = parent;
    }
    return null;
  }

  function openOnTracker(e, title) {
    e.preventDefault();
    e.stopPropagation();
    if (trackerOrigin) window.open(`${trackerOrigin}/dashboard?search=${encodeURIComponent(title)}`, '_blank');
  }

  const OUR_CLASSES = ['mt-tracked-badge', 'mt-tracked-ring'];

  // The pill placed right after a title link (see placeBadges), if any.
  function pillAfter(anchor) {
    const next = anchor.nextElementSibling;
    return next && next.classList.contains('mt-inline') ? next : null;
  }

  function removeBadges(holder, anchor) {
    if (holder) holder.querySelectorAll(':scope > .mt-tracked-badge, :scope > .mt-tracked-ring').forEach((el) => el.remove());
    const pill = anchor && pillAfter(anchor);
    if (pill) pill.remove();
  }

  function makeBadge(match, color, key, text, inline) {
    const badge = document.createElement('span');
    badge.className = inline ? 'mt-tracked-badge mt-inline' : 'mt-tracked-badge';
    badge.dataset.mtKey = key;
    badge.style.setProperty('--mt-color', color);
    badge.textContent = text;
    badge.title = badgeTooltip(match);
    // Inside Kenmei's card link: keep the click from opening the series
    // on Kenmei as well.
    badge.addEventListener('click', (e) => openOnTracker(e, match.series.title), true);
    badge.addEventListener('mousedown', (e) => e.stopPropagation(), true);
    badge.addEventListener('pointerdown', (e) => e.stopPropagation(), true);
    return badge;
  }

  // A ring and badge on the cover; a cover too narrow for the full label
  // (the search page's list rows) gets just a tick there, and the label
  // goes in a pill after the title link instead. No cover found at all:
  // only the pill.
  function badgeKey(match) {
    const s = match.series;
    return `${s.id}|${s.status}|${s.current_chapter}|${s.latest_chapter}|${match.rank}`;
  }

  function placeBadges(cover, anchor, match) {
    const s = match.series;
    const key = badgeKey(match);
    const width = cover ? cover.getBoundingClientRect().width : 0;
    const compact = !!anchor && (!cover || (width > 0 && width < 120));
    const coverBadge = cover && cover.querySelector(':scope > .mt-tracked-badge');
    const pill = anchor && pillAfter(anchor);
    if ((!cover || (coverBadge && coverBadge.dataset.mtKey === key))
        && (compact ? pill && pill.dataset.mtKey === key : !pill)) return;
    removeBadges(cover, anchor);

    const color = STATUS_COLORS[s.status] || '#16a34a';
    const text = badgeText(s);
    if (cover) {
      if (getComputedStyle(cover).position === 'static') cover.style.position = 'relative';
      const ring = document.createElement('div');
      ring.className = 'mt-tracked-ring';
      ring.style.setProperty('--mt-color', color);
      cover.appendChild(ring);
      cover.appendChild(makeBadge(match, color, key, compact ? '✓' : text, false));
    }
    if (compact) anchor.insertAdjacentElement('afterend', makeBadge(match, color, key, text, true));
  }

  // A series page: the full label as a pill beside the title - or, since
  // a page about one series is where "do I have this?" gets asked, a grey
  // "Not in your tracker" when nothing matched - and the ring and badge on
  // its big cover.
  function scanSeriesPage(slug, seen) {
    const h1 = document.querySelector('h1');
    const shown = h1 ? h1.textContent.trim() : '';
    const known = kenmeiSeries.get(slug);
    if (!h1 || (!known && !shown)) return;
    const match = known ? matchKenmei(known) : matchCard(slug, { textContent: shown });
    seen.add(h1);

    const key = match ? badgeKey(match) : `none|${slug}`;
    const pill = pillAfter(h1);
    if (!pill || pill.dataset.mtKey !== key) {
      if (pill) pill.remove();
      let el;
      if (match) {
        el = makeBadge(match, STATUS_COLORS[match.series.status] || '#16a34a', key, badgeText(match.series), true);
      } else {
        el = document.createElement('span');
        el.className = 'mt-tracked-badge mt-inline mt-missing';
        el.dataset.mtKey = key;
        el.style.setProperty('--mt-color', '#64748b');
        el.textContent = 'Not in your tracker';
        el.title = 'No series in your tracker shares a title with this one';
      }
      h1.insertAdjacentElement('afterend', el);
    }

    const img = Array.from(document.querySelectorAll('img[alt^="Cover for"]'))
      .find((i) => !i.closest('a[href*="/series/"], .splide__slide'));
    const cover = img && ((img.closest('picture') || img).parentElement);
    if (!cover) return;
    seen.add(cover);
    if (match) placeBadges(cover, null, match);
    else removeBadges(cover, null);
  }

  let scanTimer = null;
  function scheduleScan() {
    if (scanTimer) return;
    scanTimer = setTimeout(() => {
      scanTimer = null;
      scan();
    }, 150);
  }

  let lastPath = null;
  function scan() {
    if (lastPath !== location.pathname) {
      const wasList = lastPath !== null && isSupportedPath(lastPath);
      lastPath = location.pathname;
      // Back on a list page after a while elsewhere: pick up series added
      // to the tracker since (background.js caches for 30s).
      if (onSupportedPage() && indexLoaded && !wasList) loadIndex(false).then((ok) => ok && scheduleScan());
    }
    if (!onSupportedPage()) return;
    if (!indexLoaded) {
      loadIndex(false).then((ok) => ok && scheduleScan());
      return;
    }
    injectStyles();

    const seen = new Set(); // covers and title links handled this pass
    const pageSlug = seriesPageSlug();
    if (pageSlug) scanSeriesPage(pageSlug, seen);
    for (const anchor of document.querySelectorAll('a[href*="/series/"]')) {
      const slug = slugOf(anchor);
      // Links to the page's own series are its tabs (reviews...), not cards.
      if (!slug || slug === pageSlug) continue;
      const cover = coverFor(anchor, slug);
      // A card can link to its series more than once (cover and title):
      // one set of badges per card.
      if (seen.has(cover || anchor)) continue;
      seen.add(cover || anchor);
      seen.add(anchor);
      const match = matchCard(slug, anchor);
      if (match) placeBadges(cover, anchor, match);
      else removeBadges(cover, anchor);
    }
    for (const slide of document.querySelectorAll('.splide__slide')) {
      const pic = slide.querySelector('picture') || slide.querySelector('img');
      const cover = pic && pic.parentElement;
      if (!cover || seen.has(cover)) continue;
      seen.add(cover);
      const match = matchSlide(slide);
      if (match) placeBadges(cover, null, match);
      else removeBadges(cover, null);
    }
    // Badges left behind on elements no longer showing a series link
    // (Kenmei reused the element for something else).
    document.querySelectorAll('.mt-tracked-badge').forEach((badge) => {
      if (badge.classList.contains('mt-inline')) {
        const prev = badge.previousElementSibling;
        if (!prev || !seen.has(prev)) badge.remove();
      } else if (badge.parentElement && !seen.has(badge.parentElement)) {
        removeBadges(badge.parentElement, null);
      }
    });
  }

  new MutationObserver((mutations) => {
    // Ignore the observer's own echo: mutations that only added/removed our
    // badges.
    const relevant = mutations.some((m) => {
      if (m.type === 'attributes') return true;
      const nodes = [...m.addedNodes, ...m.removedNodes];
      return nodes.some((n) => !(n.classList && OUR_CLASSES.some((c) => n.classList.contains(c))));
    });
    if (relevant) scheduleScan();
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['href'] });

  // Coming back to the tab (e.g. after adding a series on the tracker):
  // refresh the library so the new one gets its badge. Not forced -
  // background.js still serves its copy if it's under 30s old, since the
  // index is ~450 KB even gzipped.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && onSupportedPage() && indexLoaded) {
      loadIndex(false).then((ok) => ok && scheduleScan());
    }
  });

  scheduleScan();
})();
