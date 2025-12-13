# backend/trackers/kagane.py

import re
from ..selenium_kagane import kagane_selenium

def extract_series_id(url):
    """Extract Kagane series ID from URL like https://kagane.org/series/ABC123"""
    match = re.search(r'https://kagane\.org/series/([A-Za-z0-9]+)', url)
    return match.group(1) if match else None

def _is_expected_special(title):
    """Return True if title is a known non-chapter entry."""
    lower = title.strip().lower()
    return any(word in lower for word in [
        'hiatus', 'notice', 'prologue', 'epilogue', 'bonus', 'extra',
        'chapter not available', 'not available', 'coming soon',
        'oneshot', 'special', 'side story', 'preview', 'interlude',
        'aftermath', 'flashback', 'recap', 'ova', 'ova chapter'
    ])

def _extract_chapter_number(title):
    """Extract chapter number from title like 'Episode 15', 'Ep.5', 'Ch 3', etc."""
    match = re.search(
        r'(?:episode|ep\.?|chapter|ch\.?|chap\.?)\s*(\d+\.?\d*)',
        title,
        re.IGNORECASE
    )
    if match:
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            pass

    title_clean = title.strip()
    if re.fullmatch(r'\d+\.?\d*', title_clean):
        try:
            return float(title_clean)
        except (ValueError, TypeError):
            pass

    if not _is_expected_special(title):
        try:
            from error_logger import log_error
            log_error(
                source_url="kagane:parse",
                error_message=f"Unparseable chapter title: '{title}'",
                series_title="Chapter Parser"
            )
        except:
            pass
    return None

def get_series_info(series_id):
    if not series_id:
        raise ValueError("Invalid series ID")

    # Fetch via Selenium (bypasses Cloudflare)
    meta, books = kagane_selenium.get_series_info(series_id)

    chapters = []
    last_real_chapter = 0.0
    books_sorted = sorted(books, key=lambda x: x.get('number_sort', 0))

    for book in books_sorted:
        title = book.get('title', 'Untitled')
        chapter_num = _extract_chapter_number(title)

        if chapter_num is not None:
            last_real_chapter = chapter_num
        else:
            base = last_real_chapter
            proposed_num = base + 0.01
            if chapters and chapters[-1]['chapter_number'] >= proposed_num:
                proposed_num = chapters[-1]['chapter_number'] + 0.01
            chapter_num = round(proposed_num, 2)

        # ✅ FIX: Also remove space in reader URL
        chapter_url = f"https://kagane.org/series/{series_id}/reader/{book['id']}"

        chapters.append({
            'chapter_number': chapter_num,
            'title': title,
            'release_date': book.get('release_date'),
            'chapter_url': chapter_url,
            'is_oneshot': False
        })

    chapters.sort(key=lambda x: x['chapter_number'])

    # Status mapping
    kagane_status = meta.get('status', '').upper()
    status_map = {
        'ONGOING': 'reading',
        'ENDED': 'completed',
        'HIATUS': 'on_hold',
        'CANCELLED': 'dropped'
    }
    source_status = status_map.get(kagane_status, 'plan_to_read')

    raw_genres = meta.get('genres', [])
    clean_genres = [g for g in raw_genres if g not in ('Manhwa', 'Manhua', 'Manga')]

    age_rating = meta.get('age_rating')
    if age_rating == 19:
        content_rating = 'explicit'
    elif age_rating == 18:
        content_rating = 'mature'
    elif age_rating == 16:
        content_rating = 'mild'
    else:
        content_rating = 'safe'

    if 'Manhwa' in raw_genres:
        source_type = 'manhwa'
    elif 'Manhua' in raw_genres:
        source_type = 'manhua'
    else:
        source_type = 'other'

    return {
        'title': meta.get('name', 'Unknown Title'),
        'cover_url': f"https://api.kagane.org/api/v1/series/{series_id}/thumbnail",
        'status': source_status,
        'chapters': chapters,
        'alt_titles': [t['title'] for t in meta.get('alternate_titles', []) if t.get('title')],
        'genres': clean_genres,
        'content_rating': content_rating,
        'source_type': source_type
    }