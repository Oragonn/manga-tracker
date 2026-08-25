# backend/trackers/kagane.py - FIXED with season offset support

import re
from ..selenium_kagane import kagane_selenium

def extract_series_id(url):
    """Extract Kagane series ID from URL like https://kagane.to/series/019dda10-c2c5-7dc7-9128-387e20611e51"""
    match = re.search(r'https://kagane\.(?:to|org)/series/([A-Za-z0-9-]+)', url)
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

def _extract_season_and_chapter(title):
    """
    Extract season number and chapter number from title.
    
    Returns: (season_number, chapter_number) tuple
    - season_number: int or None (1-based, None = Season 1)
    - chapter_number: float or None
    
    Examples:
    - "(S2) Episode 3" → (2, 3.0)
    - "Episode 65" → (None, 65.0)
    - "Episode 1 (Season 2 Premiere)" → (2, 1.0)
    """
    original_title = title
    season_number = None
    
    # *** NEW: Extract season number first ***
    # Look for season markers in various formats
    season_patterns = [
        r'[\(\[\{]\s*S(\d+)\s*[\)\]\}]',  # (S2), [S2], {S2}
        r'[\(\[\{]\s*Season\s+(\d+)',      # (Season 2), [Season 2 Premiere]
        r'\bS(\d+)\b(?!\d)',                # S2 (but not S20 in "Episode 20")
        r'\bSeason\s+(\d+)\b',              # Season 2
    ]
    
    for pattern in season_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            try:
                season_number = int(match.group(1))
                break
            except (ValueError, TypeError):
                pass
    
    # Remove all season markers for chapter extraction
    title_clean = title
    
    # Remove season prefixes
    title_clean = re.sub(
        r'^[\(\[\{]?\s*(?:S|Season)\s*\d+\s*[\)\]\}]?\s*[-:•]?\s*',
        '',
        title_clean,
        flags=re.IGNORECASE
    )
    
    # Remove season suffixes
    title_clean = re.sub(
        r'\s*[\(\[\{]\s*(?:S|Season)\s*\d+[^\)\]\}]*[\)\]\}]\s*$',
        '',
        title_clean,
        flags=re.IGNORECASE
    )
    
    # Remove remaining season markers
    title_clean = re.sub(
        r'\b(?:Season|S)\s*\d+\b',
        '',
        title_clean,
        flags=re.IGNORECASE
    )
    
    # Clean up extra spaces and punctuation
    title_clean = re.sub(r'\s*[-:•]\s*', ' ', title_clean)
    title_clean = re.sub(r'\s+', ' ', title_clean)
    title_clean = title_clean.strip()
    
    # Extract chapter number
    match = re.search(
        r'\b(?:episode|ep\.?|e|chapter|ch\.?|chap\.?)\s*(\d+(?:\.\d+)?)\b',
        title_clean,
        re.IGNORECASE
    )
    
    if match:
        try:
            chapter_number = float(match.group(1))
            return (season_number, chapter_number)
        except (ValueError, TypeError):
            pass
    
    # Try standalone number
    if re.fullmatch(r'\d+(?:\.\d+)?', title_clean):
        try:
            chapter_number = float(title_clean)
            return (season_number, chapter_number)
        except (ValueError, TypeError):
            pass
    
    # Not parseable - but this is normal for special chapters, so don't log
    return (season_number, None)

def get_series_info(series_id):
    if not series_id:
        raise ValueError("Invalid series ID")

    # Fetch via Selenium (bypasses Cloudflare)
    meta, books = kagane_selenium.get_series_info(series_id)

    chapters = []
    books_sorted = sorted(books, key=lambda x: x.get('number_sort', 0))
    
    # *** NEW: Track season info to calculate offsets ***
    season_info = {}  # season_num -> {'first_ch': float, 'last_ch': float, 'count': int}
    season_chapters = {}  # season_num -> [chapter_dicts]
    
    # First pass: Group chapters by season and find ranges
    for book in books_sorted:
        title = book.get('title', 'Untitled')
        season_num, chapter_num = _extract_season_and_chapter(title)
        
        # Treat None (no season marker) as Season 1
        if season_num is None:
            season_num = 1
        
        if chapter_num is None:
            # Special chapter - we'll handle this later
            continue
        
        if season_num not in season_info:
            season_info[season_num] = {
                'first_ch': chapter_num,
                'last_ch': chapter_num,
                'count': 0
            }
            season_chapters[season_num] = []
        
        # Update season range
        season_info[season_num]['first_ch'] = min(season_info[season_num]['first_ch'], chapter_num)
        season_info[season_num]['last_ch'] = max(season_info[season_num]['last_ch'], chapter_num)
        season_info[season_num]['count'] += 1
        
        season_chapters[season_num].append({
            'book': book,
            'title': title,
            'season': season_num,
            'chapter': chapter_num
        })
    
    # *** NEW: Calculate offsets for each season ***
    # Season 1 offset = 0
    # Season 2 offset = last chapter of Season 1
    # Season 3 offset = last chapter of Season 2, etc.
    season_offsets = {1: 0}
    sorted_seasons = sorted(season_info.keys())
    
    for i, season_num in enumerate(sorted_seasons):
        if i == 0:
            continue  # Season 1 always has offset 0
        
        prev_season = sorted_seasons[i - 1]
        # Offset = previous season's offset + previous season's last chapter
        season_offsets[season_num] = season_offsets[prev_season] + season_info[prev_season]['last_ch']
    
    print(f"[Kagane] Detected {len(sorted_seasons)} season(s):")
    for season_num in sorted_seasons:
        info = season_info[season_num]
        offset = season_offsets.get(season_num, 0)
        print(f"  Season {season_num}: Ch.{info['first_ch']}-{info['last_ch']} ({info['count']} chapters) → Offset: +{offset}")
    
    # Second pass: Create final chapter list with offsets
    last_real_chapter = 0.0
    
    for book in books_sorted:
        title = book.get('title', 'Untitled')
        season_num, chapter_num = _extract_season_and_chapter(title)
        
        if season_num is None:
            season_num = 1
        
        if chapter_num is not None:
            # Apply offset
            offset = season_offsets.get(season_num, 0)
            final_chapter_num = offset + chapter_num
            last_real_chapter = final_chapter_num
        else:
            # Special chapter - assign incremental decimal
            base = last_real_chapter
            proposed_num = base + 0.01
            if chapters and chapters[-1]['chapter_number'] >= proposed_num:
                proposed_num = chapters[-1]['chapter_number'] + 0.01
            final_chapter_num = round(proposed_num, 2)
        
        chapter_url = f"https://kagane.to/series/{series_id}/reader/{book['id']}"
        
        chapters.append({
            'chapter_number': final_chapter_num,
            'title': title,
            'release_date': book.get('release_date'),
            'chapter_url': chapter_url,
            'is_oneshot': False
        })
    
    chapters.sort(key=lambda x: x['chapter_number'])
    
    print(f"[Kagane] Final chapter range: {chapters[0]['chapter_number']:.1f} - {chapters[-1]['chapter_number']:.1f} ({len(chapters)} total)")

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
        'cover_url': f"https://kagane.to/api/v2/series/{series_id}/thumbnail",
        'status': source_status,
        'chapters': chapters,
        'alt_titles': [t['title'] for t in meta.get('alternate_titles', []) if t.get('title')],
        'genres': clean_genres,
        'content_rating': content_rating,
        'source_type': source_type
    }