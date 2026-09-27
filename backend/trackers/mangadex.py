# backend/trackers/mangadex.py

import re
import requests
import threading
import time
from urllib.parse import urlparse

from ..tag_utils import merge_tag_lists

_session = requests.Session()
_session.headers.update({
    'User-Agent': 'MangaTracker/1.0 (your.email@example.com)'
})

_last_call = 0
_last_call_lock = threading.Lock()
_MIN_DELAY = 0.4
_MAX_RETRIES = 3

def _delayed_get(url, **kwargs):
    global _last_call
    for attempt in range(_MAX_RETRIES):
        # The scheduler scans several series at once, so the throttle
        # read+write must be atomic (same as atsu.py/asura.py), and the time
        # recorded is when this request actually goes out - after any sleep -
        # or the next caller measures its gap from too early.
        with _last_call_lock:
            now = time.time()
            if now - _last_call < _MIN_DELAY:
                time.sleep(_MIN_DELAY - (now - _last_call))
            _last_call = time.time()
        try:
            resp = _session.get(url, timeout=10, **kwargs)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return resp
        except Exception as e:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    raise Exception("Max retries exceeded")

def extract_manga_id(url):
    parsed = urlparse(url)
    if 'mangadex.org' not in parsed.netloc:
        return None
    path_parts = parsed.path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[0] in ('title', 'manga'):
        return path_parts[1]
    elif len(path_parts) == 1:
        return path_parts[0]
    return None

def collect_titles(attrs):
    """Every title MangaDex lists for a manga: its main title(s), then the
    alternates. `altTitles` is a list of one-entry {language: title} objects
    (e.g. [{"ja": "..."}, {"en": "..."}]) - the same language can appear
    more than once - so each entry's values are read rather than looking for
    a "language" / "title" field, which the API doesn't have."""
    titles = list((attrs.get('title') or {}).values())
    for entry in attrs.get('altTitles') or []:
        titles.extend((entry or {}).values())
    return merge_tag_lists([t.strip() for t in titles if isinstance(t, str) and t.strip()])

def get_titles_for_manga(manga_ids):
    """{manga_id: [every title]} for many manga at once - up to 100 per
    request instead of one request (plus a cover lookup) each. Ids MangaDex
    doesn't return are simply absent; raises if a request fails."""
    ids = list(dict.fromkeys(manga_ids))
    found = {}
    for start in range(0, len(ids), 100):
        resp = _delayed_get("https://api.mangadex.org/manga", params={
            'ids[]': ids[start:start + 100],
            'limit': 100,
            # without this the endpoint leaves out pornographic titles
            'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'],
        })
        if resp.status_code != 200:
            raise Exception(f"MangaDex API returned HTTP {resp.status_code} for a batch of {len(ids[start:start + 100])} manga")
        for item in resp.json()['data']:
            found[item['id']] = collect_titles(item['attributes'])
    return found

def get_covers_for_manga(manga_ids):
    """{manga_id: main cover url} for many manga at once - up to 100 per
    request, with the cover filename included in the same response instead
    of a separate /cover lookup each. The cover picked is the same main
    cover_art get_manga_info() uses. Ids MangaDex doesn't return (or that
    have no cover) are simply absent; raises if a request fails."""
    ids = list(dict.fromkeys(manga_ids))
    found = {}
    for start in range(0, len(ids), 100):
        resp = _delayed_get("https://api.mangadex.org/manga", params={
            'ids[]': ids[start:start + 100],
            'limit': 100,
            'includes[]': ['cover_art'],
            'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'],
        })
        if resp.status_code != 200:
            raise Exception(f"MangaDex API returned HTTP {resp.status_code} for a batch of {len(ids[start:start + 100])} manga")
        for item in resp.json()['data']:
            for rel in item.get('relationships') or []:
                filename = (rel.get('attributes') or {}).get('fileName')
                if rel.get('type') == 'cover_art' and filename:
                    found[item['id']] = f"https://uploads.mangadex.org/covers/{item['id']}/{filename}"
                    break
    return found

_STATUS_MAP = {
    'ongoing': 'reading',
    'completed': 'completed',
    'hiatus': 'on_hold',
    'cancelled': 'dropped'
}

