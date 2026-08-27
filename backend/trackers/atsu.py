# backend/trackers/atsu.py

import re
import time
import threading
import requests
from datetime import datetime, timezone

_session = requests.Session()
_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
})

_last_call = 0
_last_call_lock = threading.Lock()
_MIN_DELAY = 0.4
_MAX_RETRIES = 3

CDN_BASE = "https://cdn.atsu.moe/static/"

def _delayed_get(url, **kwargs):
    global _last_call
    for attempt in range(_MAX_RETRIES):
        # The scheduler thread and the add-series queue worker both call
        # into this module concurrently, so the throttle read+write needs
        # to be atomic or two threads can both see a stale _last_call and
        # fire requests back-to-back, defeating the rate limit.
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
    """Extract Atsumaru manga ID from a series URL (https://atsu.moe/manga/2VgNt)
    or a read/chapter URL (https://atsu.moe/read/2VgNt/xxxxxx) — atsu.moe often
    redirects a pasted series link straight to the latest chapter, so both
    forms need to resolve to the same manga ID."""
    match = re.search(r'https://atsu\.moe/(?:manga|read)/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else None

def _cover_url(poster):
    if not poster:
        return None
    path = poster.get('largeImage') or poster.get('mediumImage') or poster.get('image')
    return f"{CDN_BASE}{path}" if path else None

def get_series_info(manga_id):
    """
    Fetch series metadata and chapters from Atsumaru's JSON API (no auth,
    no browser automation needed). Returns a dict shaped like
    kagane.get_series_info's return value:
    {title, cover_url, status, chapters, alt_titles, genres, content_rating, source_type}
    or None on failure.
    """
    if not manga_id:
        raise ValueError("manga_id is required")

    try:
        page_resp = _delayed_get("https://atsu.moe/api/manga/page", params={'id': manga_id})
        if page_resp.status_code != 200:
            return None
        mp = page_resp.json().get('mangaPage')
        if not mp:
            return None

        chapters_resp = _delayed_get("https://atsu.moe/api/manga/allChapters", params={'mangaId': manga_id})
        raw_chapters = chapters_resp.json().get('chapters', []) if chapters_resp.status_code == 200 else mp.get('chapters', [])

        # Multiple scanlator groups can post the same chapter number; keep
        # whichever posting is most recent, same logic the scheduler already
        # uses to merge duplicate chapter numbers across sources.
        best_by_number = {}
        for ch in raw_chapters:
            number = ch.get('number')
            if number is None:
                continue
            created_at = ch.get('createdAt') or 0
            existing = best_by_number.get(number)
            if existing is None or created_at > (existing.get('createdAt') or 0):
                best_by_number[number] = ch

        chapters = []
        for number, ch in best_by_number.items():
            created_at_ms = ch.get('createdAt')
            release_date = None
            if created_at_ms:
                release_date = datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
            chapters.append({
                'chapter_number': float(number),
                'title': ch.get('title'),
                'release_date': release_date,
                'chapter_url': f"https://atsu.moe/read/{manga_id}/{ch['id']}",
                'is_oneshot': False
            })
        chapters.sort(key=lambda c: c['chapter_number'])

        # Atsumaru has no oneshot flag of its own -- a oneshot just shows up
        # as a lone "Chapter 0". Same heuristic as mangadex.py: a single
        # chapter numbered 0 with nothing else published is a oneshot.
        if len(chapters) == 1 and chapters[0]['chapter_number'] == 0:
            chapters[0]['is_oneshot'] = True

        status_map = {
            'Ongoing': 'reading',
            'Completed': 'completed',
            'Hiatus': 'on_hold',
            'Canceled': 'dropped',   # Atsumaru's UI/API use the single-L spelling
            'Cancelled': 'dropped'   # kept in case either spelling shows up
        }
        status = status_map.get(mp.get('status'), 'plan_to_read')

        genres = [g['name'] for g in mp.get('genres', []) if g.get('name')]

        # isAdult alone only distinguishes Pornographic from everything else.
        # Atsumaru's 'tags' field carries hierarchical namePaths like
        # "Sexual Content > Intensity > Ecchi" or "Sexual Content > Nudity",
        # which mirror the site's own Safe/Suggestive/Erotica/Pornographic
        # filter. Any Sexual Content tag other than the mild Ecchi intensity
        # one (Nudity, Sexual Acts, Erotica intensity, etc.) means Mature,
        # even when isAdult is false.
        tag_paths = [t.get('namePath') or '' for t in (mp.get('tags') or [])]
        sexual_paths = [p for p in tag_paths if p.startswith('Sexual Content')]
        has_heavy_sexual_content = any(
            p != 'Sexual Content > Intensity > Ecchi' for p in sexual_paths
        )
        is_hentai_genre = 'Hentai' in genres

        if mp.get('isAdult') or is_hentai_genre:
            content_rating = 'explicit'
        elif has_heavy_sexual_content:
            content_rating = 'mature'
        elif sexual_paths:
            content_rating = 'mild'
        else:
            content_rating = 'safe'

        type_map = {
            'Manga': 'manga',
            'Manwha': 'manhwa',  # Atsumaru's own data has this typo, not "Manhwa"
            'Manhwa': 'manhwa',
            'Manhua': 'manhua'
        }
        source_type = type_map.get(mp.get('type'), 'other')

        alt_titles = [t for t in (mp.get('otherNames') or []) if t]
        if mp.get('englishTitle'):
            alt_titles.append(mp['englishTitle'])

        return {
            'title': mp.get('title') or 'Unknown Title',
            'cover_url': _cover_url(mp.get('poster')),
            'status': status,
            'chapters': chapters,
            'alt_titles': alt_titles,
            'genres': genres,
            'content_rating': content_rating,
            'source_type': source_type
        }
    except Exception as e:
        print(f"[Atsumaru] Error fetching manga {manga_id}: {e}")
        return None
