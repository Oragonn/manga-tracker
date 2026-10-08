// "Already tracked" badges on kenmei.co's search and discovery pages.
//
// Every series card linking to /series/<slug> (and every slide of
// Discovery's top carousel) is checked against the
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

  function onListPage() {
    return /^\/(search|discovery)(\/|$)/.test(location.pathname);
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
  let indexLoaded = false;
  let indexRequest = null;
  let noticeShown = false;

  function buildIndex(series) {
    byTitle = new Map();
    byDespaced = new Map();
    for (const s of series) {
      s.mainTitle = s.titles[0];
      s.titleSet = new Set(s.titles);
      for (const t of s.titles) {
        if (!byTitle.has(t)) byTitle.set(t, []);
        byTitle.get(t).push(s);
        const despaced = t.replace(/ /g, '');
        if (!byDespaced.has(despaced)) byDespaced.set(despaced, []);
        byDespaced.get(despaced).push(s);
      }
    }
  }

  function loadIndex(fresh) {
    if (indexRequest) return indexRequest;
    indexRequest = new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: 'getTrackerIndex', fresh }, (res) => {
          if (chrome.runtime.lastError || !res) {
            log('could not reach the extension:', chrome.runtime.lastError && chrome.runtime.lastError.message);
            resolve(false);
            return;
          }
          if (!res.ok) {
            log('tracker library unavailable:', res.error, res.origin || '');
            showNotice(res.error === 'no-origin'
              ? 'Manga Tracker: open your tracker once in this browser so the extension learns its address.'
              : `Manga Tracker: couldn’t load your library from ${res.origin} (${res.error}).`);
            resolve(false);
            return;
          }
          trackerOrigin = res.origin;
          buildIndex(res.series);
          indexLoaded = true;
          log(`library loaded: ${res.series.length} series from ${res.origin}`);
          resolve(true);
        });
      } catch (err) {
        // "Extension context invalidated" - the extension was reloaded
        // after this tab loaded.
        log('extension was reloaded, reload this tab (F5):', err && err.message);
        resolve(false);
      }
    }).finally(() => { indexRequest = null; });
    return indexRequest;
  }

  function showNotice(text) {
    if (noticeShown || !onListPage()) return;
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
    setTimeout(() => el.remove(), 10000);
  }

  // --- Kenmei's own data for each card, from content_kenmei_hook.js ---

  const kenmeiSeries = new Map(); // slug -> [normalised titles], main first
  const kenmeiByTitle = new Map(); // normalised main title -> the same list

  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.source !== HOOK_SOURCE) return;
    for (const item of e.data.series || []) {
      const titles = [];
      for (const raw of [item.title, ...(item.alternativeTitles || [])]) {
        const t = normalizeTitle(decodeEntities(raw));
        if (t && !titles.includes(t)) titles.push(t);
      }
      if (titles.length) {
        kenmeiSeries.set(item.slug, titles);
        kenmeiByTitle.set(titles[0], titles);
      }
    }
    scheduleScan();
  });

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
    if (known) return matchTitles(known);
    const visible = normalizeTitle((anchor.textContent || '').trim());
    if (visible) {
      const found = matchTitles([visible]);
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
    return matchTitles(kenmeiByTitle.get(title) || [title]);
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
    if (match.rank === 2) lines.push(`(matched by the alternate title “${match.via}”)`);
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
  function placeBadges(cover, anchor, match) {
    const s = match.series;
    const key = `${s.id}|${s.status}|${s.current_chapter}|${s.latest_chapter}|${match.rank}`;
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
      const wasList = lastPath !== null && /^\/(search|discovery)(\/|$)/.test(lastPath);
      lastPath = location.pathname;
      // Back on a list page after a while elsewhere: pick up series added
      // to the tracker since (background.js caches for 30s).
      if (onListPage() && indexLoaded && !wasList) loadIndex(false).then((ok) => ok && scheduleScan());
    }
    if (!onListPage()) return;
    if (!indexLoaded) {
      loadIndex(false).then((ok) => ok && scheduleScan());
      return;
    }
    injectStyles();

    const seen = new Set(); // covers and title links handled this pass
    for (const anchor of document.querySelectorAll('a[href*="/series/"]')) {
      const slug = slugOf(anchor);
      if (!slug) continue;
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
    if (document.visibilityState === 'visible' && onListPage() && indexLoaded) {
      loadIndex(false).then((ok) => ok && scheduleScan());
    }
  });

  scheduleScan();
})();
