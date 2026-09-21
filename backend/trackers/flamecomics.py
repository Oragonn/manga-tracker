# backend/trackers/flamecomics.py

import json
import re
import time
import threading
from datetime import datetime, timezone
from urllib.parse import quote
import requests

_session = requests.Session()
_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
})

_last_call = 0
_last_call_lock = threading.Lock()
_MIN_DELAY = 0.4
_MAX_RETRIES = 3

SITE_BASE = "https://flamecomics.xyz"
CDN_BASE = "https://cdn.flamecomics.xyz/uploads/images/series"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


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
    """Extract the Flame Comics series id from a URL like
    https://flamecomics.xyz/series/2 (also matches a chapter URL:
    .../series/<id>/<token>)."""
    match = re.search(r'flamecomics\.xyz/series/(\d+)', url)
    return match.group(1) if match else None


def _cover_url(series_id, cover):
    """The site's own resized-image URL for a cover. The raw CDN file is the
    full-size upload (a couple of MB of PNG), which is far too heavy for a
    gallery of cards; /_next/image serves the same picture at 720px, using
    the width/quality pair the site itself requests for its series pages."""
    if not cover:
        return None
    raw = f"{CDN_BASE}/{series_id}/{cover}"
    return f"{SITE_BASE}/_next/image?url={quote(raw, safe='')}&w=720&q=75"


def get_series_info(series_id):
    """
    Fetch series metadata and chapters from Flame Comics (no auth, no browser
    automation, no Cloudflare challenge - a plain request works). Returns a
    dict shaped like hivetoons.get_series_info's return value: {title,
    cover_url, status, chapters, alt_titles, genres, content_rating,
    source_type}.
    Raises on a genuine fetch failure so callers can tell "broken" from
    "legitimately nothing new" instead of getting None for both.

    Flame Comics has no public API (its /api/ routes are disallowed in
    robots.txt) - it's a Next.js site, and the series page is server-rendered
    with the full series metadata and chapter list embedded in its
    __NEXT_DATA__ block, so this reads that instead of scraping markup. The
    /_next/data/<buildId>/... JSON route would be lighter, but its buildId
    changes on every site deploy.
    """
    if not series_id:
        raise ValueError("series_id is required")

    try:
        resp = _delayed_get(f"{SITE_BASE}/series/{series_id}")
        if resp.status_code != 200:
            raise Exception(f"Flame Comics returned HTTP {resp.status_code} for series {series_id}")

        match = _NEXT_DATA_RE.search(resp.text)
        if not match:
            raise Exception(f"Flame Comics page for {series_id} had no parseable series data")
        props = json.loads(match.group(1)).get('props', {}).get('pageProps') or {}
        s = props.get('series')
        if not s:
            raise Exception(f"Flame Comics page for {series_id} had no series data")

        # The same chapter number is occasionally posted twice a few minutes
        # apart (a re-upload) - keep only the earliest posting, both its link
        # and its date, so the repost can't make an already-out chapter look
        # freshly dropped.
        by_number = {}
        for ch in props.get('chapters') or []:
            number = ch.get('chapter')
            token = ch.get('token')
            if number is None or not token:
                continue
            posted = ch.get('release_date')
            release_date = (
                datetime.fromtimestamp(posted, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
                if posted else None
            )
            chapter_number = float(number)
            existing = by_number.get(chapter_number)
            if existing is None or (release_date and (not existing['release_date'] or release_date < existing['release_date'])):
                by_number[chapter_number] = {
                    'chapter_number': chapter_number,
                    'title': ch.get('title') or None,
                    'release_date': release_date,
                    'chapter_url': f"{SITE_BASE}/series/{series_id}/{token}",
                    'is_oneshot': False
                }
        chapters = sorted(by_number.values(), key=lambda c: c['chapter_number'])

        status_map = {
            'ongoing': 'reading',
            'completed': 'completed',
            'hiatus': 'on_hold',
            'dropped': 'dropped',
            'cancelled': 'dropped',
            'canceled': 'dropped',
        }
        # "Coming Soon" and anything else unrecognised: plan_to_read, which the
        # scheduler treats as "no information" rather than a real status.
        status = status_map.get((s.get('status') or '').lower(), 'plan_to_read')

        # Same lone-chapter-0 heuristic as the other trackers, but only for a
        # finished series: Flame posts a "Prologue" as chapter 0 of ordinary
        # series, so a brand-new ongoing one can have nothing else yet.
        if len(chapters) == 1 and chapters[0]['chapter_number'] == 0 and status == 'completed':
            chapters[0]['is_oneshot'] = True

        genres = [t.strip() for t in (s.get('tags') or []) if t and t.strip()]

        type_map = {
            'manga': 'manga',
            'manhwa': 'manhwa',
            'manhua': 'manhua',
        }
        source_type = type_map.get((s.get('type') or '').lower(), 'other')

        title = (s.get('title') or '').strip() or 'Unknown Title'
        alt_titles = []
        for t in s.get('altTitles') or []:
            t = (t or '').strip()
            if t and t.casefold() != title.casefold() and t not in alt_titles:
                alt_titles.append(t)

        # Flame Comics has no content-rating field, only its own tags, so this
        # is best-effort (a "Mature" tag, or Ecchi for the milder end) and
        # ranks below every source with a real rating in the multi-source
        # rating merge (see SOURCE_RATING_PRIORITY in main.py).
        tag_names = {g.lower() for g in genres}
        if 'mature' in tag_names:
            content_rating = 'mature'
        elif tag_names & {'ecchi', 'ecchi comedy'}:
            content_rating = 'mild'
        else:
            content_rating = 'safe'

        return {
            'title': title,
            'cover_url': _cover_url(series_id, s.get('cover')),
            'status': status,
            'chapters': chapters,
            'alt_titles': alt_titles,
            'genres': genres,
            'content_rating': content_rating,
            'source_type': source_type
        }
    except Exception as e:
        print(f"[FlameComics] Error fetching series {series_id}: {e}")
        raise
