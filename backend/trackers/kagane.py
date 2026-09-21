# backend/trackers/kagane.py - FIXED with season offset support

import re
from ..camoufox_kagane import kagane_browser
from ..tag_utils import merge_tag_lists

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

# Kagane lists announcements and author's notes among a series' books:
# "Hiatus", "Hiatus Special", "Scheduled Break", "Hiatus Notice", "Special
# Announcement", "Season 1 Afterword", "Creator's Note", "Annonce Anime". They
# have no chapter to read, but used to be stored as an x.01 chapter - so when
# the newest entry was one, the series showed an unread chapter that doesn't
# exist (Yes Ma'am: latest 44.01 after Episode 44).
#
# A title only counts when EVERY word in it is a notice word or one of the
# fillers that go with them, so "Prison Break", "To Bend or Break" and
# "Special Episode 2" stay chapters.
_NOTICE_WORDS = {'hiatus', 'break', 'notice', 'announcement', 'annoucement', 'annonce',
                 'afterword', 'note'}
_NOTICE_FILLER = {'scheduled', 'special', 'anime', 'creator', "creator's", 'creators',
                  'author', "author's", 'authors'}
_BRACKETED = re.compile(r'[\(\[\{][^\)\]\}]*[\)\]\}]')
_SEASON_MARK = re.compile(r'\bseason\s*\d+\b')
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")

def _is_notice(title):
    """True if title is an announcement / author's note rather than a chapter
    (only asked about titles that carry no chapter number)."""
    text = title.lower().replace('’', "'")
    # "(Koneko-Scantrad)"-style group tags aren't part of the wording
    words = _WORD.findall(_SEASON_MARK.sub(' ', _BRACKETED.sub(' ', text))) or _WORD.findall(text)
    return (any(word in _NOTICE_WORDS for word in words)
            and all(word in _NOTICE_WORDS or word in _NOTICE_FILLER for word in words))

# "Special Episode 1", "Bonus Chapter 2", "Extra Ep. 3": a side entry that
# only borrows the episode wording. Its number belongs to the specials, not
# to the main sequence.
_SIDE_ENTRY = re.compile(
    r'\b(?:special|bonus|extra)\s+(?:episode|ep\.?|chapter|ch\.?|chap\.?)\s*\d',
    re.IGNORECASE
)

# "1. Calamity Sword", "62 - The Waiting", "12: Departure": some uploads put the
# chapter number in front of the chapter's name instead of writing "Chapter N".
# Only tried once every other pattern has failed, so a title that already
# parses keeps the number it always had. The separator has to be followed by a
# space (or, for a dot, a non-digit) so "2.5 Something" isn't read as chapter 2.
_NUMBER_PREFIX = re.compile(r'^\s*(\d+(?:\.\d+)?)(?:\s*[-\u2013\u2014:]\s+|\.\s+|\.(?=\D))')

# Chapter-less titles that are still side entries rather than the next chapter
# in line ("Prologue", "Oneshot", "Extra", "Bonus Chapters", "SE 3. Day Off").
# They keep the x.01 numbering - right after the last real chapter - instead of
# being numbered from Kagane's own numbering, which gives them a slot of their own
# (Falling for Danger: "Prologue" is 1, "Chapter 1" is 2).
#
# Like _is_notice, a title only counts when the whole of it is special wording,
# or when it opens with a label such as "Side Story:" - a chapter that merely
# contains one of the words ("Prologue to the War", "Extra Life") is a chapter.
_SPECIAL_WORDS = {'prologue', 'prologues', 'epilogue', 'epilogues', 'oneshot', 'oneshots',
                  'extra', 'extras', 'bonus', 'bonuses', 'special', 'specials',
                  'preview', 'previews', 'interlude', 'omake', 'recap'}
_SPECIAL_FILLER = {'chapter', 'chapters', 'episode', 'episodes', 'ep', 'volume', 'vol',
                   'free', 'the', 'end'}
_SPECIAL_LABEL = re.compile(
    r"^\s*(?:side\s+stor(?:y|ies)\b|(?:prologue|epilogue|one-?shot|extra|bonus|omake|special|preview)\s*[:\-\u2013\u2014])"
    r"|^\s*SE\s*\d|\b(?:coming\s+soon|not\s+available)\b",
    re.IGNORECASE
)

