# backend/source_metadata.py
#
# What each source contributes to its series, and taking it back out when
# the source is removed.
#
# Adding a source merges its titles into series.alt_titles, its tags into
# series.genres and (if it outranks the others) its content rating. Removing
# a source used to leave all of that behind - so a wrong source added by
# mistake kept polluting search, tags and rating forever.
#
# Each source's own contribution is now stored as JSON in
# series_sources.metadata ({titles, genres, content_rating}). Sources added
# before that have none stored, so it's fetched live the first time it's
# needed (and then kept).
#
# On removal, a title/tag is dropped only if the removed source reported it
# AND no remaining source does - anything else in the lists (tags you added
# yourself, titles from sources we can't attribute) is left alone. If any of
# the sources involved can't be fetched, nothing is dropped: better to leave
# a stray tag than to remove one a remaining source actually has.

import json

from .tag_utils import normalize_tag_list, merge_tag_lists
from .title_utils import source_titles

# Which source's content rating wins when several are attached - see the
# long note in main.api_add_source.
SOURCE_RATING_PRIORITY = {'mangadex': 5, 'kagane': 4, 'atsu': 3, 'hive': 2, 'flame': 1, 'asura': 0}


def fetch_source_info(source_type, source_url, with_gallery=False):
    """The tracker's get_*_info() dict for one source, or None if the URL
    isn't one we can parse. Raises if the site request fails."""
    if source_type == 'mangadex':
        from .trackers.mangadex import extract_manga_id, get_manga_info_with_anilist
        manga_id = extract_manga_id(source_url)
        return get_manga_info_with_anilist(manga_id) if manga_id else None
    if source_type == 'kagane':
        from .trackers.kagane import extract_series_id, get_series_info
        kagane_id = extract_series_id(source_url)
        return get_series_info(kagane_id, with_gallery=with_gallery) if kagane_id else None
    if source_type == 'atsu':
        from .trackers.atsu import extract_series_id, get_series_info
    elif source_type == 'asura':
        from .trackers.asura import extract_series_id, get_series_info
    elif source_type == 'hive':
        from .trackers.hivetoons import extract_series_id, get_series_info
    elif source_type == 'flame':
        from .trackers.flamecomics import extract_series_id, get_series_info
    else:
        return None
    site_id = extract_series_id(source_url)
    return get_series_info(site_id) if site_id else None


def metadata_from_info(info):
    """The part of a tracker info dict that gets merged into the series."""
    return {
        'titles': source_titles(info),
        'genres': normalize_tag_list(info.get('genres')),
        'content_rating': info.get('content_rating'),
    }


def save_source_metadata(cursor, source_id, metadata):
    cursor.execute("UPDATE series_sources SET metadata = ? WHERE id = ?",
                   (json.dumps(metadata, ensure_ascii=False), source_id))


def load_source_metadata(raw):
    try:
        data = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _metadata_for(source):
    """A source's stored metadata, or fetched live. None if it can't be had."""
    stored = load_source_metadata(source.get('metadata'))
    if stored is not None:
        return stored
    try:
        info = fetch_source_info(source['source_type'], source['source_url'])
    except Exception as e:
        print(f"[Source Metadata] Couldn't fetch {source['source_url']}: {e}")
        return None
    return metadata_from_info(info) if info else None


