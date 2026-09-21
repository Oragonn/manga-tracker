# backend/search_utils.py
#
# Title search for the dashboard. A series' `searchable_text` column holds all
# of its titles (main, English/romaji/native, alternates) normalised by
# normalize_search_text() and joined with "|", e.g.
#     "the eminence in shadow|kage no jitsuryokusha ni naritakute|..."
# and search_series() matches what was typed against it.
#
# A word matches when it starts at a word boundary inside ONE title. It may
# run on past the end of that word, so "swordmaster" still finds
# "Sword Master", but it can't start in the middle of a word ("art" no longer
# matches "martial") or straddle two titles. Only if nothing at all matches
# that way does it fall back to matching anywhere, so a half-remembered
# fragment still finds something.
#
# Matches come back in tiers, best first:
#   0  the query is the series' title           2  a title starts with the query
#   1  ...or one of its alternate titles        3  ...an alternate title does
#   4  one title has every word (main title)    5  ...an alternate title does
#   6  every word appears, spread over different titles
#   7  loose fallback (anywhere in the text)

import itertools
import unicodedata
from collections import Counter
from functools import lru_cache

TITLE_SEP = '|'
LOOSE_TIER = 7
# "Did you mean" only tries to repair words of at least this many letters
# (shorter ones are too easy to "correct" into something unrelated) - and a
# query that is a single word has no other word to anchor the guess, so it
# needs one more.
MIN_SUGGEST_WORD = 4
MIN_SUGGEST_LONE_WORD = 5

# Deleted rather than turned into a space, so "I'm" and "Regressor’s" become
# "im" and "regressors" - the way they are typed without the mark.
_APOSTROPHES = frozenset("'’‘‛ʼ´`′")


@lru_cache(maxsize=16384)
def normalize_search_text(text):
    """Lower-case, diacritics stripped, full-width and ligature forms folded
    (NFKC), apostrophes removed, every other punctuation mark or symbol
    turned into a word break ("Re:Zero" -> "re zero", "Dr.STONE" ->
    "dr stone"), whitespace collapsed. Used on stored titles and on what is
    typed, so both sides always agree."""
    if not text:
        return ''
    text = unicodedata.normalize('NFD', unicodedata.normalize('NFKC', str(text)).lower())
    out = []
    for ch in text:
        if ch in _APOSTROPHES:
            continue
        category = unicodedata.category(ch)
        if category == 'Mn':                      # combining accents (é -> e)
            continue
        if category[0] in 'PS' or category in ('Zs', 'Zl', 'Zp', 'Cc'):
            out.append(' ')
        elif category[0] == 'C':                  # zero-width and other invisible characters
            continue
        else:
            out.append(ch)
    return ' '.join(''.join(out).split())


def build_searchable_text(titles):
    """The value for a series' `searchable_text` column: every non-empty title
    normalised, duplicates dropped, joined with TITLE_SEP. Pass the main
    title first."""
    seen = set()
    parts = []
    for title in titles:
        if not isinstance(title, str):
            continue
        normalised = normalize_search_text(title)
        if normalised and normalised not in seen:
            seen.add(normalised)
            parts.append(normalised)
    return TITLE_SEP.join(parts)


def _word_starts(title):
    """(the title with its spaces removed, offsets in that where a word begins)"""
    starts = set()
    position = 0
    for word in title.split(' '):
        starts.add(position)
        position += len(word)
    return title.replace(' ', ''), starts


def _starts_a_word(despaced, starts, word):
    at = despaced.find(word)
    while at != -1:
        if at in starts:
            return True
        at = despaced.find(word, at + 1)
    return False


def _tier(text, main_title, words, query):
    """Best tier for one series, or None if its words don't line up at word
    starts (the caller then decides whether to fall back to a loose match)."""
    best = None
    seen_word = [False] * len(words)
    for title in text.split(TITLE_SEP):
        is_main = title == main_title
        despaced, starts = _word_starts(title)
        found = [_starts_a_word(despaced, starts, w) for w in words]
        for i, hit in enumerate(found):
            if hit:
                seen_word[i] = True

        if title == query:
            tier = 0 if is_main else 1
        elif title.startswith(query):
            tier = 2 if is_main else 3
        elif all(found):
            tier = 4 if is_main else 5
        else:
            continue
        if best is None or tier < best:
            best = tier
    if best is not None:
        return best
    return 6 if all(seen_word) else None