def get_manga_status(manga_id):
    """Just a manga's publication status, mapped like get_manga_info() does
    ('plan_to_read' for one MangaDex reports that isn't recognised) - a single
    request, for the scheduler to check on every scan without also looking up
    the cover. Raises on a failed request."""
    resp = _delayed_get(f"https://api.mangadex.org/manga/{manga_id}")
    if resp.status_code != 200:
        raise Exception(f"MangaDex API returned HTTP {resp.status_code} for manga {manga_id}")
    return _STATUS_MAP.get(resp.json()['data']['attributes']['status'], 'plan_to_read')

def get_manga_info(manga_id):
    try:
        resp = _delayed_get(f"https://api.mangadex.org/manga/{manga_id}")
        if resp.status_code != 200:
            raise Exception(f"MangaDex API returned HTTP {resp.status_code} for manga {manga_id}")
        data = resp.json()['data']
        attrs = data['attributes']
        
        # The series keeps MangaDex's main title. (An English alternate title
        # is deliberately not preferred over it: that would rename series
        # differently from everything already in the library.)
        title = attrs['title'].get('en')
        if not title:
            title = next(iter(attrs['title'].values()), "Unknown Manga")

        all_titles = collect_titles(attrs)

        status = _STATUS_MAP.get(attrs['status'], 'plan_to_read')
        
        cover_url = None
        for rel in data['relationships']:
            if rel['type'] == 'cover_art':
                cover_id = rel['id']
                # Cover art is a nice-to-have, not core data - a CDN hiccup
                # here shouldn't flag the whole source as broken.
                try:
                    cover_resp = _delayed_get(f"https://api.mangadex.org/cover/{cover_id}")
                    if cover_resp.status_code == 200:
                        filename = cover_resp.json()['data']['attributes']['fileName']
                        cover_url = f"https://uploads.mangadex.org/covers/{manga_id}/{filename}"
                except Exception as cover_err:
                    print(f"[MangaDex] Cover fetch failed for {manga_id}, continuing without it: {cover_err}")
                break

        # === EXTRACT GENRES AND METADATA ===
        genres = []
        content_rating_raw = attrs.get('contentRating', 'safe')
        
        # Map contentRating to internal rating
        content_rating_map = {
            'safe': 'safe',
            'suggestive': 'mild',
            'erotica': 'mature',
            'pornographic': 'explicit'
        }
        content_rating = content_rating_map.get(content_rating_raw, 'safe')
        
        # Keep every tag MangaDex attaches, not just group = "genre" -- its
        # themes (Isekai, Reincarnation...), content warnings and formats
        # are tags too and were being dropped.
        for tag in attrs.get('tags', []):
            name_map = tag.get('attributes', {}).get('name', {})
            # Prefer English name
            tag_name = name_map.get('en') or next(iter(name_map.values()), None)
            if tag_name and tag_name not in ('Manga', 'Manhwa', 'Manhua'):
                genres.append(tag_name)

        # The demographic (Shounen, Seinen...) isn't a tag on MangaDex, it's
        # its own attribute, but every other source lists it as one.
        demographic = attrs.get('publicationDemographic')
        if demographic:
            genres.append(demographic.capitalize())

        # Determine source_type from originalLanguage
        lang = attrs.get('originalLanguage')
        if lang == 'ja':
            source_type = 'manga'
        elif lang == 'ko':
            source_type = 'manhwa'
        elif lang in ('zh', 'zh-hans', 'zh-hant'):
            source_type = 'manhua'
        else:
            source_type = 'other'
        
        return {
            'title': title,
            'alt_titles': all_titles,
            'cover_url': cover_url,
            'status': status,
            'genres': genres,
            'content_rating': content_rating,
            'source_type': source_type
        }
    except Exception as e:
        print(f"[MangaDex] Error fetching manga {manga_id}: {e}")
        raise

def get_manga_info_with_anilist(manga_id):
    """
    Fetch manga info from MangaDex, then enrich titles/synonyms with AniList
    if possible. Covers always come from MangaDex (no AniList fallback).
    Returns combined data.
    """
    md_data = get_manga_info(manga_id)  # raises on real failure, no None case anymore
    return enrich_with_anilist(md_data, manga_id)


