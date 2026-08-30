# backend/trackers/asura.py

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

API_BASE = "https://api.asurascans.com/api"
SITE_BASE = "https://asurascans.com"

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
    """Extract the AsuraScans series slug from a comics URL like
    https://asurascans.com/comics/raising-villains-the-right-way-b57aa235
    (also matches a chapter URL: .../comics/<slug>/chapter/<n>)."""
    match = re.search(r'asurascans\.com/comics/([a-zA-Z0-9-]+)', url)
    return match.group(1) if match else None

def get_series_info(slug):
    """
    Fetch series metadata and chapters from AsuraScans' JSON API (no auth,
    no browser automation needed). Returns a dict shaped like
    atsu.get_series_info's return value:
    {title, cover_url, status, chapters, alt_titles, genres, content_rating, source_type}
    Raises on a genuine fetch failure (network error, bad status, missing
    body) so callers - the scheduler in particular - can tell "broken" from
    "legitimately nothing new" instead of getting None for both.

    Premium/early-access chapters (is_premium) are dropped immediately and
    never turned into stored chapter rows -- they aren't freely readable, so
    we don't want them showing up or linking to a paywall.
    """
    if not slug:
        raise ValueError("slug is required")

    try:
        series_resp = _delayed_get(f"{API_BASE}/series/{slug}")
        if series_resp.status_code != 200:
            raise Exception(f"AsuraScans API returned HTTP {series_resp.status_code} for series {slug}")
        # Normally {"series": {...}} at the top level, but AsuraScans
        # serves at least dropped/delisted series wrapped an extra level
        # deep as {"data": {"series": {...}}} instead - check both shapes.
        payload = series_resp.json()
        s = payload.get('series') or (payload.get('data') or {}).get('series')
        if not s:
            raise Exception(f"AsuraScans API returned no series data for {slug}")

        # The API accepts either the plain slug or the hash-suffixed public
        # one, but the series' own 'slug'/'public_url' are the canonical
        # values -- use those instead of whatever the user happened to paste.
        canonical_slug = s.get('slug') or slug
        public_url = s.get('public_url') or f"/comics/{slug}"

        # The chapter list is a secondary call - if it fails, keep going with
        # empty chapters rather than treating the whole source as broken
        # (metadata itself already succeeded).
        chapters_resp = _delayed_get(f"{API_BASE}/series/{canonical_slug}/chapters")
        raw_chapters = []
        if chapters_resp.status_code == 200:
            raw_chapters = chapters_resp.json().get('data') or []

        chapters = []
        for ch in raw_chapters:
            if ch.get('is_premium'):
                continue
            number = ch.get('number')
            if number is None:
                continue
            chapters.append({
                'chapter_number': float(number),
                'title': None,
                'release_date': ch.get('published_at'),
                'chapter_url': f"{SITE_BASE}{public_url}/chapter/{number}",
                'is_oneshot': False
            })
        chapters.sort(key=lambda c: c['chapter_number'])

        # Same heuristic as atsu.py/mangadex.py: a lone chapter 0 with
        # nothing else published is a oneshot.
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
        status = status_map.get((s.get('status') or '').lower(), 'plan_to_read')

        genres = [g['name'] for g in (s.get('genres') or []) if g.get('name')]

        type_map = {
            'manga': 'manga',
            'manhwa': 'manhwa',
            'manhua': 'manhua',
        }
        source_type = type_map.get((s.get('type') or '').lower(), 'other')

        alt_titles = [t for t in (s.get('alt_titles') or []) if t]

        return {
            'title': s.get('title') or 'Unknown Title',
            'cover_url': s.get('cover'),
            'status': status,
            'chapters': chapters,
            'alt_titles': alt_titles,
            'genres': genres,
            # AsuraScans has no content-rating system at all -- everything is
            # treated as safe, and this source is also given the lowest
            # priority in the multi-source rating merge (see
            # SOURCE_RATING_PRIORITY in main.py) so a "safe" from here can
            # never override a stricter rating from another source.
            'content_rating': 'safe',
            'source_type': source_type
        }
    except Exception as e:
        print(f"[AsuraScans] Error fetching series {slug}: {e}")
        raise