def search_series(rows, query):
    """Match `query` against (id, title, searchable_text) rows. Returns
    {id: tier} for the series that match, best tier lowest; None if the query
    has no searchable text in it (so it shouldn't filter anything)."""
    query = normalize_search_text(query)
    words = query.split()
    if not words:
        return None

    # Cheap first pass: every word occurs somewhere in the text (with the
    # spaces and separators gone). Anything matching at word starts does.
    candidates = []
    for series_id, title, text in rows:
        text = text or ''
        blob = text.replace(' ', '').replace(TITLE_SEP, '')
        if all(word in blob for word in words):
            candidates.append((series_id, title, text))

    ranked = {}
    for series_id, title, text in candidates:
        tier = _tier(text, normalize_search_text(title), words, query)
        if tier is not None:
            ranked[series_id] = tier
    if ranked:
        return ranked
    # nothing lines up at word starts: match anywhere, as it always used to
    return {series_id: LOOSE_TIER for series_id, _, _ in candidates}


# ---------------------------------------------------------------------------
# "Did you mean": what to offer when a search finds nothing.
#
# Each word that isn't the start of any known word is replaced by a known word
# one edit away (a letter added, dropped, changed, or two neighbours swapped -
# or one edit away from the start of a longer word, for a half-typed one), and
# the normal search is rerun with the repaired words. It gives up rather than
# guess when a word has no close match, is too short to repair safely (see
# MIN_SUGGEST_WORD), or is more than one edit from anything.

def _one_edit_apart(a, b):
    """0 if a == b, 1 if one insertion, deletion, substitution or swap of two
    neighbouring letters turns a into b, otherwise 2 (meaning "not close")."""
    if a == b:
        return 0
    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > 1:
        return 2
    i = 0
    while i < len_a and i < len_b and a[i] == b[i]:
        i += 1
    if len_a == len_b:
        if a[i + 1:] == b[i + 1:]:
            return 1
        if i + 1 < len_a and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:]:
            return 1
        return 2
    if len_a > len_b:
        return 1 if a[i + 1:] == b[i:] else 2
    return 1 if b[i + 1:] == a[i:] else 2


def _corrections(word, known_words, limit=3):
    """Known words one edit from `word` (or from the start of a longer word),
    the closest and most widely used first."""
    n = len(word)
    scored = []
    for candidate, uses in known_words.items():
        if len(candidate) < n - 1:
            continue
        distance = _one_edit_apart(word, candidate)
        if distance > 1 and len(candidate) > n:
            # perhaps still typing it: compare with the same-length start
            distance = _one_edit_apart(word, candidate[:n]) + 0.5
        if distance <= 1.5:
            scored.append((distance, -uses, candidate))
    scored.sort()
    return [(candidate, distance) for distance, _, candidate in scored[:limit]]


def suggest_series(rows, query, allowed_ids=None, limit=4):
    """Series to offer for a `query` that found nothing, best first: ids from
    (id, title, searchable_text) `rows`. With `allowed_ids`, only those series
    are offered (the ones that pass the caller's other filters). Empty when
    there is nothing sensible to suggest.

    Two kinds of repair are tried. Every word that isn't the start of any
    known word is replaced at once (the usual typo). And, since a typo often
    spells some other real word - the titles include alternates in many
    languages - each word that looks fine is also tried on its own with its
    close neighbours swapped in."""
    words = normalize_search_text(query).split()
    if not words:
        return []

    known_words = Counter()
    for _, _, text in rows:
        known_words.update(set((text or '').replace(TITLE_SEP, ' ').split()))

    min_length = MIN_SUGGEST_WORD if len(words) > 1 else MIN_SUGGEST_LONE_WORD
    fine = [any(known.startswith(word) for known in known_words) for word in words]
    fixes = [
        [(w, d) for w, d in _corrections(word, known_words) if w != word] if len(word) >= min_length else []
        for word in words
    ]

    combos = []
    if not all(fine):
        # every word that isn't fine must be repairable, or there is no guess to make
        if all(fixes[i] for i, ok in enumerate(fine) if not ok):
            options = [[(word, 0)] if ok else fixes[i] for i, (word, ok) in enumerate(zip(words, fine))]
            combos.extend(itertools.islice(itertools.product(*options), 9))
    for i, ok in enumerate(fine):
        if ok:
            for replacement, distance in fixes[i]:
                combo = [(word, 0) for word in words]
                combo[i] = (replacement, distance + 1)      # a little behind repairing an unknown word
                combos.append(tuple(combo))

    best = {}
    for combo in combos:
        distance = sum(d for _, d in combo)
        found = search_series(rows, ' '.join(word for word, _ in combo))
        for series_id, tier in (found or {}).items():
            if tier == LOOSE_TIER or (allowed_ids is not None and series_id not in allowed_ids):
                continue
            key = (distance, tier)
            if series_id not in best or key < best[series_id]:
                best[series_id] = key

    titles = {series_id: title or '' for series_id, title, _ in rows}
    ranked = sorted(best, key=lambda series_id: (best[series_id], titles[series_id]))
    return ranked[:limit]