def enrich_with_anilist(md_data, manga_id):
    """Fill in get_manga_info()'s missing titles and extra synonyms from
    AniList, in place (and returned). Best-effort: a failed look-up leaves it
    as it was. Split out so the Add Series preview can show a series without
    waiting on AniList, and the add does this step itself afterwards."""
    # Try AniList enrichment (titles/synonyms only - covers always come from MangaDex)
    try:
        from .anilist import search_manga_by_title  # relative import
        anilist_data = search_manga_by_title(md_data['title'])
        if anilist_data:
            # Titles
            if not md_data.get('title_en') and anilist_data.get('title_en'):
                md_data['title_en'] = anilist_data['title_en']
            if not md_data.get('title_romaji') and anilist_data.get('title_romaji'):
                md_data['title_romaji'] = anilist_data['title_romaji']
            if not md_data.get('title_native') and anilist_data.get('title_native'):
                md_data['title_native'] = anilist_data['title_native']
            
            # === MERGE SYNONYMS INTO alt_titles ===
            current_alt = md_data.get('alt_titles') or []
            if isinstance(current_alt, dict):
                # MangaDex alt_titles is a dict like {'en': '...', 'ja': '...'}
                current_list = list(current_alt.values())
            elif isinstance(current_alt, list):
                current_list = current_alt
            else:
                current_list = [str(current_alt)] if current_alt else []
            
            synonyms = anilist_data.get('synonyms') or []
            combined_alt = list(set(current_list + synonyms))  # dedupe
            md_data['alt_titles'] = combined_alt  # store as list for consistency
            
    except Exception as e:
        # Let the caller handle logging
        print(f"[MangaDex] AniList enrichment failed for {manga_id}: {e}")
        # Do NOT log here — just return current data
        pass

    return md_data

def get_all_covers(manga_id):
    """Fetch every cover art variant for a manga (one per volume/locale,
    the same set MangaDex's own "Art" tab shows) - not just the single one
    get_manga_info() picks. Paginated the same way get_latest_chapters() is,
    since some long-running series have well over 100 volume covers.
    Raises on a genuine fetch failure; caller treats this as optional/
    best-effort and should catch accordingly.
    """
    page_size = 100
    offset = 0
    covers = []

    while True:
        params = {
            'manga[]': manga_id,
            'limit': page_size,
            'offset': offset,
            'order[volume]': 'asc'
        }
        resp = _delayed_get("https://api.mangadex.org/cover", params=params)
        if resp.status_code != 200:
            raise Exception(f"MangaDex cover list returned HTTP {resp.status_code} for manga {manga_id}")

        payload = resp.json()
        data = payload.get('data', [])
        for item in data:
            attrs = item.get('attributes', {})
            filename = attrs.get('fileName')
            if not filename:
                continue
            covers.append({
                'cover_url': f"https://uploads.mangadex.org/covers/{manga_id}/{filename}",
                'volume': attrs.get('volume'),
                'locale': attrs.get('locale')
            })

        total = payload.get('total', len(data))
        offset += len(data)
        if len(data) < page_size or offset >= total:
            break

    return covers

_PART_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
}
_PART_RE = re.compile(
    r'\b(?:part|pt|chapter|ch|episode|ep)\.?\s*(\d+(?:\.\d+)?|' + '|'.join(_PART_WORDS) + r')\b',
    re.IGNORECASE)


def _part_number(title):
    """'Part 2', 'Pt. 2', 'Part Two: The End' -> 2.0; None when no part number."""
    m = _PART_RE.search(title or '')
    if not m:
        return None
    token = m.group(1).lower()
    return float(_PART_WORDS.get(token, token))


def _resolve_unnumbered_chapters(chapters):
    """
    Settle what MangaDex's unnumbered (chapter=None) uploads become.

    Normally they are all one oneshot, stored as a single 0.0 entry per
    volume (several groups uploading the same oneshot must not count twice).
    The exception is a oneshot released in parts: when a series has nothing
    but unnumbered uploads and every one is titled with a part number
    ("Part 1", "Part 2"), each distinct part becomes a real chapter
    numbered by that part, so the series reads as 2 chapters instead of
    a oneshot that silently lost Part 1.
    """
    unnumbered = [c for c in chapters if c['_unnumbered']]
    parts = {}
    if unnumbered and len(unnumbered) == len(chapters):
        for c in unnumbered:
            part = _part_number(c['_title'])
            if part is None:
                parts = {}
                break
            parts.setdefault(part, c)  # feed is newest first: keep that upload

    result = []
    if len(parts) > 1:
        for part, c in sorted(parts.items()):
            c['chapter_number'] = part
            c['raw_chapter'] = f"{part:g}"
            c['is_oneshot'] = False
            result.append(c)
    else:
        seen_volumes = set()
        for c in chapters:
            if c['_unnumbered']:
                if c['volume'] in seen_volumes:
                    continue
                seen_volumes.add(c['volume'])
            result.append(c)

    for c in result:
        del c['_unnumbered'], c['_title']
    return result


