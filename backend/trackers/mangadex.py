# backend/trackers/mangadex.py

import requests
import time
from urllib.parse import urlparse

_session = requests.Session()
_session.headers.update({
    'User-Agent': 'MangaTracker/1.0 (your.email@example.com)'
})

_last_call = 0
_MIN_DELAY = 0.4
_MAX_RETRIES = 3

def _delayed_get(url, **kwargs):
    global _last_call
    for attempt in range(_MAX_RETRIES):
        now = time.time()
        if now - _last_call < _MIN_DELAY:
            time.sleep(_MIN_DELAY - (now - _last_call))
        _last_call = now
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

def get_manga_info(manga_id):
    try:
        resp = _delayed_get(f"https://api.mangadex.org/manga/{manga_id}")
        if resp.status_code != 200:
            raise Exception(f"MangaDex API returned HTTP {resp.status_code} for manga {manga_id}")
        data = resp.json()['data']
        attrs = data['attributes']
        
        main_title = attrs['title'].get('en')
        alt_titles_dict = {}
        alt_titles_list = attrs.get('altTitles', [])
        for entry in alt_titles_list:
            lang = entry.get('language')
            text = entry.get('title')
            if lang and text:
                alt_titles_dict[lang] = text
        
        title = alt_titles_dict.get('en', main_title)
        if not title:
            title = next(iter(attrs['title'].values()), "Unknown Manga")
        
        all_titles = {**attrs['title'], **alt_titles_dict}
        
        status_map = {
            'ongoing': 'reading',
            'completed': 'completed',
            'hiatus': 'on_hold',
            'cancelled': 'dropped'
        }
        raw_status = attrs['status']
        status = status_map.get(raw_status, 'plan_to_read')
        
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
        
        # Extract genre tags (group = "genre")
        for tag in attrs.get('tags', []):
            if tag.get('attributes', {}).get('group') == 'genre':
                name_map = tag['attributes'].get('name', {})
                # Prefer English name
                genre_name = name_map.get('en') or next(iter(name_map.values()), None)
                if genre_name and genre_name not in ('Manga', 'Manhwa', 'Manhua'):
                    genres.append(genre_name)
        
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
    Fetch manga info from MangaDex, then enrich with AniList if possible.
    Returns combined data.
    """
    md_data = get_manga_info(manga_id)  # raises on real failure, no None case anymore

    # Try AniList enrichment
    try:
        from .anilist import search_manga_by_title  # relative import
        anilist_data = search_manga_by_title(md_data['title'])
        if anilist_data:
            # Cover & banner
            if anilist_data.get('cover_url'):
                md_data['cover_url'] = anilist_data['cover_url']
            if anilist_data.get('banner_url'):
                md_data['banner_url'] = anilist_data['banner_url']
            
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
                if chapter_str is None or str(chapter_str).strip() == "":
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

                key = (volume_str, chapter_str)
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
                    'is_oneshot': is_oneshot
                })

            total = payload.get('total', len(data))
            offset += len(data)
            if len(data) < page_size or offset >= total:
                break

        # A lone "chapter 0" with nothing else published is effectively a
        # oneshot; once other chapters exist, chapter 0 is just a normal chapter.
        if len(chapters) == 1 and str(chapters[0]['raw_chapter'] or "").strip() == "0":
            chapters[0]['is_oneshot'] = True

        return chapters
    except Exception as e:
        print(f"[MangaDex] Failed to fetch chapters for {manga_id}: {e}")
        raise
    