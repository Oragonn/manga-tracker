# backend/trackers/kagane.py - FIXED with season offset support

import re
from ..camoufox_kagane import kagane_browser

def extract_series_id(url):
    """Extract Kagane series ID from a series URL, e.g.
    https://kagane.to/series/019dda10-c2c5-7dc7-9128-387e20611e51
    (optionally followed by /reader/... for a chapter-reader link to the
    same series). The UUID must come immediately after /series/ -- Kagane
    also has non-series links under that prefix, e.g. /series/similar/{id}
    ("find similar series" cross-links, where {id} is a tracker_id, not a
    series_id) -- those are intentionally rejected rather than misparsed.
    """
    match = re.search(
        r'https://kagane\.(?:to|org)/series/'
        r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})',
        url
    )
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

    # Fetch via a stealth-hardened browser (clears Cloudflare's Turnstile challenge)
    meta, books = kagane_browser.get_series_info(series_id)

    chapters = []
    books_sorted = sorted(books, key=lambda x: x.get('number_sort', 0))

    # Kagane labels chapters with a season marker in the title purely as a
    # descriptor (e.g. "Episode 133 (Season 3 Finale)") - numbering does
    # NOT reset per season on Kagane itself (verified live: that exact
    # "finale" chapter is immediately followed by Episode 134, no reset).
    # Assuming every season boundary is a reset and pre-summing offsets
    # from labeled season buckets double-counted an already-continuous
    # number (e.g. produced chapter 280 for what Kagane itself calls
    # Episode 133). Instead, only apply an offset when the raw number
    # actually drops compared to the previous chapter in reading order -
    # correct whether or not a given series' numbering happens to reset.
    running_offset = 0.0
    prev_raw = None
    last_real_chapter = 0.0

    for book in books_sorted:
        title = book.get('title', 'Untitled')
        _, raw_chapter_num = _extract_season_and_chapter(title)

        if raw_chapter_num is not None:
            if prev_raw is not None and raw_chapter_num < prev_raw:
                # Numbering actually went backwards - a genuine reset.
                # Carry the peak reached so far forward as the new base.
                running_offset += prev_raw
                print(f"[Kagane] Detected a numbering reset before \"{title}\" "
                      f"({raw_chapter_num} after {prev_raw}) - offset now +{running_offset}")
            final_chapter_num = running_offset + raw_chapter_num
            prev_raw = raw_chapter_num
            last_real_chapter = final_chapter_num
        else:
            # Special chapter (no parseable number) - assign an
            # incremental decimal right after the last real chapter.
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

    if chapters:
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

    rating_map = {
        'safe': 'safe',
        'suggestive': 'mild',
        'erotica': 'mature',
        'pornographic': 'explicit',
    }
    content_rating = rating_map.get((meta.get('content_rating') or '').strip().lower(), 'safe')

    if 'Manhwa' in raw_genres:
        source_type = 'manhwa'
    elif 'Manhua' in raw_genres:
        source_type = 'manhua'
    elif 'Manga' in raw_genres:
        source_type = 'manga'
    else:
        source_type = 'other'

    return {
        'title': meta.get('name', 'Unknown Title'),
        'cover_url': meta.get('cover_url'),
        'status': source_status,
        'chapters': chapters,
        'alt_titles': [t['title'] for t in meta.get('alternate_titles', []) if t.get('title')],
        'genres': clean_genres,
        'content_rating': content_rating,
        'source_type': source_type
    }