def get_latest_chapters(manga_id, limit=100):
    """
    Fetch all chapters for a manga, paginating through MangaDex's per-manga
    chapter feed (`/manga/{id}/feed`, capped at 500 results per request
    server-side — the generic `/chapter` endpoint caps at 100 and was
    silently truncating series with more chapters than that) until every
    chapter has been retrieved. `limit` is kept as a parameter for backward
    compatibility but is no longer used to truncate results.
    """
    page_size = 500
    offset = 0
    chapters = []
    seen = set()
    first_page = True

    try:
        while True:
            params = {
                'translatedLanguage[]': ['en'],
                'contentRating[]': ['safe', 'suggestive', 'erotica', 'pornographic'],
                'order[createdAt]': 'desc',
                'limit': page_size,
                'offset': offset
            }
            # Only the first page failing counts as "this source is broken" -
            # a later page failing mid-pagination (rare, very long series)
            # just stops early and keeps whatever was already collected,
            # rather than discarding real progress.
            try:
                resp = _delayed_get(f"https://api.mangadex.org/manga/{manga_id}/feed", params=params)
            except Exception:
                if first_page:
                    raise
                print(f"[MangaDex] Pagination request failed for {manga_id}, keeping partial results")
                break
            if resp.status_code != 200:
                if first_page:
                    raise Exception(f"MangaDex chapter feed returned HTTP {resp.status_code} for manga {manga_id}")
                break
            first_page = False

            payload = resp.json()
            data = payload.get('data', [])

            for item in data:
                attrs = item['attributes']
                chapter_str = attrs.get('chapter')
                volume_str = attrs.get('volume')

                is_oneshot = False
                unnumbered = chapter_str is None or str(chapter_str).strip() == ""
                if unnumbered:
                    is_oneshot = True
                    normalized_chapter = 0.0
                elif str(chapter_str).strip() == "0":
                    # Chapter 0 is a real chapter (prologue/ch.0), not a oneshot,
                    # unless it's the only chapter the series has at all.
                    normalized_chapter = 0.0
                elif str(chapter_str).replace('.', '', 1).isdigit():
                    normalized_chapter = float(chapter_str)
                else:
                    is_oneshot = True
                    normalized_chapter = 0.0

                # Unnumbered uploads all share chapter None, so the title has
                # to be part of the key or a oneshot released as "Part 1" /
                # "Part 2" collapses into one entry. Genuine duplicates of the
                # same oneshot are collapsed again after the loop.
                title = (attrs.get('title') or '').strip()
                key = (volume_str, chapter_str, title.lower() if unnumbered else None)
                if key in seen:
                    continue
                seen.add(key)

                release_date = attrs['createdAt']
                chapter_url = f"https://mangadex.org/chapter/{item['id']}"

                chapters.append({
                    'volume': volume_str,
                    'raw_chapter': chapter_str,
                    'chapter_number': normalized_chapter,
                    'release_date': release_date,
                    'chapter_url': chapter_url,
                    'is_oneshot': is_oneshot,
                    '_unnumbered': unnumbered,
                    '_title': title,
                })

            total = payload.get('total', len(data))
            offset += len(data)
            if len(data) < page_size or offset >= total:
                break

        chapters = _resolve_unnumbered_chapters(chapters)

        # A lone "chapter 0" with nothing else published is effectively a
        # oneshot; once other chapters exist, chapter 0 is just a normal chapter.
        if len(chapters) == 1 and str(chapters[0]['raw_chapter'] or "").strip() == "0":
            chapters[0]['is_oneshot'] = True

        return chapters
    except Exception as e:
        print(f"[MangaDex] Failed to fetch chapters for {manga_id}: {e}")
        raise
    