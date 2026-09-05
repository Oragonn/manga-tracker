# backend/trackers/hivetoons.py

import html
import json
import re
import time
import threading
import requests

_session = requests.Session()
_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
})

_last_call = 0
_last_call_lock = threading.Lock()
_MIN_DELAY = 0.4
_MAX_RETRIES = 3

SITE_BASE = "https://hivetoons.org"

_ISLAND_RE = re.compile(r'<astro-island\b[^>]*\bprops="([^"]*)"', re.IGNORECASE)


def _delayed_get(url, **kwargs):
    global _last_call
    for attempt in range(_MAX_RETRIES):
        # Shared with the scheduler thread and the add-series queue worker,
        # so the throttle read+write needs to be atomic (see atsu.py, which
        # hit this as a real race before the lock was added).
        with _last_call_lock:
            now = time.time()
            if now - _last_call < _MIN_DELAY:
                time.sleep(_MIN_DELAY - (now - _last_call))
            _last_call = time.time()
        try:
            resp = _session.get(url, timeout=15, **kwargs)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return resp
        except Exception:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    raise Exception("Max retries exceeded")


def extract_series_id(url):
    """Extract the HiveToons series slug from a URL like
    https://hivetoons.org/series/eleceed (also matches a chapter URL:
    .../series/<slug>/chapter-<n>)."""
    match = re.search(r'hivetoons\.org/series/([a-zA-Z0-9-]+)', url)
    return match.group(1) if match else None


def _unwrap(v):
    """Astro serializes island props as [tag, value] tuples, recursively -
    tag 0 means a plain value. Strip the wrapper to get plain Python data."""
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        return _unwrap(v[1])
    if isinstance(v, dict):
        return {k: _unwrap(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


def _find_series_payload(page_html):
    """HiveToons has no public JSON API - the series page is plain
    server-rendered HTML, but it embeds the full series metadata and
    chapter list as one Astro island's serialized props (HTML-entity-
    encoded JSON), so this reads that instead of scraping rendered markup.
    Component build hashes change on every site deploy, so the right
    island is identified by its payload shape ('post' + 'initialChap'
    keys) rather than a hardcoded component filename."""
    for match in _ISLAND_RE.finditer(page_html):
        raw = html.unescape(match.group(1))
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and 'post' in data and 'initialChap' in data:
            return _unwrap(data)
    return None


def get_series_info(slug):
    """
    Fetch series metadata and chapters from HiveToons (no auth, no browser
    automation, no Cloudflare challenge - a plain request works). Returns a
    dict shaped like asura.get_series_info's return value: {title,
    cover_url, status, chapters, alt_titles, genres, content_rating,
    source_type}.
    Raises on a genuine fetch failure so callers can tell "broken" from
    "legitimately nothing new" instead of getting None for both.

    Premium/coin-locked chapters (isAccessible == False) are dropped
    immediately and never turned into stored chapter rows, same as
    AsuraScans' is_premium handling -- they aren't freely readable, so we
    don't want them showing up or linking to a paywall.
    """
    if not slug:
        raise ValueError("slug is required")

    try:
        resp = _delayed_get(f"{SITE_BASE}/series/{slug}")
        if resp.status_code != 200:
            raise Exception(f"HiveToons returned HTTP {resp.status_code} for series {slug}")

        payload = _find_series_payload(resp.text)
        if not payload:
            raise Exception(f"HiveToons page for {slug} had no parseable series data")

        post = payload.get('post') or {}
        raw_chapters = payload.get('initialChap') or []

        chapters = []
        for ch in raw_chapters:
            if not ch.get('isAccessible', True):
                continue
            number = ch.get('number')
            if number is None:
                continue
            chapter_slug = ch.get('slug') or f"chapter-{number}"
            chapters.append({
                'chapter_number': float(number),
                'title': ch.get('title'),
                'release_date': ch.get('createdAt'),
                'chapter_url': f"{SITE_BASE}/series/{slug}/{chapter_slug}",
                'is_oneshot': False
            })
        chapters.sort(key=lambda c: c['chapter_number'])

        # Same heuristic as atsu.py/asura.py: a lone chapter 0 with nothing
        # else published is a oneshot.
        if len(chapters) == 1 and chapters[0]['chapter_number'] == 0:
            chapters[0]['is_oneshot'] = True

        status_map = {
            'ongoing': 'reading',
            'completed': 'completed',
            'hiatus': 'on_hold',
            'dropped': 'dropped',
            'cancelled': 'dropped',
            'canceled': 'dropped',
        }
        status = status_map.get((post.get('seriesStatus') or '').lower(), 'plan_to_read')

        genres = [g['name'] for g in (post.get('genres') or []) if g.get('name')]

        type_map = {
            'manga': 'manga',
            'manhwa': 'manhwa',
            'manhua': 'manhua',
        }
        source_type = type_map.get((post.get('seriesType') or '').lower(), 'other')

        title = post.get('postTitle') or 'Unknown Title'
        alt_titles_raw = post.get('alternativeTitles') or ''
        alt_titles = [t.strip() for t in alt_titles_raw.split(',') if t.strip() and t.strip() != title]

        # HiveToons has no dedicated content-rating field, just an "Adult"
        # genre tag - best-effort only, so (like AsuraScans) this source is
        # also given the lowest priority in the multi-source rating merge
        # (see SOURCE_RATING_PRIORITY in main.py) so a "safe" from here can
        # never override a stricter rating from another source.
        content_rating = 'explicit' if any(
            g.lower() in ('adult', 'hentai', 'smut') for g in genres
        ) else 'safe'

        return {
            'title': title,
            'cover_url': post.get('featuredImage'),
            'status': status,
            'chapters': chapters,
            'alt_titles': alt_titles,
            'genres': genres,
            'content_rating': content_rating,
            'source_type': source_type
        }
    except Exception as e:
        print(f"[HiveToons] Error fetching series {slug}: {e}")
        raise
