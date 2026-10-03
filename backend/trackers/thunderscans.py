# backend/trackers/thunderscans.py

import html
import re
import time
import threading
from datetime import datetime
import requests

from .redirects import watch_redirects, SiteRedirectError

# Redirected to another site = down or moved, said straight away
_session = watch_redirects(requests.Session())
_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
})

_last_call = 0
_last_call_lock = threading.Lock()
_MIN_DELAY = 0.4
_MAX_RETRIES = 3

SITE_BASE = "https://en-thunderscans.com"

_CHAPTER_RE = re.compile(r'<li data-num="([^"]*)"(.*?)</li>', re.DOTALL)
# data-num can carry a label after the number: "25-[S1 END]", "1-[PRE-RELEASE]"
_NUM_RE = re.compile(r'\s*(\d+(?:\.\d+)?)')
_HREF_RE = re.compile(r'<a\s[^>]*?href="(https?://[^"#]+)"')
_DATE_RE = re.compile(r'<span class="chapterdate">\s*([^<]*?)\s*</span>')
_TITLE_RE = re.compile(r'<h1 class="entry-title"[^>]*>(.*?)</h1>', re.DOTALL)
_ALT_RE = re.compile(r'<div class="desktop-titles">(.*?)</div>', re.DOTALL)
_INFO_RE = re.compile(r'<div class="imptdt">\s*<h1>\s*([^<]*?)\s*</h1>\s*<i>(.*?)</i>', re.DOTALL)
_GENRES_RE = re.compile(r'<span class="mgen">(.*?)</span>', re.DOTALL)
_TAG_RE = re.compile(r'<a [^>]*rel="tag"[^>]*>(.*?)</a>', re.DOTALL)
_COVER_RE = re.compile(r'<div class="thumb"[^>]*>\s*<img [^>]*?src="([^"]+)"', re.DOTALL)


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
        except SiteRedirectError:
            raise
        except Exception:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    raise Exception("Max retries exceeded")


def extract_series_id(url):
    """Extract the Thunderscans series slug from a URL like
    https://en-thunderscans.com/comics/god-of-blackfield/. Chapter links
    (/<slug>-chapter-326/) aren't accepted: they sit outside /comics/ and
    their slug isn't guaranteed to be the series' own."""
    match = re.search(r'en-thunderscans\.com/comics/([a-z0-9][a-z0-9_-]*)', url, re.I)
    return match.group(1).lower() if match else None


def _text(fragment):
    return html.unescape(re.sub(r'<[^>]+>', '', fragment or '')).strip()


def _parse_date(text):
    """'October 3, 2026' -> '2026-10-03T00:00:00Z'. The site only shows a
    day, no time; None if it's missing or in a format we don't know."""
    try:
        return datetime.strptime(text.strip(), '%B %d, %Y').strftime('%Y-%m-%dT00:00:00Z')
    except (ValueError, AttributeError):
        return None


def get_series_info(slug):
    """
    Fetch series metadata and chapters from Thunderscans (no auth, no browser
    automation, no Cloudflare challenge - a plain request works). Returns a
    dict shaped like flamecomics.get_series_info's return value: {title,
    cover_url, status, chapters, alt_titles, genres, content_rating,
    source_type}.
    Raises on a genuine fetch failure so callers can tell "broken" from
    "legitimately nothing new" instead of getting None for both.

    Thunderscans is a WordPress site whose REST API is switched off
    (/wp-json/ answers rest_no_route for every route), so this reads the
    server-rendered series page, which lists every chapter.

    Only free chapters are kept. The newest chapters of most series are
    coin-locked early access: they're listed with a date but have no link
    (an <a data-bs-toggle="modal"> that opens the "locked chapter" dialog
    instead), and only become readable weeks later.
    """
    if not slug:
        raise ValueError("slug is required")

    try:
        resp = _delayed_get(f"{SITE_BASE}/comics/{slug}/")
        if resp.status_code != 200:
            raise Exception(f"Thunderscans returned HTTP {resp.status_code} for series {slug}")
        page = resp.text

        title_match = _TITLE_RE.search(page)
        if not title_match:
            raise Exception(f"Thunderscans page for {slug} had no series data")
        title = _text(title_match.group(1)) or 'Unknown Title'

        # A chapter number can in principle be listed twice - keep the
        # earliest posting, both its link and its date, so a repost can't
        # make an already-out chapter look freshly dropped.
        by_number = {}
        for num, body in _CHAPTER_RE.findall(page):
            href = _HREF_RE.search(body)
            if not href:
                continue  # coin-locked
            num_match = _NUM_RE.match(num)
            if not num_match:
                continue
            chapter_number = float(num_match.group(1))
            date_match = _DATE_RE.search(body)
            release_date = _parse_date(date_match.group(1)) if date_match else None
            existing = by_number.get(chapter_number)
            if existing is None or (release_date and (not existing['release_date'] or release_date < existing['release_date'])):
                by_number[chapter_number] = {
                    'chapter_number': chapter_number,
                    'title': None,
                    'release_date': release_date,
                    'chapter_url': href.group(1),
                    'is_oneshot': False
                }
        chapters = sorted(by_number.values(), key=lambda c: c['chapter_number'])

        info = {k.strip().lower(): _text(v) for k, v in _INFO_RE.findall(page)}

        status_map = {
            'ongoing': 'reading',
            'completed': 'completed',
            'hiatus': 'on_hold',
            'dropped': 'dropped',
            'cancelled': 'dropped',
            'canceled': 'dropped',
        }
        # Anything else unrecognised: plan_to_read, which the scheduler
        # treats as "no information" rather than a real status.
        status = status_map.get(info.get('status', '').lower(), 'plan_to_read')

        if len(chapters) == 1 and chapters[0]['chapter_number'] == 0 and status == 'completed':
            chapters[0]['is_oneshot'] = True

        genres_match = _GENRES_RE.search(page)
        genres = []
        for g in _TAG_RE.findall(genres_match.group(1)) if genres_match else []:
            g = _text(g)
            if g and g not in genres:
                genres.append(g)

        # "Novel" (Thunderscans also hosts light novels) and anything else
        # unrecognised: 'other'.
        type_map = {
            'manga': 'manga',
            'manhwa': 'manhwa',
            'manhua': 'manhua',
        }
        source_type = type_map.get(info.get('type', '').lower(), 'other')

        alt_titles = []
        alt_match = _ALT_RE.search(page)
        if alt_match:
            # Listed as "A | B | C" or "A / B"; commas are part of titles
            for t in re.split(r'\s*[/|]\s*|\n', _text(alt_match.group(1))):
                t = t.strip()
                if t and t.casefold() != title.casefold() and t not in alt_titles:
                    alt_titles.append(t)

        cover_match = _COVER_RE.search(page)
        cover_url = html.unescape(cover_match.group(1)) if cover_match else None

        # Thunderscans has no content-rating field, only its genres, so this
        # is best-effort and ranks below every source with a real rating in
        # the multi-source rating merge (see SOURCE_RATING_PRIORITY).
        tag_names = {g.lower() for g in genres}
        if tag_names & {'mature', 'adult', 'smut'}:
            content_rating = 'mature'
        elif 'ecchi' in tag_names:
            content_rating = 'mild'
        else:
            content_rating = 'safe'

        return {
            'title': title,
            'cover_url': cover_url,
            'status': status,
            'chapters': chapters,
            'alt_titles': alt_titles,
            'genres': genres,
            'content_rating': content_rating,
            'source_type': source_type
        }
    except Exception as e:
        print(f"[Thunderscans] Error fetching series {slug}: {e}")
        raise