def _is_special(title):
    """True if title is a side entry rather than the next chapter in line
    (only asked about titles that carry no chapter number)."""
    if _SIDE_ENTRY.search(title) or _SPECIAL_LABEL.search(title):
        return True
    text = title.lower().replace('one-shot', 'oneshot')
    words = _WORD.findall(_SEASON_MARK.sub(' ', _BRACKETED.sub(' ', text))) or _WORD.findall(text)
    return (any(word in _SPECIAL_WORDS for word in words)
            and all(word in _SPECIAL_WORDS or word in _SPECIAL_FILLER for word in words))

def _extract_season_and_chapter(title):
    """
    Extract season number and chapter number from title.

    Returns: (season_number, chapter_number) tuple
    - season_number: int or None (1-based, None = Season 1)
    - chapter_number: float or None (also None for a "Special Episode N"
      style side entry, so it is numbered like any other special)

    Examples:
    - "(S2) Episode 3" → (2, 3.0)
    - "Episode 65" → (None, 65.0)
    - "Episode 1 (Season 2 Premiere)" → (2, 1.0)
    - "62. The Waiting" → (None, 62.0)
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

    # Reading "Special Episode 1" as Episode 1 made the numbering look like it
    # had restarted, which shifted every real episode after it (Yes Ma'am's
    # Episode 32, right after "Special Episode 2", came out as 63).
    if _SIDE_ENTRY.search(title_clean):
        return (season_number, None)

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
    
    # "12. Title" / "12 - Title"
    match = _NUMBER_PREFIX.match(original_title)
    if match:
        return (season_number, float(match.group(1)))

    # Not parseable - but this is normal for special chapters, so don't log
    return (season_number, None)

def _book_number(book, key):
    """book[key] as a float, or None if the book has no usable value there."""
    try:
        number = float(book.get(key))
    except (TypeError, ValueError):
        return None
    return number if number == number else None

def get_series_info(series_id, with_gallery=False):
    """with_gallery=True also downloads the series' full cover gallery and
    returns it under 'gallery_covers' as [{cover_url, volume, locale, note}].
    Only worth it when a source is first added - not on routine scans."""
    if not series_id:
        raise ValueError("Invalid series ID")

    # Fetch via a stealth-hardened browser (clears Cloudflare's Turnstile challenge)
    meta, books = kagane_browser.get_series_info(series_id, with_gallery=with_gallery)

    books_sorted = sorted(books, key=lambda x: x.get('number_sort', 0))

    # What each book is. "numbered" carries its chapter number in the title;
    # "unnumbered" is a plain chapter with only a name ("Untitled", "Bathhouse",
    # "The Brand (danke-Empire)"); "special" is a side entry that gets an x.01.
    entries = []
    for book in books_sorted:
        title = book.get('title', 'Untitled')
        _, raw_chapter_num = _extract_season_and_chapter(title)
        if raw_chapter_num is not None:
            kind = 'numbered'
        elif _is_notice(title):
            # An announcement, not a chapter: leave it out (and don't let it
            # take a number, or the entry after it shifts).
            continue
        elif _is_special(title):
            kind = 'special'
        else:
            kind = 'unnumbered'
        entries.append({'book': book, 'title': title, 'raw': raw_chapter_num, 'kind': kind,
                        'chapter_no': _book_number(book, 'chapter_no'),
                        'position': _book_number(book, 'number_sort')})

    # Chapter-less books are numbered from the numbers Kagane gives its books.
    # chapter_no ("3.5", "36") is the chapter number the site shows, whereas
    # number_sort is only the book's position in the list, so every half-chapter
    # and extra before a chapter pushes it one higher (Yakuza Fiance's last
    # chapter is chapter_no 36 but number_sort 43). chapter_no is only used as
    # a sequence when it never goes backwards along the list, though: an
    # anthology numbers each story on its own (3, 10, 6, 16, 1 ...) and
    # Berserk labels its first books "0.9" then "0.10", which reads as 0.1.
    chapter_nos = [e['chapter_no'] for e in entries]
    use_chapter_no = (None not in chapter_nos
                      and all(later >= earlier for earlier, later in zip(chapter_nos, chapter_nos[1:])))
    for e in entries:
        e['site_no'] = e['chapter_no'] if use_chapter_no else e['position']

    # A series with a single book is a oneshot: nothing to number it against.
    if len(entries) == 1 and entries[0]['kind'] == 'unnumbered':
        entries[0]['kind'] = 'special'

    # When chapter-less books outnumber the titled ones, the numbers that do
    # appear in titles are per-arc ("The Golden Age, Chapter 1") rather than a
    # sequence, so the site's numbers are used for everything.
    numbered_count = sum(1 for e in entries if e['kind'] == 'numbered')
    unnumbered_count = sum(1 for e in entries if e['kind'] == 'unnumbered')
    site_numbering = unnumbered_count > numbered_count
    if site_numbering:
        for e in entries:
            if e['kind'] == 'numbered':
                e['kind'], e['raw'] = 'unnumbered', None

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
    for e in entries:
        if e['kind'] != 'numbered':
            continue
        raw_chapter_num = e['raw']
        if prev_raw is not None and raw_chapter_num < prev_raw:
            # Numbering actually went backwards - a genuine reset.
            # Carry the peak reached so far forward as the new base.
            running_offset += prev_raw
            print(f"[Kagane] Detected a numbering reset before \"{e['title']}\" "
                  f"({raw_chapter_num} after {prev_raw}) - offset now +{running_offset}")
        e['final'] = running_offset + raw_chapter_num
        prev_raw = raw_chapter_num

    taken = {e['final'] for e in entries if 'final' in e}
    first_anchor = next(((e['site_no'], e['final']) for e in entries
                         if e['kind'] == 'numbered' and e['site_no'] is not None), None)

    chapters = []
    last_real_chapter = 0.0
    anchor = None  # (site number, chapter number) of the latest titled chapter
    from_site_count = 0

    for e in entries:
        book, title = e['book'], e['title']
        final_chapter_num = None

        if e['kind'] == 'numbered':
            final_chapter_num = e['final']
            if e['site_no'] is not None:
                anchor = (e['site_no'], final_chapter_num)
        elif e['kind'] == 'unnumbered' and e['site_no'] is not None:
            # The site's numbering doesn't always agree with the titles' (an
            # "Episode 0" prologue is chapter_no 1, so "Episode 1" is 2), so
            # where a titled chapter is nearby it's the reference point:
            # "Chapter 23" then an untitled entry numbered 24 is chapter 24,
            # and a series whose titles start at Episode 0 stays in step.
            reference = None if site_numbering else (anchor or first_anchor)
            candidate = e['site_no'] if reference is None else reference[1] + (e['site_no'] - reference[0])
            candidate = round(candidate, 2)
            # If that number is already a chapter's (a contest post between
            # two chapters, or a number shared by several entries of an
            # anthology), this one is a side entry after all.
            if candidate >= 0 and candidate not in taken:
                final_chapter_num = candidate
                from_site_count += 1

        if final_chapter_num is not None:
            last_real_chapter = final_chapter_num
        else:
            # Special chapter (no parseable number) - assign an
            # incremental decimal right after the last real chapter.
            base = last_real_chapter
            proposed_num = base + 0.01
            if chapters and chapters[-1]['chapter_number'] >= proposed_num:
                proposed_num = chapters[-1]['chapter_number'] + 0.01
            final_chapter_num = round(proposed_num, 2)
        taken.add(final_chapter_num)

        chapter_url = f"https://kagane.to/series/{series_id}/reader/{book['id']}"

        chapters.append({
            'chapter_number': final_chapter_num,
            'title': title,
            'release_date': book.get('release_date'),
            'chapter_url': chapter_url,
            'is_oneshot': False
        })

    if from_site_count:
        print(f"[Kagane] {from_site_count} of {len(chapters)} books have no chapter number in "
              f"their title - numbered from Kagane's own numbering"
              f"{' (most of the series is like that)' if site_numbering else ''}")

    chapters.sort(key=lambda x: x['chapter_number'])

    if chapters:
        print(f"[Kagane] Final chapter range: {chapters[0]['chapter_number']:.1f} - {chapters[-1]['chapter_number']:.1f} ({len(chapters)} total)")

    # Status mapping. Kagane's API currently says Ongoing / Completed /
    # Hiatus / Abandoned; ENDED and CANCELLED are kept from what this
    # originally expected, in case either spelling shows up. A finished series
    # used to fall through to 'plan_to_read' because only ENDED was mapped.
    kagane_status = (meta.get('status') or '').upper()
    status_map = {
        'ONGOING': 'reading',
        'COMPLETED': 'completed',
        'ENDED': 'completed',
        'HIATUS': 'on_hold',
        'ABANDONED': 'dropped',
        'CANCELLED': 'dropped',
        'CANCELED': 'dropped'
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

    info = {
        'title': meta.get('name', 'Unknown Title'),
        'cover_url': meta.get('cover_url'),
        'status': source_status,
        'chapters': chapters,
        'alt_titles': [t['title'] for t in meta.get('alternate_titles', []) if t.get('title')],
        'genres': merge_tag_lists(clean_genres, meta.get('tags', [])),
        'content_rating': content_rating,
        'source_type': source_type
    }
    if with_gallery:
        info['gallery_covers'] = meta.get('gallery_covers') or []
    return info