# backend/title_utils.py
#
# Helpers for a series' alternate titles, which are merged across all of its
# sources and feed both the stored `alt_titles` column and the search text.

from .tag_utils import normalize_tag_list, merge_tag_lists


def source_titles(info):
    """Every name a tracker's get_*_info() result calls the series by: its
    main title, the AniList English / romaji / native titles MangaDex adds,
    and its alternate titles.

    Only MangaDex lists its main title among `alt_titles`; Kagane, Atsumaru,
    AsuraScans and HiveToons keep it in `title` alone, so reading just
    `alt_titles` (as adding a second source used to) drops the one name that
    source is actually known by."""
    if not info:
        return []
    return merge_tag_lists(
        normalize_tag_list([
            info.get('title'), info.get('title_en'),
            info.get('title_romaji'), info.get('title_native'),
        ]),
        normalize_tag_list(info.get('alt_titles')),
    )


def merge_source_titles(existing_alt_titles, info, series_titles=()):
    """The `alt_titles` list for a series once the source described by `info`
    is attached: what it already had plus every name the source uses, in
    first-seen order and without case-insensitive duplicates.

    `series_titles` are the series' own title columns; a source name that only
    repeats one of those is left out, since it is already searchable."""
    existing = normalize_tag_list(existing_alt_titles)
    known = {t.casefold() for t in normalize_tag_list(list(series_titles))}
    incoming = [t for t in source_titles(info) if t.casefold() not in known]
    return merge_tag_lists(existing, incoming)