def strip_removed_source(series_id, removed):
    """Take out of a series what `removed` (the deleted series_sources row,
    as a dict with source_url/source_type/metadata) contributed and no
    remaining source also provides. Call after the row is deleted.

    Also deletes that site's cover gallery when no remaining source is from
    the same site.

    Returns {'removed': {...}} describing exactly what was taken out (for
    the activity log, so undo can put it back), plus 'metadata' - the
    removed source's own metadata, so an undo can store it again - and
    'warning' if the titles/tags/rating had to be left alone."""
    from .database import get_db, release_db, series_search_titles
    from .search_utils import build_searchable_text

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, source_url, source_type, metadata FROM series_sources WHERE series_id = ?
        """, (series_id,))
        remaining = [dict(zip(('id', 'source_url', 'source_type', 'metadata'), r)) for r in cursor.fetchall()]
    finally:
        release_db(conn)

    result = {'removed': {}}

    # Gallery covers: only the site's own, and only if no other source of
    # that site is left to have fetched them.
    same_site_left = any(s['source_type'] == removed['source_type'] for s in remaining)
    if not same_site_left:
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT cover_url, volume, locale, note FROM gallery_covers
                WHERE series_id = ? AND source_type = ?
            """, (series_id, removed['source_type']))
            covers = [dict(zip(('cover_url', 'volume', 'locale', 'note'), r)) for r in cursor.fetchall()]
            if covers:
                cursor.execute("DELETE FROM gallery_covers WHERE series_id = ? AND source_type = ?",
                               (series_id, removed['source_type']))
                result['removed']['gallery_covers'] = covers
        finally:
            release_db(conn)

    # Metadata (network, when not stored yet) - outside any DB connection.
    removed_meta = _metadata_for(removed)
    if removed_meta is None:
        result['warning'] = "couldn't read what the removed source contributed, its titles/tags were left in place"
        return result
    result['metadata'] = removed_meta
    remaining_meta = {}
    for s in remaining:
        meta = _metadata_for(s)
        if meta is None:
            result['warning'] = f"couldn't reach {s['source_url']}, titles/tags were left in place"
            return result
        remaining_meta[s['id']] = meta

    conn = get_db()
    try:
        cursor = conn.cursor()
        # Remember what was fetched, so the next removal needn't fetch again.
        for s in remaining:
            if load_source_metadata(s['metadata']) is None:
                save_source_metadata(cursor, s['id'], remaining_meta[s['id']])

        cursor.execute("""
            SELECT title, title_en, title_romaji, title_native, alt_titles, genres, content_rating
            FROM series WHERE id = ?
        """, (series_id,))
        row = cursor.fetchone()
        if not row:
            return result
        title, title_en, title_romaji, title_native, alt_raw, genres_raw, rating = row
        try:
            alt_titles = normalize_tag_list(json.loads(alt_raw)) if alt_raw else []
        except (ValueError, TypeError):
            alt_titles = []
        try:
            genres = normalize_tag_list(json.loads(genres_raw)) if genres_raw else []
        except (ValueError, TypeError):
            genres = []

        def keys(field):
            return {v.casefold() for m in remaining_meta.values() for v in m.get(field) or []}

        def only_removed(field):
            return {v.casefold() for v in removed_meta.get(field) or []} - keys(field)

        drop_titles = only_removed('titles')
        drop_genres = only_removed('genres')
        gone_titles = [t for t in alt_titles if t.casefold() in drop_titles]
        gone_genres = [g for g in genres if g.casefold() in drop_genres]

        # Content rating: only if the removed source is the one that set it
        # (it outranked every remaining source and the series still shows
        # its rating - otherwise it was set by hand or by another source).
        new_rating = rating
        removed_rank = SOURCE_RATING_PRIORITY.get(removed['source_type'], 0)
        ranked = sorted(remaining, key=lambda s: SOURCE_RATING_PRIORITY.get(s['source_type'], 0), reverse=True)
        if (ranked and rating == removed_meta.get('content_rating')
                and removed_rank > SOURCE_RATING_PRIORITY.get(ranked[0]['source_type'], 0)):
            fallback = remaining_meta[ranked[0]['id']].get('content_rating')
            if fallback and fallback != rating:
                new_rating = fallback

        if gone_titles or gone_genres or new_rating != rating:
            alt_titles = [t for t in alt_titles if t not in gone_titles]
            genres = [g for g in genres if g not in gone_genres]
            cursor.execute("""
                UPDATE series SET alt_titles = ?, genres = ?, content_rating = ?, searchable_text = ?
                WHERE id = ?
            """, (
                json.dumps(alt_titles, ensure_ascii=False) if alt_titles else None,
                json.dumps(genres, ensure_ascii=False) if genres else None,
                new_rating,
                build_searchable_text(series_search_titles(title, title_en, title_romaji, title_native, alt_titles)),
                series_id,
            ))
            if gone_titles:
                result['removed']['alt_titles'] = gone_titles
            if gone_genres:
                result['removed']['genres'] = gone_genres
            if new_rating != rating:
                result['removed']['content_rating'] = {'old': rating, 'new': new_rating}
    finally:
        release_db(conn)
    return result


def restore_removed_source(series_id, source_id, log_old_value):
    """Undo of strip_removed_source(): put back what a source_removed log
    entry says was taken out, and store the source's metadata again on its
    re-added row."""
    from .database import get_db, release_db, save_gallery_covers, series_search_titles
    from .search_utils import build_searchable_text

    removed = log_old_value.get('removed') or {}
    save_gallery_covers(series_id, log_old_value.get('source_type'), removed.get('gallery_covers'))

    conn = get_db()
    try:
        cursor = conn.cursor()
        if source_id and log_old_value.get('metadata'):
            save_source_metadata(cursor, source_id, log_old_value['metadata'])
        if not any(k in removed for k in ('alt_titles', 'genres', 'content_rating')):
            return
        cursor.execute("""
            SELECT title, title_en, title_romaji, title_native, alt_titles, genres, content_rating
            FROM series WHERE id = ?
        """, (series_id,))
        row = cursor.fetchone()
        if not row:
            return
        title, title_en, title_romaji, title_native, alt_raw, genres_raw, rating = row
        try:
            alt_titles = normalize_tag_list(json.loads(alt_raw)) if alt_raw else []
        except (ValueError, TypeError):
            alt_titles = []
        try:
            genres = normalize_tag_list(json.loads(genres_raw)) if genres_raw else []
        except (ValueError, TypeError):
            genres = []
        alt_titles = merge_tag_lists(alt_titles, removed.get('alt_titles'))
        genres = merge_tag_lists(genres, removed.get('genres'))
        # Only revert the rating if nothing has changed it since.
        change = removed.get('content_rating')
        if change and rating == change.get('new'):
            rating = change.get('old')
        cursor.execute("""
            UPDATE series SET alt_titles = ?, genres = ?, content_rating = ?, searchable_text = ?
            WHERE id = ?
        """, (
            json.dumps(alt_titles, ensure_ascii=False) if alt_titles else None,
            json.dumps(genres, ensure_ascii=False) if genres else None,
            rating,
            build_searchable_text(series_search_titles(title, title_en, title_romaji, title_native, alt_titles)),
            series_id,
        ))
    finally:
        release_db(conn)
