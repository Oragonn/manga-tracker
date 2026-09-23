from flask import Flask, request, jsonify, render_template
from datetime import datetime, timezone
import sqlite3
import json
import os
import threading
import time
import uuid
from queue import Queue, Empty
from .activity_logger import log_activity, get_series_snapshot, detect_source_type
from .source_links import clean_source_url
from .search_utils import find_same_title_series


from .database import (
    init_db,
    update_series,
    add_series,
    series_search_titles,
    update_last_dashboard_visit,
    get_unread_reading_count,
    get_db,
    release_db
)
from .trackers.mangadex import extract_manga_id, get_manga_info_with_anilist, get_latest_chapters, get_all_covers
from .scheduler import MangaScheduler

# === Request Queue System ===
_add_queue = Queue()
_add_results = {}  # task_id -> result dict
_add_lock = threading.Lock()

class PossibleDuplicate(Exception):
    """Raised inside the add worker when the series being added shares a title
    with one that is already tracked. `result` is what add-status returns."""
    def __init__(self, result):
        super().__init__(result.get('error'))
        self.result = result


def _check_for_duplicate_titles(data, url, titles):
    """Stop an add whose titles match a series that is already tracked, so the
    dashboard can offer to attach the link to that series as another source
    instead. Only for requests that ask for it (check_duplicates) and haven't
    been answered yet (allow_duplicate), and never when the link itself is
    already tracked - the add's usual duplicate handling covers that."""
    if not data.get('check_duplicates') or data.get('allow_duplicate'):
        return
    try:
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM series WHERE source_url = ? UNION SELECT 1 FROM series_sources WHERE source_url = ?",
                (url, url)
            )
            if cursor.fetchone():
                return
            cursor.execute("SELECT id, title, searchable_text, status FROM series")
            all_rows = cursor.fetchall()
        finally:
            release_db(conn)
        statuses = {row[0]: row[3] for row in all_rows}
        matches = find_same_title_series([row[:3] for row in all_rows], titles)
    except Exception as e:
        # a failed look-up must not stop the add
        print(f"[Add Series] Duplicate title check failed: {e}")
        return
    if matches:
        raise PossibleDuplicate({
            'success': False,
            'title': titles[0],
            'possible_duplicates': [
                {'id': series_id, 'title': title, 'status': statuses.get(series_id)} for series_id, title in matches
            ],
            'error': f'You already track "{matches[0][1]}" - add this as a source of it instead?'
        })


def _find_tracked_series(url):
    """(series_id, title) of a tracked series that already has a source
    pointing at the same series as `url` on the same site, whatever form
    either link is written in; None if there is none (or `url` isn't a
    recognised series link)."""
    from .source_links import parse_source_link, find_series_ids
    link = parse_source_link(url)
    if not link:
        return None
    conn = get_db()
    try:
        cursor = conn.cursor()
        ids = find_series_ids(cursor, *link)
        if not ids:
            return None
        cursor.execute("SELECT id, title FROM series WHERE id = ?", (ids[0],))
        return cursor.fetchone()
    finally:
        release_db(conn)


class AddTask:
    def __init__(self, data, task_id):
        self.data = data
        self.task_id = task_id
        self.timestamp = time.time()

def _add_worker():
    """Process add requests one-by-one (crash-resistant)."""
    while True:
        try:
            task = _add_queue.get(timeout=1)
            if task is None:
                break

            result = {'success': False, 'error': 'Unknown error'}
            task_processed = False
            url = None  # define early for error logging

            try:
                data = task.data
                url = clean_source_url(data.get('source_url'))
                user_status = data.get('status', 'reading')

                if not url:
                    result = {'error': 'Missing source_url'}
                    task_processed = True
                    continue

                is_mangadex = url.startswith("https://mangadex.org/title/")
                is_kagane = url.startswith("https://kagane.to/series/") or url.startswith("https://kagane.org/series/")
                is_atsu = url.startswith("https://atsu.moe/manga/") or url.startswith("https://atsu.moe/read/")
                is_asura = "asurascans.com/comics/" in url
                is_hive = "hivetoons.org/series/" in url
                is_flame = "flamecomics.xyz/series/" in url

                if not (is_mangadex or is_kagane or is_atsu or is_asura or is_hive or is_flame):
                    result = {'error': 'Only MangaDex, Kagane, Atsumaru, AsuraScans, HiveToons, or Flame Comics series URLs are supported'}
                    task_processed = True
                    continue

                # The same series can be linked in more than one form
                # (kagane.org vs kagane.to, a MangaDex link with or without its
                # title slug, a trailing slash...), which the unique source_url
                # check below can't see. Compare the site's own series id.
                existing = _find_tracked_series(url)
                if existing:
                    series_id, existing_title = existing
                    try:
                        from .error_logger import log_error
                        log_error(url, f"Duplicate series: '{existing_title}' is already in your tracker",
                                  series_title=existing_title)
                    except Exception:
                        pass
                    result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                    task_processed = True
                    continue

                # Otherwise the database's unique source_url is the final
                # guard (an IntegrityError is handled below)

                if is_mangadex:
                    manga_id = extract_manga_id(url)
                    if not manga_id:
                        title = data.get('title') or "Untitled"
                        try:
                            series_id = add_series(
                                title=title, source_url=url, status=user_status,
                                cover_url=None, banner_url=None, anilist_id=None,
                                title_en=None, title_romaji=None, title_native=None,
                                source_status=None, alt_titles=None,
                                genres=[], content_rating='unknown', source_type='other'
                            )
                            result = {'id': series_id, 'success': True}

                            # Add logging
                            try:
                                log_activity(
                                    action_type='added',
                                    series_id=series_id,
                                    series_title=title,
                                    new_value={
                                        'title': title,
                                        'sources': [{
                                            'url': url,
                                            'type': 'MangaDex',
                                            'is_primary': True
                                        }],
                                        'status': user_status,
                                        'cover_url': None,
                                        'source_type': 'other'
                                    }
                                )
                            except Exception as log_err:
                                pass
                            task_processed = True
                        except sqlite3.IntegrityError as e:
                            error_str = str(e).lower()
                            if "source_url" in error_str or "unique" in error_str:
                                # Race condition or duplicate: fetch existing series
                                conn_dup = get_db()
                                cursor_dup = conn_dup.cursor()
                                cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ? "
                                    "UNION ALL SELECT s.id, s.title FROM series_sources x JOIN series s ON s.id = x.series_id "
                                    "WHERE x.source_url = ? LIMIT 1", (url, url))
                                existing = cursor_dup.fetchone()
                                release_db(conn_dup)
                                
                                if existing:
                                    series_id, existing_title = existing
                                    error_msg = f"Duplicate series: '{existing_title}' is already in your tracker"
                                    try:
                                        from .error_logger import log_error
                                        log_error(url, error_msg, series_title=existing_title)
                                    except Exception as log_err:
                                        import traceback
                                        traceback.print_exc()
                                    
                                    result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                                else:
                                    error_msg = 'This series is already in your tracker (unable to retrieve details)'
                                    
                                    # Log to logs/ folder
                                    try:
                                        from .error_logger import log_error
                                        log_error(url, error_msg, series_title="Unknown Series")
                                    except Exception as log_err:
                                        pass
                                    
                                    result = {'error': error_msg}
                                task_processed = True
                            else:
                                error_msg = f'Database integrity error: {str(e)}'
                                
                                # Log to logs/ folder
                                try:
                                    from .error_logger import log_error
                                    log_error(url, error_msg, series_title=data.get('title', 'Unknown'))
                                except Exception as log_err:
                                    pass
                                # Log to error page
                                try:
                                    from .error_logger import log_error
                                    log_error(url, error_msg, series_title=data.get('title', 'Unknown'))
                                except Exception as log_err:
                                    pass
                                result = {'error': 'Database integrity error.'}
                                task_processed = True
                        except Exception as e:
                            error_msg = str(e) or f'{type(e).__name__} (no message)'
                            try:
                                from .error_logger import log_error
                                log_error(url, error_msg, series_title=title)
                            except Exception:
                                pass
                            try:
                                from .failed_sources_logger import log_failed_source
                                log_failed_source(title, url)
                            except Exception:
                                pass
                            result = {'error': error_msg}
                            task_processed = True
                    else:
                        # Valid manga_id path
                        info = get_manga_info_with_anilist(manga_id)
                        if info:
                            title = info['title']
                            cover_url = info['cover_url']
                            mangadex_status = info['status']
                            alt_titles = info['alt_titles']
                            title_en = info.get('title_en')
                            title_romaji = info.get('title_romaji')
                            title_native = info.get('title_native')
                            banner_url = info.get('banner_url')
                            anilist_id = None
                        else:
                            title = "Unknown Manga"
                            cover_url = None
                            mangadex_status = None
                            alt_titles = None
                            title_en = None
                            title_romaji = None
                            title_native = None
                            banner_url = None
                            anilist_id = None

                        # Fetch chapters directly
                        chapters_to_save = get_latest_chapters(manga_id, limit=100)
                        if chapters_to_save is None:
                            chapters_to_save = []

                        try:
                            _check_for_duplicate_titles(data, url, series_search_titles(title, title_en, title_romaji, title_native, alt_titles))
                            series_id = add_series(
                                title=title,
                                source_url=url,
                                status=user_status,
                                cover_url=cover_url,
                                banner_url=banner_url,
                                anilist_id=anilist_id,
                                title_en=title_en,
                                title_romaji=title_romaji,
                                title_native=title_native,
                                source_status=mangadex_status,
                                alt_titles=alt_titles,
                                genres=info.get('genres', []) if info else [],
                                content_rating=info.get('content_rating', 'unknown') if info else 'unknown',
                                source_type=info.get('source_type', 'other') if info else 'other'
                            )

                            # Inject chapters
                            conn = get_db()
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
                            for ch in chapters_to_save:
                                cursor.execute("""
                                    INSERT INTO chapters (
                                        series_id, volume, raw_chapter, chapter_number,
                                        release_date, chapter_url, is_oneshot, source_type
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    series_id,
                                    ch.get('volume'),
                                    ch.get('raw_chapter', str(ch['chapter_number'])),
                                    ch['chapter_number'],
                                    ch['release_date'],
                                    ch['chapter_url'],
                                    int(ch.get('is_oneshot', False)),
                                    'mangadex'
                                ))
                            if chapters_to_save:
                                latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                # min(), not max(): a source can list the same chapter number
                                # more than once (different translator groups on Atsumaru/
                                # MangaDex), and the earliest posting is the true release date -
                                # a later repost of an already-out chapter shouldn't make it
                                # look freshly dropped.
                                latest_release = min(
                                    (ch['release_date'] for ch in chapters_to_save if ch['chapter_number'] == latest_ch and ch['release_date']),
                                    default=''
                                )
                                cursor.execute("""
                                    UPDATE series
                                    SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                    WHERE id = ?
                                """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                            release_db(conn)
                            result = {'id': series_id, 'success': True}

                            # Best-effort: grab the full MangaDex cover
                            # gallery (every volume/locale variant) for the
                            # Series Settings cover picker. Not fetching
                            # this shouldn't fail the add itself.
                            try:
                                covers = get_all_covers(manga_id)
                                from .database import save_gallery_covers
                                save_gallery_covers(series_id, 'mangadex', covers)
                            except Exception as cov_err:
                                print(f"[Add Series] Failed to fetch MangaDex cover gallery: {cov_err}")

                            # Logging
                            try:
                                log_activity(
                                    action_type='added',
                                    series_id=series_id,
                                    series_title=title,
                                    new_value={
                                        'title': title,
                                        'sources': [{
                                            'url': url,
                                            'type': 'MangaDex',
                                            'is_primary': True
                                        }],
                                        'status': user_status,
                                        'cover_url': cover_url,
                                        'source_type': info.get('source_type', 'other') if info else 'other'
                                    }
                                )
                            except Exception as log_err:
                                pass
                            # Update stats
                            try:
                                from .database import update_current_period_stats
                                update_current_period_stats()
                            except Exception as stats_err:
                                pass
                            task_processed = True
                        except PossibleDuplicate as dup:
                            result = dup.result
                            task_processed = True
                        except sqlite3.IntegrityError as e:
                            error_str = str(e).lower()
                            if "source_url" in error_str or "unique" in error_str:
                                # Race condition: fetch existing series
                                conn_dup = get_db()
                                cursor_dup = conn_dup.cursor()
                                cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ? "
                                    "UNION ALL SELECT s.id, s.title FROM series_sources x JOIN series s ON s.id = x.series_id "
                                    "WHERE x.source_url = ? LIMIT 1", (url, url))
                                existing = cursor_dup.fetchone()
                                release_db(conn_dup)
                                
                                if existing:
                                    series_id, existing_title = existing
                                    error_msg = f"Duplicate series: '{existing_title}' is already in your tracker"
                                    
                                    # Log to logs/ folder
                                    try:
                                        from .error_logger import log_error
                                        log_error(url, error_msg, series_title=existing_title)
                                    except Exception as log_err:
                                        import traceback
                                        traceback.print_exc()
                                    
                                    result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                                else:
                                    error_msg = 'This series is already in your tracker (unable to retrieve details)'
                                    
                                    # Log to logs/ folder
                                    try:
                                        from .error_logger import log_error
                                        log_error(url, error_msg, series_title="Unknown Series")
                                    except Exception as log_err:
                                        pass
                                    result = {'error': error_msg}
                                task_processed = True
                            else:
                                result = {'error': 'Database integrity error.'}
                                task_processed = True
                        except Exception as e:
                            error_msg = str(e) or f'{type(e).__name__} (no message)'
                            try:
                                from .error_logger import log_error
                                log_error(url, error_msg, series_title=title)
                            except Exception:
                                pass
                            try:
                                from .failed_sources_logger import log_failed_source
                                log_failed_source(title, url)
                            except Exception:
                                pass
                            result = {'error': error_msg}
                            task_processed = True
                
                elif is_kagane:
                    from .trackers.kagane import extract_series_id, get_series_info
                    kagane_id = extract_series_id(url)
                    if not kagane_id:
                        result = {'error': 'Invalid Kagane URL'}
                        task_processed = True
                    else:
                        # with_gallery: also download every cover in the series'
                        # gallery (same browser fetch, no extra navigation)
                        kagane_info = get_series_info(kagane_id, with_gallery=True)
                        if not kagane_info:
                            result = {'error': 'Failed to fetch Kagane series data'}
                            task_processed = True
                        else:
                            title = kagane_info['title']
                            cover_url = kagane_info['cover_url']
                            alt_titles = kagane_info.get('alt_titles') or []
                            chapters_to_save = kagane_info['chapters']
                            _chapters_source_type = 'kagane'

                            try:
                                _check_for_duplicate_titles(data, url, series_search_titles(title, None, None, None, alt_titles))
                                series_id = add_series(
                                    title=title,
                                    source_url=url,
                                    status=user_status,
                                    cover_url=cover_url,
                                    banner_url=None,
                                    anilist_id=None,
                                    title_en=None,
                                    title_romaji=None,
                                    title_native=None,
                                    source_status=kagane_info['status'],
                                    alt_titles=alt_titles,
                                    genres=kagane_info.get('genres', []),
                                    content_rating=kagane_info.get('content_rating', 'unknown'),
                                    source_type=kagane_info.get('source_type', 'other')
                                )

                                conn = get_db()
                                cursor = conn.cursor()
                                cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
                                for ch in chapters_to_save:
                                    cursor.execute("""
                                        INSERT INTO chapters (
                                            series_id, volume, raw_chapter, chapter_number,
                                            release_date, chapter_url, is_oneshot, source_type
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        series_id,
                                        None,
                                        str(ch['chapter_number']),
                                        ch['chapter_number'],
                                        ch['release_date'],
                                        ch['chapter_url'],
                                        int(ch.get('is_oneshot', False)),
                                        _chapters_source_type
                                    ))
                                if chapters_to_save:
                                    latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                    # min(), not max(): a source can list the same chapter number
                                    # more than once (different translator groups on Atsumaru/
                                    # MangaDex), and the earliest posting is the true release date -
                                    # a later repost of an already-out chapter shouldn't make it
                                    # look freshly dropped.
                                    latest_release = min(
                                        (ch['release_date'] for ch in chapters_to_save if ch['chapter_number'] == latest_ch and ch['release_date']),
                                        default=''
                                    )
                                    cursor.execute("""
                                        UPDATE series
                                        SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                        WHERE id = ?
                                    """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                                release_db(conn)
                                result = {'id': series_id, 'success': True}

                                # Best-effort: store the gallery downloaded
                                # above for the Series Settings cover picker.
                                try:
                                    from .database import save_gallery_covers
                                    save_gallery_covers(series_id, 'kagane', kagane_info.get('gallery_covers'))
                                except Exception as cov_err:
                                    print(f"[Add Series] Failed to save Kagane cover gallery: {cov_err}")

                                # Logging
                                try:
                                    log_activity(
                                        action_type='added',
                                        series_id=series_id,
                                        series_title=title,
                                        new_value={
                                            'title': title,
                                            'sources': [{
                                                'url': url,
                                                'type': 'Kagane',
                                                'is_primary': True
                                            }],
                                            'status': user_status,
                                            'cover_url': cover_url,
                                            'source_type': kagane_info.get('source_type', 'other')
                                        }
                                    )
                                except Exception as log_err:
                                    pass
                                # Update stats
                                try:
                                    from .database import update_current_period_stats
                                    update_current_period_stats()
                                except Exception as stats_err:
                                    pass
                                task_processed = True
                            except PossibleDuplicate as dup:
                                result = dup.result
                                task_processed = True
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ? "
                                    "UNION ALL SELECT s.id, s.title FROM series_sources x JOIN series s ON s.id = x.series_id "
                                    "WHERE x.source_url = ? LIMIT 1", (url, url))
                                    existing = cursor_dup.fetchone()
                                    release_db(conn_dup)
                                    
                                    if existing:
                                        series_id, existing_title = existing
                                        error_msg = f"Duplicate series: '{existing_title}' is already in your tracker"
                                        
                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title=existing_title)
                                        except Exception as log_err:
                                            import traceback
                                            traceback.print_exc()
                                        
                                        result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                                    else:
                                        error_msg = 'This series is already in your tracker (unable to retrieve details)'
                                        
                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title="Unknown Series")
                                        except Exception as log_err:
                                            pass
                                        result = {'error': error_msg}
                                    task_processed = True
                                else:
                                    result = {'error': 'Database integrity error.'}
                                    task_processed = True
                            except Exception as e:
                                error_msg = str(e) or f'{type(e).__name__} (no message)'
                                try:
                                    from .error_logger import log_error
                                    log_error(url, error_msg, series_title=title)
                                except Exception:
                                    pass
                                try:
                                    from .failed_sources_logger import log_failed_source
                                    log_failed_source(title, url)
                                except Exception:
                                    pass
                                result = {'error': error_msg}
                                task_processed = True

                elif is_atsu:
                    from .trackers.atsu import extract_series_id, get_series_info
                    atsu_id = extract_series_id(url)
                    if not atsu_id:
                        result = {'error': 'Invalid Atsumaru URL'}
                        task_processed = True
                    else:
                        # Normalize read/chapter URLs (atsu.moe often redirects a
                        # pasted series link straight to the latest chapter) to
                        # the canonical series URL before storing/logging.
                        url = f"https://atsu.moe/manga/{atsu_id}"
                        atsu_info = get_series_info(atsu_id)
                        if not atsu_info:
                            result = {'error': 'Failed to fetch Atsumaru series data'}
                            task_processed = True
                        else:
                            title = atsu_info['title']
                            cover_url = atsu_info['cover_url']
                            alt_titles = atsu_info.get('alt_titles') or []
                            chapters_to_save = atsu_info['chapters']
                            _chapters_source_type = 'atsu'

                            try:
                                _check_for_duplicate_titles(data, url, series_search_titles(title, None, None, None, alt_titles))
                                series_id = add_series(
                                    title=title,
                                    source_url=url,
                                    status=user_status,
                                    cover_url=cover_url,
                                    banner_url=None,
                                    anilist_id=None,
                                    title_en=None,
                                    title_romaji=None,
                                    title_native=None,
                                    source_status=atsu_info['status'],
                                    alt_titles=alt_titles,
                                    genres=atsu_info.get('genres', []),
                                    content_rating=atsu_info.get('content_rating', 'unknown'),
                                    source_type=atsu_info.get('source_type', 'other')
                                )

                                conn = get_db()
                                cursor = conn.cursor()
                                cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
                                for ch in chapters_to_save:
                                    cursor.execute("""
                                        INSERT INTO chapters (
                                            series_id, volume, raw_chapter, chapter_number,
                                            release_date, chapter_url, is_oneshot, source_type
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        series_id,
                                        None,
                                        str(ch['chapter_number']),
                                        ch['chapter_number'],
                                        ch['release_date'],
                                        ch['chapter_url'],
                                        int(ch.get('is_oneshot', False)),
                                        _chapters_source_type
                                    ))
                                if chapters_to_save:
                                    latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                    # min(), not max(): a source can list the same chapter number
                                    # more than once (different translator groups on Atsumaru/
                                    # MangaDex), and the earliest posting is the true release date -
                                    # a later repost of an already-out chapter shouldn't make it
                                    # look freshly dropped.
                                    latest_release = min(
                                        (ch['release_date'] for ch in chapters_to_save if ch['chapter_number'] == latest_ch and ch['release_date']),
                                        default=''
                                    )
                                    cursor.execute("""
                                        UPDATE series
                                        SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                        WHERE id = ?
                                    """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                                release_db(conn)
                                result = {'id': series_id, 'success': True}

                                # Full cover gallery for the Series Settings
                                # cover picker - on its own thread, see
                                # gallery_covers.py for why.
                                from .gallery_covers import save_atsu_gallery_in_background
                                save_atsu_gallery_in_background(series_id, atsu_id)

                                # Logging
                                try:
                                    log_activity(
                                        action_type='added',
                                        series_id=series_id,
                                        series_title=title,
                                        new_value={
                                            'title': title,
                                            'sources': [{
                                                'url': url,
                                                'type': 'Atsumaru',
                                                'is_primary': True
                                            }],
                                            'status': user_status,
                                            'cover_url': cover_url,
                                            'source_type': atsu_info.get('source_type', 'other')
                                        }
                                    )
                                except Exception as log_err:
                                    pass
                                # Update stats
                                try:
                                    from .database import update_current_period_stats
                                    update_current_period_stats()
                                except Exception as stats_err:
                                    pass
                                task_processed = True
                            except PossibleDuplicate as dup:
                                result = dup.result
                                task_processed = True
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ? "
                                    "UNION ALL SELECT s.id, s.title FROM series_sources x JOIN series s ON s.id = x.series_id "
                                    "WHERE x.source_url = ? LIMIT 1", (url, url))
                                    existing = cursor_dup.fetchone()
                                    release_db(conn_dup)

                                    if existing:
                                        series_id, existing_title = existing
                                        error_msg = f"Duplicate series: '{existing_title}' is already in your tracker"

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title=existing_title)
                                        except Exception as log_err:
                                            import traceback
                                            traceback.print_exc()

                                        result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                                    else:
                                        error_msg = 'This series is already in your tracker (unable to retrieve details)'

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title="Unknown Series")
                                        except Exception as log_err:
                                            pass
                                        result = {'error': error_msg}
                                    task_processed = True
                                else:
                                    result = {'error': 'Database integrity error.'}
                                    task_processed = True
                            except Exception as e:
                                error_msg = str(e) or f'{type(e).__name__} (no message)'
                                try:
                                    from .error_logger import log_error
                                    log_error(url, error_msg, series_title=title)
                                except Exception:
                                    pass
                                try:
                                    from .failed_sources_logger import log_failed_source
                                    log_failed_source(title, url)
                                except Exception:
                                    pass
                                result = {'error': error_msg}
                                task_processed = True

                elif is_asura:
                    from .trackers.asura import extract_series_id, get_series_info
                    asura_id = extract_series_id(url)
                    if not asura_id:
                        result = {'error': 'Invalid AsuraScans URL'}
                        task_processed = True
                    else:
                        asura_info = get_series_info(asura_id)
                        if not asura_info:
                            result = {'error': 'Failed to fetch AsuraScans series data'}
                            task_processed = True
                        else:
                            title = asura_info['title']
                            cover_url = asura_info['cover_url']
                            alt_titles = asura_info.get('alt_titles') or []
                            chapters_to_save = asura_info['chapters']
                            _chapters_source_type = 'asura'

                            try:
                                _check_for_duplicate_titles(data, url, series_search_titles(title, None, None, None, alt_titles))
                                series_id = add_series(
                                    title=title,
                                    source_url=url,
                                    status=user_status,
                                    cover_url=cover_url,
                                    banner_url=None,
                                    anilist_id=None,
                                    title_en=None,
                                    title_romaji=None,
                                    title_native=None,
                                    source_status=asura_info['status'],
                                    alt_titles=alt_titles,
                                    genres=asura_info.get('genres', []),
                                    content_rating=asura_info.get('content_rating', 'unknown'),
                                    source_type=asura_info.get('source_type', 'other')
                                )

                                conn = get_db()
                                cursor = conn.cursor()
                                cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
                                for ch in chapters_to_save:
                                    cursor.execute("""
                                        INSERT INTO chapters (
                                            series_id, volume, raw_chapter, chapter_number,
                                            release_date, chapter_url, is_oneshot, source_type
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        series_id,
                                        None,
                                        str(ch['chapter_number']),
                                        ch['chapter_number'],
                                        ch['release_date'],
                                        ch['chapter_url'],
                                        int(ch.get('is_oneshot', False)),
                                        _chapters_source_type
                                    ))
                                if chapters_to_save:
                                    latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                    # min(), not max(): a source can list the same chapter number
                                    # more than once (different translator groups on Atsumaru/
                                    # MangaDex), and the earliest posting is the true release date -
                                    # a later repost of an already-out chapter shouldn't make it
                                    # look freshly dropped.
                                    latest_release = min(
                                        (ch['release_date'] for ch in chapters_to_save if ch['chapter_number'] == latest_ch and ch['release_date']),
                                        default=''
                                    )
                                    cursor.execute("""
                                        UPDATE series
                                        SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                        WHERE id = ?
                                    """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                                release_db(conn)
                                result = {'id': series_id, 'success': True}

                                # Logging
                                try:
                                    log_activity(
                                        action_type='added',
                                        series_id=series_id,
                                        series_title=title,
                                        new_value={
                                            'title': title,
                                            'sources': [{
                                                'url': url,
                                                'type': 'AsuraScans',
                                                'is_primary': True
                                            }],
                                            'status': user_status,
                                            'cover_url': cover_url,
                                            'source_type': asura_info.get('source_type', 'other')
                                        }
                                    )
                                except Exception as log_err:
                                    pass
                                # Update stats
                                try:
                                    from .database import update_current_period_stats
                                    update_current_period_stats()
                                except Exception as stats_err:
                                    pass
                                task_processed = True
                            except PossibleDuplicate as dup:
                                result = dup.result
                                task_processed = True
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ? "
                                    "UNION ALL SELECT s.id, s.title FROM series_sources x JOIN series s ON s.id = x.series_id "
                                    "WHERE x.source_url = ? LIMIT 1", (url, url))
                                    existing = cursor_dup.fetchone()
                                    release_db(conn_dup)

                                    if existing:
                                        series_id, existing_title = existing
                                        error_msg = f"Duplicate series: '{existing_title}' is already in your tracker"

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title=existing_title)
                                        except Exception as log_err:
                                            import traceback
                                            traceback.print_exc()

                                        result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                                    else:
                                        error_msg = 'This series is already in your tracker (unable to retrieve details)'

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title="Unknown Series")
                                        except Exception as log_err:
                                            pass
                                        result = {'error': error_msg}
                                    task_processed = True
                                else:
                                    result = {'error': 'Database integrity error.'}
                                    task_processed = True
                            except Exception as e:
                                error_msg = str(e) or f'{type(e).__name__} (no message)'
                                try:
                                    from .error_logger import log_error
                                    log_error(url, error_msg, series_title=title)
                                except Exception:
                                    pass
                                try:
                                    from .failed_sources_logger import log_failed_source
                                    log_failed_source(title, url)
                                except Exception:
                                    pass
                                result = {'error': error_msg}
                                task_processed = True

                elif is_hive:
                    from .trackers.hivetoons import extract_series_id, get_series_info
                    hive_id = extract_series_id(url)
                    if not hive_id:
                        result = {'error': 'Invalid HiveToons URL'}
                        task_processed = True
                    else:
                        hive_info = get_series_info(hive_id)
                        if not hive_info:
                            result = {'error': 'Failed to fetch HiveToons series data'}
                            task_processed = True
                        else:
                            title = hive_info['title']
                            cover_url = hive_info['cover_url']
                            alt_titles = hive_info.get('alt_titles') or []
                            chapters_to_save = hive_info['chapters']
                            _chapters_source_type = 'hive'

                            try:
                                _check_for_duplicate_titles(data, url, series_search_titles(title, None, None, None, alt_titles))
                                series_id = add_series(
                                    title=title,
                                    source_url=url,
                                    status=user_status,
                                    cover_url=cover_url,
                                    banner_url=None,
                                    anilist_id=None,
                                    title_en=None,
                                    title_romaji=None,
                                    title_native=None,
                                    source_status=hive_info['status'],
                                    alt_titles=alt_titles,
                                    genres=hive_info.get('genres', []),
                                    content_rating=hive_info.get('content_rating', 'unknown'),
                                    source_type=hive_info.get('source_type', 'other')
                                )

                                conn = get_db()
                                cursor = conn.cursor()
                                cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
                                for ch in chapters_to_save:
                                    cursor.execute("""
                                        INSERT INTO chapters (
                                            series_id, volume, raw_chapter, chapter_number,
                                            release_date, chapter_url, is_oneshot, source_type
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        series_id,
                                        None,
                                        str(ch['chapter_number']),
                                        ch['chapter_number'],
                                        ch['release_date'],
                                        ch['chapter_url'],
                                        int(ch.get('is_oneshot', False)),
                                        _chapters_source_type
                                    ))
                                if chapters_to_save:
                                    latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                    # min(), not max(): a source can list the same chapter number
                                    # more than once (different translator groups on Atsumaru/
                                    # MangaDex), and the earliest posting is the true release date -
                                    # a later repost of an already-out chapter shouldn't make it
                                    # look freshly dropped.
                                    latest_release = min(
                                        (ch['release_date'] for ch in chapters_to_save if ch['chapter_number'] == latest_ch and ch['release_date']),
                                        default=''
                                    )
                                    cursor.execute("""
                                        UPDATE series
                                        SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                        WHERE id = ?
                                    """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                                release_db(conn)
                                result = {'id': series_id, 'success': True}

                                # Logging
                                try:
                                    log_activity(
                                        action_type='added',
                                        series_id=series_id,
                                        series_title=title,
                                        new_value={
                                            'title': title,
                                            'sources': [{
                                                'url': url,
                                                'type': 'HiveToons',
                                                'is_primary': True
                                            }],
                                            'status': user_status,
                                            'cover_url': cover_url,
                                            'source_type': hive_info.get('source_type', 'other')
                                        }
                                    )
                                except Exception as log_err:
                                    pass
                                # Update stats
                                try:
                                    from .database import update_current_period_stats
                                    update_current_period_stats()
                                except Exception as stats_err:
                                    pass
                                task_processed = True
                            except PossibleDuplicate as dup:
                                result = dup.result
                                task_processed = True
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ? "
                                    "UNION ALL SELECT s.id, s.title FROM series_sources x JOIN series s ON s.id = x.series_id "
                                    "WHERE x.source_url = ? LIMIT 1", (url, url))
                                    existing = cursor_dup.fetchone()
                                    release_db(conn_dup)

                                    if existing:
                                        series_id, existing_title = existing
                                        error_msg = f"Duplicate series: '{existing_title}' is already in your tracker"

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title=existing_title)
                                        except Exception as log_err:
                                            import traceback
                                            traceback.print_exc()

                                        result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                                    else:
                                        error_msg = 'This series is already in your tracker (unable to retrieve details)'

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title="Unknown Series")
                                        except Exception as log_err:
                                            pass
                                        result = {'error': error_msg}
                                    task_processed = True
                                else:
                                    result = {'error': 'Database integrity error.'}
                                    task_processed = True
                            except Exception as e:
                                error_msg = str(e) or f'{type(e).__name__} (no message)'
                                try:
                                    from .error_logger import log_error
                                    log_error(url, error_msg, series_title=title)
                                except Exception:
                                    pass
                                try:
                                    from .failed_sources_logger import log_failed_source
                                    log_failed_source(title, url)
                                except Exception:
                                    pass
                                result = {'error': error_msg}
                                task_processed = True

                elif is_flame:
                    from .trackers.flamecomics import extract_series_id, get_series_info
                    flame_id = extract_series_id(url)
                    if not flame_id:
                        result = {'error': 'Invalid Flame Comics URL'}
                        task_processed = True
                    else:
                        flame_info = get_series_info(flame_id)
                        if not flame_info:
                            result = {'error': 'Failed to fetch Flame Comics series data'}
                            task_processed = True
                        else:
                            title = flame_info['title']
                            cover_url = flame_info['cover_url']
                            alt_titles = flame_info.get('alt_titles') or []
                            chapters_to_save = flame_info['chapters']
                            _chapters_source_type = 'flame'

                            try:
                                _check_for_duplicate_titles(data, url, series_search_titles(title, None, None, None, alt_titles))
                                series_id = add_series(
                                    title=title,
                                    source_url=url,
                                    status=user_status,
                                    cover_url=cover_url,
                                    banner_url=None,
                                    anilist_id=None,
                                    title_en=None,
                                    title_romaji=None,
                                    title_native=None,
                                    source_status=flame_info['status'],
                                    alt_titles=alt_titles,
                                    genres=flame_info.get('genres', []),
                                    content_rating=flame_info.get('content_rating', 'unknown'),
                                    source_type=flame_info.get('source_type', 'other')
                                )

                                conn = get_db()
                                cursor = conn.cursor()
                                cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
                                for ch in chapters_to_save:
                                    cursor.execute("""
                                        INSERT INTO chapters (
                                            series_id, volume, raw_chapter, chapter_number,
                                            release_date, chapter_url, is_oneshot, source_type
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        series_id,
                                        None,
                                        str(ch['chapter_number']),
                                        ch['chapter_number'],
                                        ch['release_date'],
                                        ch['chapter_url'],
                                        int(ch.get('is_oneshot', False)),
                                        _chapters_source_type
                                    ))
                                if chapters_to_save:
                                    latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                    # min(), not max(): a source can list the same chapter number
                                    # more than once (different translator groups on Atsumaru/
                                    # MangaDex), and the earliest posting is the true release date -
                                    # a later repost of an already-out chapter shouldn't make it
                                    # look freshly dropped.
                                    latest_release = min(
                                        (ch['release_date'] for ch in chapters_to_save if ch['chapter_number'] == latest_ch and ch['release_date']),
                                        default=''
                                    )
                                    cursor.execute("""
                                        UPDATE series
                                        SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                        WHERE id = ?
                                    """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                                release_db(conn)
                                result = {'id': series_id, 'success': True}

                                # Logging
                                try:
                                    log_activity(
                                        action_type='added',
                                        series_id=series_id,
                                        series_title=title,
                                        new_value={
                                            'title': title,
                                            'sources': [{
                                                'url': url,
                                                'type': 'Flame Comics',
                                                'is_primary': True
                                            }],
                                            'status': user_status,
                                            'cover_url': cover_url,
                                            'source_type': flame_info.get('source_type', 'other')
                                        }
                                    )
                                except Exception as log_err:
                                    pass
                                # Update stats
                                try:
                                    from .database import update_current_period_stats
                                    update_current_period_stats()
                                except Exception as stats_err:
                                    pass
                                task_processed = True
                            except PossibleDuplicate as dup:
                                result = dup.result
                                task_processed = True
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ? "
                                    "UNION ALL SELECT s.id, s.title FROM series_sources x JOIN series s ON s.id = x.series_id "
                                    "WHERE x.source_url = ? LIMIT 1", (url, url))
                                    existing = cursor_dup.fetchone()
                                    release_db(conn_dup)

                                    if existing:
                                        series_id, existing_title = existing
                                        error_msg = f"Duplicate series: '{existing_title}' is already in your tracker"

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title=existing_title)
                                        except Exception as log_err:
                                            import traceback
                                            traceback.print_exc()

                                        result = {'id': series_id, 'title': existing_title, 'success': True, 'duplicate': True}
                                    else:
                                        error_msg = 'This series is already in your tracker (unable to retrieve details)'

                                        # Log to logs/ folder
                                        try:
                                            from .error_logger import log_error
                                            log_error(url, error_msg, series_title="Unknown Series")
                                        except Exception as log_err:
                                            pass
                                        result = {'error': error_msg}
                                    task_processed = True
                                else:
                                    result = {'error': 'Database integrity error.'}
                                    task_processed = True
                            except Exception as e:
                                error_msg = str(e) or f'{type(e).__name__} (no message)'
                                try:
                                    from .error_logger import log_error
                                    log_error(url, error_msg, series_title=title)
                                except Exception:
                                    pass
                                try:
                                    from .failed_sources_logger import log_failed_source
                                    log_failed_source(title, url)
                                except Exception:
                                    pass
                                result = {'error': error_msg}
                                task_processed = True

            except Exception as e:
                error_msg = str(e)
                result = {'error': error_msg}
                task_processed = True
                try:
                    from .error_logger import log_error
                    title_guess = data.get('title') or "Unknown"
                    log_error(url, error_msg, series_title=title_guess)
                except:
                    pass

            finally:
                from .database import release_leaked_db
                release_leaked_db()
                # Always return a result to unblock UI
                if not task_processed:
                    result = {'error': 'Internal processing error'}
                with _add_lock:
                    _add_results[task.task_id] = result
                _add_queue.task_done()

        except Empty:
            continue
        except Exception as e:
            # CRITICAL: Worker must never die
            time.sleep(1)

# Start background worker
_worker_thread = threading.Thread(target=_add_worker, daemon=True)
_worker_thread.start()

# === Flask App ===
manga_scheduler = MangaScheduler()

app = Flask(__name__,
            static_folder='../web/static',
            template_folder='../web/templates')

@app.teardown_request
def _release_leaked_db(_exc):
    # A request that raised between get_db() and release_db() would otherwise
    # keep the global DB lock forever, hanging every later request and scan.
    from .database import release_leaked_db
    release_leaked_db()

# Every file under web/static/uploads/ is content-addressed - atsu_covers and
# kagane_covers are keyed by the source's own stable image id/filename, and
# user cover uploads get a fresh uuid4 filename each time - so nothing at a
# given path is ever overwritten with different content, and it's safe to
# tell the browser to cache these forever instead of re-validating with this
# (single-process) server on every page load. This previously showed up as a
# ~0.5s-per-cover slowdown once Atsu covers moved from CDN-hotlinked (which
# sent their own long max-age) to served from here with no cache headers.
@app.route('/static/uploads/<path:filename>')
def cached_upload(filename):
    from flask import send_from_directory
    return send_from_directory(
        os.path.join(app.static_folder, 'uploads'),
        filename,
        max_age=31536000
    )

@app.route('/api/unread-error-count')
def api_unread_error_count():
    from .error_logger import get_unread_error_count
    return jsonify({'count': get_unread_error_count()})

@app.route('/api/series', methods=['POST'])
def api_add_series():
    data = request.get_json()
    if not data or 'source_url' not in data:
        return jsonify({'error': 'source_url is required'}), 400

    task_id = str(uuid.uuid4())
    task = AddTask(data, task_id)
    _add_queue.put(task)
    return jsonify({'task_id': task_id}), 202

@app.route('/api/series/add-status/<task_id>')
def api_add_status(task_id):
    with _add_lock:
        result = _add_results.get(task_id)
        if result is not None:
            _add_results.pop(task_id, None)  # always clean up
    if result is None:
        return jsonify({'status': 'pending'}), 200
    return jsonify(result), 200

# === Existing Routes (unchanged) ===

@app.route('/api/series')
def api_series():
    from .database import get_db, release_db
    # Clamped: SQLite rejects an integer this large as LIMIT/OFFSET, and
    # per_page=0 would divide by zero below. The dashboard asks for up to
    # 9999 at once when it needs the whole list.
    page = min(max(request.args.get('page', 1, type=int), 1), 1_000_000)
    per_page = min(max(request.args.get('per_page', 50, type=int), 1), 10_000)
    status_filter = request.args.get('status', 'reading').strip()
    sort_order = request.args.get('sort', 'unread_first').strip()
    sort_dir = request.args.get('dir', '').strip()
    search_query = request.args.get('search', '').strip()

    # ADD 'available_chapters' to valid sorts
    valid_sorts = ['unread_first', 'title', 'latest_release', 'last_added', 'total_chapters', 'available_chapters']
    if sort_order not in valid_sorts:
        sort_order = 'unread_first'

    # Update effective_dir logic to include available_chapters
    if sort_order in ('latest_release', 'last_added', 'available_chapters'):
        effective_dir = sort_dir if sort_dir in ('asc', 'desc') else 'desc'
    else:
        effective_dir = sort_dir if sort_dir in ('asc', 'desc') else 'asc'

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    where_parts = []
    params = []

    # Status filter. Held apart from the others and added last, so a search
    # that finds series only this filter hides can say so (hidden_matches).
    status_where = []
    status_params = []
    if status_filter != 'all':
        status_where.append("status = ?")
        status_params.append(status_filter)

    # Type filter (multi-select)
    type_filter = request.args.get('type', '').strip()
    if type_filter:
        type_list = [t.strip() for t in type_filter.split(',') if t.strip()]
        if type_list:
            placeholders = ','.join(['?'] * len(type_list))
            where_parts.append(f"source_type IN ({placeholders})")
            params.extend(type_list)

    # Genre and Rating filters with individual include/exclude modes
    genre_filter = request.args.get('genre', '').strip()
    genre_modes_filter = request.args.get('genre_modes', '').strip()
    rating_filter = request.args.get('rating', '').strip()
    rating_modes_filter = request.args.get('rating_modes', '').strip()
    
    # Parse genres with their modes
    genre_list = [g.strip() for g in genre_filter.split(',') if g.strip()] if genre_filter else []
    genre_modes = [m.strip() for m in genre_modes_filter.split(',') if m.strip()] if genre_modes_filter else []
    
    # Parse ratings with their modes
    rating_list = [r.strip() for r in rating_filter.split(',') if r.strip()] if rating_filter else []
    rating_modes = [m.strip() for m in rating_modes_filter.split(',') if m.strip()] if rating_modes_filter else []
    
    # Process genres
    if genre_list and len(genre_list) == len(genre_modes):
        include_genres = [genre_list[i] for i in range(len(genre_list)) if genre_modes[i] == 'include']
        exclude_genres = [genre_list[i] for i in range(len(genre_list)) if genre_modes[i] == 'exclude']
        
        # A tag in the dropdown may stand for several stored tags (merged
        # via Fixes > Tag), so each selection matches any of them. A banned
        # tag matches nothing, so a stale selection of one is ignored rather
        # than silently narrowing results by something no longer listed.
        from .tag_utils import load_tag_rules, tags_matching
        tag_rules = load_tag_rules(cursor)

        # Include genres - series must have ALL of these
        for g in include_genres:
            names = tags_matching(g, tag_rules)
            if not names:
                continue
            where_parts.append("(" + " OR ".join(["genres LIKE ?"] * len(names)) + ")")
            params.extend(f'%"{n}"%' for n in names)

        # Exclude genres - series must NOT have ANY of these
        for g in exclude_genres:
            for n in tags_matching(g, tag_rules):
                where_parts.append("genres NOT LIKE ?")
                params.append(f'%"{n}"%')
    
    # Process ratings. Held apart from where_parts like status_where, below --
    # ratings default to Mature/Explicit excluded (not an explicit user
    # choice), so a search hidden by it alone still gets diagnosed via
    # hidden_matches the same way one hidden by status alone does.
    rating_where = []
    rating_params = []
    if rating_list and len(rating_list) == len(rating_modes):
        include_ratings = [rating_list[i] for i in range(len(rating_list)) if rating_modes[i] == 'include']
        exclude_ratings = [rating_list[i] for i in range(len(rating_list)) if rating_modes[i] == 'exclude']

        # Include ratings - series can have ANY of these (OR logic)
        if include_ratings:
            placeholders = ','.join(['?'] * len(include_ratings))
            rating_where.append(f"content_rating IN ({placeholders})")
            rating_params.extend(include_ratings)

        # Exclude ratings - series must NOT have ANY of these
        if exclude_ratings:
            placeholders = ','.join(['?'] * len(exclude_ratings))
            rating_where.append(f"content_rating NOT IN ({placeholders})")
            rating_params.extend(exclude_ratings)

    # Publication Status filter (multi-select)
    pub_status_filter = request.args.get('pub_status', '').strip()
    if pub_status_filter:
        pub_status_list = [p.strip() for p in pub_status_filter.split(',') if p.strip()]
        if pub_status_list:
            placeholders = ','.join(['?'] * len(pub_status_list))
            where_parts.append(f"source_status IN ({placeholders})")
            params.extend(pub_status_list)

    # Readable On filter
    readable_on_filter = request.args.get('readable_on', '').strip()
    if readable_on_filter:
        readable_on_list = [s.strip() for s in readable_on_filter.split(',') if s.strip()]
        if readable_on_list:
            source_conditions = []
            for source_type in readable_on_list:
                source_conditions.append(f"EXISTS (SELECT 1 FROM series_sources WHERE series_sources.series_id = series.id AND series_sources.source_type = ?)")
                params.append(source_type)
            where_parts.append(f"({' OR '.join(source_conditions)})")

    # Custom tags filter (OR semantics, like ratings' include list -- a
    # series matches if it has ANY of the selected tags)
    custom_tags_filter = request.args.get('custom_tags', '').strip()
    if custom_tags_filter:
        tag_id_list = [t.strip() for t in custom_tags_filter.split(',') if t.strip().isdigit()]
        if tag_id_list:
            placeholders = ','.join(['?'] * len(tag_id_list))
            where_parts.append(f"""EXISTS (
                SELECT 1 FROM series_custom_tags
                WHERE series_custom_tags.series_id = series.id
                AND series_custom_tags.tag_id IN ({placeholders})
            )""")
            params.extend(tag_id_list)

    # Search filter
    search_rank_sql = None
    search_suggestions = []
    filter_clause_count = len(where_parts)
    if search_query:
        from .source_links import parse_source_link, find_series_ids
        from .search_utils import search_series, suggest_series
        text_words = []
        for word in search_query.split():
            # A pasted source link matches the series that has that source
            # attached - there's no title text in a URL to match against.
            link = parse_source_link(word)
            if link:
                matching_ids = find_series_ids(cursor, *link)
                if matching_ids:
                    where_parts.append(f"id IN ({','.join(['?'] * len(matching_ids))})")
                    params.extend(matching_ids)
                else:
                    where_parts.append("1 = 0")
                continue
            text_words.append(word)

        if text_words:
            cursor.execute("SELECT id, title, searchable_text FROM series")
            search_rows = cursor.fetchall()
            matches = search_series(search_rows, ' '.join(text_words))
            if matches is not None:
                if not matches:
                    # Nothing matched: offer the closest titles, but only among
                    # series that pass the other active filters, so picking one
                    # can't lead to another empty screen.
                    where_so_far = "WHERE " + " AND ".join(where_parts + status_where + rating_where) if where_parts or status_where or rating_where else ""
                    cursor.execute(f"SELECT id FROM series {where_so_far}", params + status_params + rating_params)
                    allowed_ids = {row[0] for row in cursor.fetchall()}
                    titles_by_id = {row[0]: row[1] for row in search_rows}
                    search_suggestions = [
                        {'id': series_id, 'title': titles_by_id[series_id]}
                        for series_id in suggest_series(search_rows, ' '.join(text_words), allowed_ids)
                    ]
                    where_parts.append("1 = 0")
                else:
                    # ids and tiers are ints from search_series, so they're
                    # written into the SQL directly rather than as thousands
                    # of bound parameters
                    where_parts.append(f"id IN ({','.join(str(int(i)) for i in matches)})")
                    if len(set(matches.values())) > 1:
                        # best matches first; the chosen sort still orders
                        # the series within each level
                        search_rank_sql = "CASE id " + " ".join(
                            f"WHEN {int(i)} THEN {int(tier)}" for i, tier in matches.items()
                        ) + " ELSE 99 END"

    # Matches that only fail on status and/or the rating filter (which is
    # usually not a choice made for this search -- Mature/Explicit are
    # excluded by default): the search itself and every other active filter
    # (type, genre, pub_status, readable_on, custom tags) are satisfied.
    # Grouped by (status, content_rating) rather than status alone, because
    # a series can be hidden by *both* at once (wrong status AND an excluded
    # rating) -- checking status in isolation would find nothing for it and
    # the dashboard would give no explanation at all, which is exactly what
    # was happening for a Plan to Read series tagged Mature.
    hidden_matches = []
    if (status_where or rating_where) and len(where_parts) > filter_clause_count:
        status_ok = "status = ?" if status_where else "1=1"
        rating_ok = "(" + " AND ".join(rating_where) + ")" if rating_where else "1=1"
        cursor.execute(
            f"SELECT status, content_rating, COUNT(*) FROM series WHERE {' AND '.join(where_parts)} "
            f"AND NOT ({status_ok} AND {rating_ok}) GROUP BY status, content_rating",
            params + ([status_filter] if status_where else []) + rating_params
        )
        hidden_matches = [
            {'status': status, 'rating': rating, 'count': count}
            for status, rating, count in cursor.fetchall() if status and count
        ]

    where_parts += status_where
    params += status_params
    where_parts += rating_where
    params += rating_params
    where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    # ADD available_chapters sorting logic
    if sort_order == 'unread_first':
        inverted_dir = 'asc' if effective_dir == 'desc' else 'desc'
        # latest_release applies within BOTH groups (unread and caught-up),
        # not just the unread one - the old CASE...END with no ELSE made it
        # NULL for every caught-up row, so that group silently fell through
        # to the title tiebreaker instead of being date-sorted too.
        order_by = f"""
        ORDER BY
          (COALESCE(latest_chapter, -1) > current_chapter) DESC,
          latest_release {inverted_dir.upper()},
          title ASC
        """
    elif sort_order == 'latest_release':
        inverted_dir = 'asc' if effective_dir == 'desc' else 'desc'
        order_by = f"ORDER BY latest_release {inverted_dir.upper()}"
    elif sort_order == 'last_added':
        inverted_dir = 'asc' if effective_dir == 'desc' else 'desc'
        order_by = f"ORDER BY created_at {inverted_dir.upper()}"
    elif sort_order == 'title':
        order_by = f"ORDER BY title {effective_dir.upper()}"
    elif sort_order == 'total_chapters':
        order_by = f"ORDER BY total_chapters {effective_dir.upper()}"
    elif sort_order == 'available_chapters':
        # Sort by (latest_chapter - current_chapter)
        # desc = most unread first, asc = least unread first
        inverted_dir = 'asc' if effective_dir == 'desc' else 'desc'
        order_by = f"ORDER BY (COALESCE(latest_chapter, 0) - current_chapter) {inverted_dir.upper()}"

    if search_rank_sql:
        order_by = order_by.replace("ORDER BY", f"ORDER BY {search_rank_sql},", 1)

    count_query = f"SELECT COUNT(*) FROM series {where_clause}"
    cursor.execute(count_query, params)
    total = cursor.fetchone()[0]

    offset = (page - 1) * per_page
    query = f"""
        SELECT *,
               COALESCE(latest_chapter, 0) - current_chapter AS unread_count
        FROM series
        {where_clause}
        {order_by}
        LIMIT ? OFFSET ?
    """
    cursor.execute(query, params + [per_page, offset])
    rows = cursor.fetchall()
    
    # Convert rows to dictionaries
    items = [dict(row) for row in rows]
    
    # Fetch chapters for all series in this page
    if items:
        series_ids = [item['id'] for item in items]
        placeholders = ','.join(['?'] * len(series_ids))
        
        # Check which columns exist in chapters table
        cursor.execute("PRAGMA table_info(chapters)")
        cols = {row[1] for row in cursor.fetchall()}
        
        # Build SELECT fields based on available columns
        select_fields = ["series_id", "chapter_number", "chapter_url"]
        if "volume" in cols:
            select_fields.append("volume")
        else:
            select_fields.append("NULL as volume")
        if "raw_chapter" in cols:
            select_fields.append("raw_chapter")
        else:
            select_fields.append("NULL as raw_chapter")
        if "is_oneshot" in cols:
            select_fields.append("is_oneshot")
        else:
            select_fields.append("CASE WHEN chapter_number = 0.0 THEN 1 ELSE 0 END as is_oneshot")
        if "source_type" in cols:
            select_fields.append("source_type")
        else:
            select_fields.append("NULL as source_type")

        # Fetch all chapters for these series
        chapters_query = f"""
            SELECT {', '.join(select_fields)}
            FROM chapters
            WHERE series_id IN ({placeholders})
            ORDER BY series_id, chapter_number ASC
        """
        cursor.execute(chapters_query, series_ids)
        chapter_rows = cursor.fetchall()

        # Group chapters by series_id
        chapters_by_series = {}
        for row in chapter_rows:
            series_id = row[0]
            chapter = {
                'chapter_number': row[1],
                'chapter_url': row[2],
                'volume': row[3],
                'raw_chapter': row[4],
                'is_oneshot': bool(row[5]),
                'source_type': row[6]
            }
            if series_id not in chapters_by_series:
                chapters_by_series[series_id] = []
            chapters_by_series[series_id].append(chapter)
        
        # Add chapters to each series item
        for item in items:
            item['chapters'] = chapters_by_series.get(item['id'], [])
    
    release_db(conn)

    total_pages = (total + per_page - 1) // per_page
    response = {
        'items': items,
        'total_pages': total_pages,
        'current_page': page
    }
    if search_suggestions:
        response['suggestions'] = search_suggestions
    if hidden_matches:
        response['hidden_matches'] = hidden_matches
    return jsonify(response)

@app.route('/api/genres')
def api_genres():
    try:
        from .tag_utils import load_tag_rules, count_tags
        conn = get_db()
        cursor = conn.cursor()
        # Only select non-empty, non-null-looking strings
        cursor.execute("""
            SELECT genres FROM series
            WHERE genres IS NOT NULL
              AND genres != ''
              AND genres NOT LIKE 'null'
              AND genres LIKE '[%'
        """)
        rows = [row[0] for row in cursor.fetchall()]
        rules = load_tag_rules(cursor)
        release_db(conn)

        # Tags with a merge or ban rule (Fixes page > Tag) are folded into
        # their target / left out here. Sources also disagree on casing
        # ("Slice of Life" vs "Slice Of Life"), so each tag is listed once
        # under its most common spelling -- the filter query's LIKE is
        # case-insensitive, so that one entry still matches series stored
        # with either spelling.
        genres = [entry['tag'] for entry in count_tags(rows, rules)]
        return jsonify(sorted(genres, key=str.casefold))
    except Exception as e:
        print(f"[Genres API] Error: {e}")
        return jsonify([]), 500


# User-defined tags -- separate from the scraped `genres` column above.
@app.route('/api/custom-tags')
def api_get_custom_tags():
    from .database import get_custom_tags
    return jsonify(get_custom_tags())


@app.route('/api/custom-tags', methods=['POST'])
def api_create_custom_tag():
    from .database import create_custom_tag
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    if len(name) > 40:
        return jsonify({'error': 'name too long (max 40 characters)'}), 400

    tag_id = create_custom_tag(name)
    if tag_id is None:
        return jsonify({'error': 'Failed to create tag'}), 500
    return jsonify({'id': tag_id, 'name': name}), 200


@app.route('/api/custom-tags/<int:tag_id>', methods=['DELETE'])
def api_delete_custom_tag(tag_id):
    from .database import delete_custom_tag
    if delete_custom_tag(tag_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Tag not found'}), 404


@app.route('/api/series/<int:series_id>/custom-tags')
def api_get_series_custom_tags(series_id):
    from .database import get_series_custom_tag_ids
    return jsonify({'tag_ids': get_series_custom_tag_ids(series_id)})


@app.route('/api/series/<int:series_id>/custom-tags/<int:tag_id>', methods=['POST'])
def api_add_series_custom_tag(series_id, tag_id):
    from .database import add_custom_tag_to_series
    if add_custom_tag_to_series(series_id, tag_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to attach tag'}), 500


@app.route('/api/series/<int:series_id>/custom-tags/<int:tag_id>', methods=['DELETE'])
def api_remove_series_custom_tag(series_id, tag_id):
    from .database import remove_custom_tag_from_series
    if remove_custom_tag_from_series(series_id, tag_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to detach tag'}), 500

# Quick-capture "Save for Later" scratch list - a title or link the user
# wants to look at later, unrelated to the tracked series table.
@app.route('/api/later')
def api_get_later_items():
    from .database import get_later_items
    return jsonify({'items': get_later_items()})


@app.route('/api/later', methods=['POST'])
def api_create_later_item():
    from .database import create_later_item
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    url = (data.get('url') or '').strip()
    if not title and not url:
        return jsonify({'error': 'A title or link is required'}), 400
    new_id = create_later_item(title or None, url or None)
    if new_id is None:
        return jsonify({'error': 'Failed to save item'}), 500
    return jsonify({'id': new_id}), 201


@app.route('/api/later/<int:item_id>', methods=['DELETE'])
def api_delete_later_item(item_id):
    from .database import delete_later_item
    if delete_later_item(item_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Item not found'}), 404


@app.route('/api/later/<int:item_id>', methods=['PATCH'])
def api_update_later_item(item_id):
    from .database import update_later_item
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    url = (data.get('url') or '').strip()
    if not title and not url:
        return jsonify({'error': 'A title or link is required'}), 400
    if not update_later_item(item_id, title or None, url or None):
        return jsonify({'error': 'Item not found'}), 404
    return jsonify({'success': True})


# Saved filter/sort combinations the dashboard's bookmark dropdown
# switches between - "Default" (is_builtin) is seeded in init_db() and
# protected from rename/delete at the database layer.
@app.route('/api/filter-bookmarks')
def api_get_filter_bookmarks():
    from .database import get_filter_bookmarks
    return jsonify({'bookmarks': get_filter_bookmarks()})


@app.route('/api/filter-bookmarks', methods=['POST'])
def api_create_filter_bookmark():
    from .database import create_filter_bookmark
    from .activity_logger import log_activity
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    filter_state = data.get('filter_state')
    if not name:
        return jsonify({'error': 'name is required'}), 400
    if not isinstance(filter_state, dict):
        return jsonify({'error': 'filter_state is required'}), 400
    new_id = create_filter_bookmark(name, filter_state)
    if new_id is None:
        return jsonify({'error': 'Failed to create bookmark'}), 500
    try:
        log_activity(action_type='bookmark_added', series_title=name,
                     new_value={'id': new_id, 'name': name, 'filter_state': filter_state})
    except Exception as log_err:
        print(f"[Filter Bookmark] Logging failed: {log_err}")
    return jsonify({'id': new_id}), 201


@app.route('/api/filter-bookmarks/<int:bookmark_id>', methods=['PATCH'])
def api_update_filter_bookmark(bookmark_id):
    from .database import update_filter_bookmark, get_filter_bookmarks
    from .activity_logger import log_activity
    data = request.get_json() or {}
    name = data.get('name')
    filter_state = data.get('filter_state')
    if name is not None:
        name = name.strip()
        if not name:
            return jsonify({'error': 'name cannot be empty'}), 400

    before = next((b for b in get_filter_bookmarks() if b['id'] == bookmark_id), None)

    ok, err = update_filter_bookmark(bookmark_id, name=name, filter_state=filter_state)
    if not ok:
        return jsonify({'error': err}), 400

    try:
        if before:
            old_name = before['name']
            new_name = name if name is not None else old_name
            new_filter_state = filter_state if filter_state is not None else before['filter_state']
            log_activity(
                action_type='bookmark_updated',
                series_title=new_name,
                old_value={'id': bookmark_id, 'name': old_name, 'filter_state': before['filter_state']},
                new_value={
                    'id': bookmark_id,
                    'name': new_name,
                    'filter_state': new_filter_state,
                    'renamed': bool(name is not None and name != old_name)
                }
            )
    except Exception as log_err:
        print(f"[Filter Bookmark] Logging failed: {log_err}")
    return jsonify({'success': True})


@app.route('/api/filter-bookmarks/<int:bookmark_id>', methods=['DELETE'])
def api_delete_filter_bookmark(bookmark_id):
    from .database import delete_filter_bookmark, get_filter_bookmarks
    from .activity_logger import log_activity
    before = next((b for b in get_filter_bookmarks() if b['id'] == bookmark_id), None)
    ok, err = delete_filter_bookmark(bookmark_id)
    if not ok:
        return jsonify({'error': err}), 400
    try:
        if before:
            log_activity(action_type='bookmark_deleted', series_title=before['name'],
                         old_value={'name': before['name'], 'filter_state': before['filter_state']})
    except Exception as log_err:
        print(f"[Filter Bookmark] Logging failed: {log_err}")
    return jsonify({'success': True})


@app.route('/api/unread-reading-count')
def api_unread_count():
    count = get_unread_reading_count()
    return jsonify({'count': count})

@app.route('/api/series/<int:series_id>')
def api_get_single_series(series_id):
    """Single-series fetch, same row shape as the /api/series list items -
    used by the frontend to refresh one card in place after an edit instead
    of reloading the whole grid."""
    from .database import get_db, release_db
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT *, COALESCE(latest_chapter, 0) - current_chapter AS unread_count
        FROM series WHERE id = ?
    """, (series_id,))
    row = cursor.fetchone()
    release_db(conn)
    if not row:
        return jsonify({'error': 'Series not found'}), 404
    return jsonify(dict(row))


@app.route('/api/series/<int:series_id>', methods=['PATCH'])
def api_update_series(series_id):
    data = request.get_json()
    
    # Strip internal bulk tracking fields
    _bulk_id = data.pop('_bulk_id', None)
    _is_bulk = data.pop('_is_bulk', False)
    
    # REMOVE 'source_url' from allowed_fields
    allowed_fields = {'current_chapter', 'current_volume', 'status', 'cover_url', 'title', 'source_type', 'content_rating'}
    updates = {k: v for k, v in data.items() if k in allowed_fields}

    # source_type here is the series' content type (what the dashboard's
    # Content Type filter uses), not one of its source sites
    content_types = ('manga', 'manhwa', 'manhua', 'other')
    if 'source_type' in updates and updates['source_type'] not in content_types:
        return jsonify({'error': f"source_type must be one of: {', '.join(content_types)}"}), 400

    # content_rating is the age rating the Tags filter's rating section uses
    # (mild = "Suggestive"). 'unknown' exists on some series but isn't a choice.
    content_ratings = ('safe', 'mild', 'mature', 'explicit')
    if 'content_rating' in updates and updates['content_rating'] not in content_ratings:
        return jsonify({'error': f"content_rating must be one of: {', '.join(content_ratings)}"}), 400
    
    if 'current_chapter' in updates:
        val = updates['current_chapter']
        if val is None or str(val).lower() == 'null' or val == '':
            updates['current_chapter'] = -1.0
            updates['current_volume'] = None
        else:
            updates['current_chapter'] = float(val)
    
    if not updates:
        return jsonify({'error': 'No valid fields to update'}), 400
    
    # Get old values BEFORE updating
    try:
        conn_old = get_db()
        cursor_old = conn_old.cursor()
        cursor_old.execute("SELECT title, current_chapter, status, cover_url, source_type, content_rating FROM series WHERE id = ?", (series_id,))
        old_row = cursor_old.fetchone()
        release_db(conn_old)
        
        if old_row:
            old_title, old_chapter, old_status, old_cover, old_type, old_rating = old_row
            
            # One entry per kind of change, so a save that changes several
            # at once (Series Settings sends chapter, status, title and
            # cover together) logs - and can undo - each of them
            if 'current_chapter' in updates and old_chapter != updates['current_chapter']:
                log_activity(
                    action_type='progress',
                    series_id=series_id,
                    series_title=old_title,
                    old_value={'chapter': old_chapter},
                    new_value={'chapter': updates['current_chapter']},
                    is_bulk=_is_bulk,
                    bulk_id=_bulk_id
                )
                # ADDED: Update current period stats
                try:
                    from .database import update_current_period_stats
                    update_current_period_stats()
                except Exception as stats_err:
                    print(f"[Update] Stats update failed: {stats_err}")
            if 'status' in updates and old_status != updates['status']:
                log_activity(
                    action_type='status',
                    series_id=series_id,
                    series_title=old_title,
                    old_value={'status': old_status},
                    new_value={'status': updates['status']},
                    is_bulk=_is_bulk,
                    bulk_id=_bulk_id
                )
            old_vals = {}
            new_vals = {}
            if 'title' in updates and old_title != updates['title']:
                old_vals['title'] = old_title
                new_vals['title'] = updates['title']
            if 'cover_url' in updates and old_cover != updates['cover_url']:
                old_vals['cover_url'] = old_cover
                new_vals['cover_url'] = updates['cover_url']
            if new_vals:
                log_activity(
                    action_type='edited',
                    series_id=series_id,
                    series_title=old_title,
                    old_value=old_vals,
                    new_value=new_vals,
                    is_bulk=_is_bulk,
                    bulk_id=_bulk_id
                )

            # Content type and content rating are logged together, in one entry
            # of their own, with their own undo.
            classification_old = {}
            classification_new = {}
            if 'source_type' in updates and old_type != updates['source_type']:
                classification_old['source_type'] = old_type
                classification_new['source_type'] = updates['source_type']
            if 'content_rating' in updates and old_rating != updates['content_rating']:
                classification_old['content_rating'] = old_rating
                classification_new['content_rating'] = updates['content_rating']
            if classification_new:
                log_activity(
                    action_type='edited',
                    series_id=series_id,
                    series_title=old_title,
                    old_value=classification_old,
                    new_value=classification_new,
                    is_bulk=_is_bulk,
                    bulk_id=_bulk_id
                )
    except Exception as log_err:
        print(f"[Update] Logging failed: {log_err}")
    
    # Perform update
    update_series(series_id, updates)
    return jsonify({'success': True})


# Covers uploaded via the Series Settings modal's "Upload image" option --
# saved under the Flask static folder so they're servable at /static/... like
# any other asset, filenames namespaced by series id + a random suffix so
# repeated uploads for the same series never collide or overwrite silently.
UPLOAD_COVER_DIR = os.path.join(os.path.dirname(__file__), '..', 'web', 'static', 'uploads', 'covers')
ALLOWED_COVER_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
MAX_COVER_UPLOAD_BYTES = 8 * 1024 * 1024  # 8MB


@app.route('/api/series/<int:series_id>/cover-upload', methods=['POST'])
def api_upload_cover(series_id):
    file = request.files.get('cover')
    if not file or not file.filename:
        return jsonify({'error': 'No file provided'}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_COVER_EXTENSIONS:
        return jsonify({'error': 'Unsupported image type'}), 400

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_COVER_UPLOAD_BYTES:
        return jsonify({'error': 'File too large (max 8MB)'}), 400

    os.makedirs(UPLOAD_COVER_DIR, exist_ok=True)
    filename = f"{series_id}_{uuid.uuid4().hex}{ext}"
    file.save(os.path.join(UPLOAD_COVER_DIR, filename))

    cover_url = f'/static/uploads/covers/{filename}'

    from .database import add_series_cover
    cover_id = add_series_cover(series_id, cover_url)

    return jsonify({'cover_url': cover_url, 'id': cover_id}), 200


@app.route('/api/series/<int:series_id>/uploaded-covers')
def api_get_uploaded_covers(series_id):
    """Covers previously uploaded for this series, so the settings-modal
    cover picker can offer them again without re-uploading."""
    from .database import get_series_covers
    return jsonify({'covers': get_series_covers(series_id)})


@app.route('/api/series/<int:series_id>/gallery-covers')
def api_get_gallery_covers(series_id):
    """The full cover gallery (every volume/locale variant) fetched when a
    MangaDex, Atsumaru or Kagane source was added, for the Series Settings
    cover picker. Each entry says which source it came from."""
    from .database import get_gallery_covers
    return jsonify({'covers': get_gallery_covers(series_id)})


@app.route('/api/series/<int:series_id>/uploaded-covers/<int:cover_id>', methods=['DELETE'])
def api_delete_uploaded_cover(series_id, cover_id):
    from .database import delete_series_cover
    cover_url = delete_series_cover(cover_id, series_id)
    if not cover_url:
        return jsonify({'error': 'Cover not found'}), 404

    # Only ever unlink files we saved ourselves under the uploads dir --
    # never touch an arbitrary path even if cover_url were ever something else.
    if cover_url.startswith('/static/uploads/covers/'):
        file_path = os.path.join(UPLOAD_COVER_DIR, os.path.basename(cover_url))
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"[Delete Cover] Failed to remove file {file_path}: {e}")

    return jsonify({'success': True})


@app.route('/api/series/<int:series_id>/check-now', methods=['POST'])
def api_check_now(series_id):
    try:
        manga_scheduler.scan_series(series_id)
        return jsonify({'success': True, 'message': 'Checked successfully'})
    except Exception as e:
        print(f"[Check Now] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/series/<int:series_id>', methods=['DELETE'])
def api_delete_series(series_id):
    from .activity_logger import get_series_snapshot
    from .database import get_progress_history_for_stats
    snapshot = get_series_snapshot(series_id)
    # Captured before delete_series() runs - it clears series_id off these
    # rows (ON DELETE SET NULL), so undoing this deletion later couldn't
    # find them any other way.
    progress_history = get_progress_history_for_stats(series_id)

    bulk_id = request.args.get('bulk_id')
    is_bulk = bulk_id is not None

    try:
        # *** Use new delete_series function ***
        from .database import delete_series
        success = delete_series(series_id)

        if not success:
            return jsonify({'error': 'Series not found or delete failed'}), 404

        # Log after successful delete
        if snapshot:
            try:
                from .activity_logger import log_activity
                snapshot['_progress_history'] = progress_history
                log_activity(
                    action_type='deleted',
                    series_id=None,
                    series_title=snapshot['title'],
                    old_value=snapshot,
                    is_bulk=is_bulk,
                    bulk_id=bulk_id
                )
            except Exception as log_err:
                print(f"[Delete] Logging failed: {log_err}")
        
        # ADDED: Update current period stats after deletion
        try:
            from .database import update_current_period_stats
            update_current_period_stats()
        except Exception as stats_err:
            print(f"[Delete] Stats update failed: {stats_err}")
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/series/<int:series_id>/chapters')
def api_series_chapters(series_id):
    from .database import get_db, release_db
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(chapters)")
    cols = {row[1] for row in cursor.fetchall()}
    
    select_fields = ["chapter_number", "chapter_url"]
    if "volume" in cols:
        select_fields.append("volume")
    else:
        select_fields.append("NULL as volume")
    if "raw_chapter" in cols:
        select_fields.append("raw_chapter")
    else:
        select_fields.append("NULL as raw_chapter")
    if "is_oneshot" in cols:
        select_fields.append("is_oneshot")
    else:
        select_fields.append("CASE WHEN chapter_number = 0.0 THEN 1 ELSE 0 END as is_oneshot")
    if "source_type" in cols:
        select_fields.append("source_type")
    else:
        select_fields.append("NULL as source_type")
    if "release_date" in cols:
        select_fields.append("release_date")
    else:
        select_fields.append("NULL as release_date")

    query = f"SELECT {', '.join(select_fields)} FROM chapters WHERE series_id = ? ORDER BY chapter_number ASC"
    cursor.execute(query, (series_id,))
    rows = cursor.fetchall()
    release_db(conn)

    result = []
    for row in rows:
        r = {
            'chapter_number': row[0],
            'chapter_url': row[1],
            'volume': row[2],
            'raw_chapter': row[3],
            'is_oneshot': bool(row[4]),
            'source_type': row[5],
            'release_date': row[6]
        }
        result.append(r)
    return jsonify(result)

def save_completed_period_stats():
    """
    Check if any periods have ended and save their stats.
    Call this periodically (e.g., daily via scheduler or on stats page load).
    """
    from .database import get_db, release_db
    from datetime import datetime, timezone, timedelta
    import json
    
    conn = None
    try:
        now = datetime.now(timezone.utc)
        conn = get_db()
        cursor = conn.cursor()
        
        # Check last saved periods
        try:
            cursor.execute("SELECT period_type, MAX(period_start) FROM stats_history GROUP BY period_type")
            last_saved = {row[0]: row[1] for row in cursor.fetchall()}
        except Exception as table_err:
            # Table might not exist or be empty
            last_saved = {}
        
        # === SAVE YESTERDAY'S STATS (if not already saved) ===
        yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        yesterday_str = yesterday.date().isoformat()
        
        if 'day' not in last_saved or last_saved['day'] < yesterday_str:
            # Count series added yesterday
            cursor.execute("""
                SELECT COUNT(*) FROM series
                WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) <= DATETIME(?)
            """, (yesterday.isoformat(), yesterday_end.isoformat()))
            series_added = cursor.fetchone()[0] or 0
            
            # Count chapters read yesterday
            cursor.execute("""
                SELECT old_value, new_value
                FROM activity_log
                WHERE action_type = 'progress'
                AND can_undo = 1
                AND timestamp >= ? AND timestamp <= ?
            """, (yesterday.isoformat(), yesterday_end.isoformat()))
            
            chapters_read = 0
            for old_str, new_str in cursor.fetchall():
                try:
                    old_val = json.loads(old_str) if old_str else {}
                    new_val = json.loads(new_str) if new_str else {}
                    old_ch = old_val.get('chapter', -1)
                    new_ch = new_val.get('chapter', -1)
                    if old_ch >= 0 and new_ch >= 0:
                        chapters_read += float(new_ch) - float(old_ch)
                except:
                    continue
            
            # Use existing cursor instead of calling save_period_stats() to avoid deadlock
            cursor.execute("""
                INSERT OR REPLACE INTO stats_history 
                (period_type, period_start, period_end, series_added, chapters_read)
                VALUES (?, ?, ?, ?, ?)
            """, ('day', yesterday_str, yesterday_str, series_added, chapters_read))
        
        # === SAVE LAST WEEK'S STATS (if week is complete) ===
        # Week ends on Sunday (weekday 6)
        if now.weekday() == 0:  # It's Monday, so last week just ended
            last_week_end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=999999)
            last_week_start = (last_week_end - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
            week_str = last_week_start.date().isoformat()
            
            if 'week' not in last_saved or last_saved['week'] < week_str:
                cursor.execute("""
                    SELECT COUNT(*) FROM series
                    WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) <= DATETIME(?)
                """, (last_week_start.isoformat(), last_week_end.isoformat()))
                series_added = cursor.fetchone()[0] or 0
                
                cursor.execute("""
                    SELECT old_value, new_value
                    FROM activity_log
                    WHERE action_type = 'progress'
                    AND can_undo = 1
                    AND timestamp >= ? AND timestamp <= ?
                """, (last_week_start.isoformat(), last_week_end.isoformat()))
                
                chapters_read = 0
                for old_str, new_str in cursor.fetchall():
                    try:
                        old_val = json.loads(old_str) if old_str else {}
                        new_val = json.loads(new_str) if new_str else {}
                        old_ch = old_val.get('chapter', -1)
                        new_ch = new_val.get('chapter', -1)
                        if old_ch >= 0 and new_ch >= 0:
                            chapters_read += float(new_ch) - float(old_ch)
                    except:
                        continue
                
                # Use existing cursor instead of calling save_period_stats() to avoid deadlock
                cursor.execute("""
                    INSERT OR REPLACE INTO stats_history 
                    (period_type, period_start, period_end, series_added, chapters_read)
                    VALUES (?, ?, ?, ?, ?)
                """, ('week', week_str, last_week_end.date().isoformat(), series_added, chapters_read))
        
        # === SAVE LAST MONTH'S STATS (if month is complete) ===
        if now.day == 1:  # It's the 1st, so last month just ended
            last_month_end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=999999)
            last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month_str = last_month_start.date().isoformat()
            
            if 'month' not in last_saved or last_saved['month'] < month_str:
                cursor.execute("""
                    SELECT COUNT(*) FROM series
                    WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) <= DATETIME(?)
                """, (last_month_start.isoformat(), last_month_end.isoformat()))
                series_added = cursor.fetchone()[0] or 0
                
                cursor.execute("""
                    SELECT old_value, new_value
                    FROM activity_log
                    WHERE action_type = 'progress'
                    AND can_undo = 1
                    AND timestamp >= ? AND timestamp <= ?
                """, (last_month_start.isoformat(), last_month_end.isoformat()))
                
                chapters_read = 0
                for old_str, new_str in cursor.fetchall():
                    try:
                        old_val = json.loads(old_str) if old_str else {}
                        new_val = json.loads(new_str) if new_str else {}
                        old_ch = old_val.get('chapter', -1)
                        new_ch = new_val.get('chapter', -1)
                        if old_ch >= 0 and new_ch >= 0:
                            chapters_read += float(new_ch) - float(old_ch)
                    except:
                        continue
                
                # Use existing cursor instead of calling save_period_stats() to avoid deadlock
                cursor.execute("""
                    INSERT OR REPLACE INTO stats_history 
                    (period_type, period_start, period_end, series_added, chapters_read)
                    VALUES (?, ?, ?, ?, ?)
                """, ('month', month_str, last_month_end.date().isoformat(), series_added, chapters_read))
        
        # === SAVE LAST YEAR'S STATS (if year is complete) ===
        if now.month == 1 and now.day == 1:  # It's January 1st, so last year just ended
            last_year_end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=999999)
            last_year_start = last_year_end.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            year_str = last_year_start.date().isoformat()
            
            if 'year' not in last_saved or last_saved['year'] < year_str:
                cursor.execute("""
                    SELECT COUNT(*) FROM series
                    WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) <= DATETIME(?)
                """, (last_year_start.isoformat(), last_year_end.isoformat()))
                series_added = cursor.fetchone()[0] or 0

                # The year's month rows, not a recount of activity_log - it
                # only keeps the last 30 days of progress
                cursor.execute("""
                    SELECT COALESCE(SUM(chapters_read), 0) FROM stats_history
                    WHERE period_type = 'month' AND period_start >= ? AND period_start <= ?
                """, (year_str, last_year_end.date().isoformat()))
                chapters_read = round(cursor.fetchone()[0], 1)

                # Use existing cursor instead of calling save_period_stats() to avoid deadlock
                cursor.execute("""
                    INSERT OR REPLACE INTO stats_history
                    (period_type, period_start, period_end, series_added, chapters_read)
                    VALUES (?, ?, ?, ?, ?)
                """, ('year', year_str, last_year_end.date().isoformat(), series_added, chapters_read))
        
    except Exception as e:
        print(f"[Stats] Failed to save completed period stats: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # CRITICAL: Always release the database lock, even if there was an error
        if conn is not None:
            try:
                release_db(conn)
            except Exception as release_err:
                print(f"[Stats] Failed to release DB in save_completed_period_stats: {release_err}")

@app.route('/stats')
def stats_page():
    """Render stats page."""
    return render_template('stats.html')

@app.route('/api/stats')
def api_get_stats():
    """
    Everything the Stats page shows: library totals, the breakdowns behind its
    donut charts, a 30-day reading/release history and a few ranked lists.
    """
    try:
        # Save completed period stats before calculating current stats
        try:
            save_completed_period_stats()
        except Exception as save_err:
            print(f"[Stats] Failed to save period stats (continuing anyway): {save_err}")
            import traceback
            traceback.print_exc()

        from datetime import date, timedelta
        from .database import get_db, release_db
        from .tag_utils import load_tag_rules, count_tags

        HISTORY_DAYS = 30
        BULK_DAY_CHAPTERS = 500
        today = datetime.now(timezone.utc).date()
        history_start = today - timedelta(days=HISTORY_DAYS - 1)

        conn = get_db()
        try:
            cursor = conn.cursor()

            # === LIBRARY ===
            cursor.execute("""
                SELECT
                    COUNT(*),
                    COALESCE(SUM(CASE WHEN current_chapter != -1 THEN current_chapter END), 0),
                    COALESCE(SUM(latest_chapter), 0),
                    SUM(CASE WHEN current_chapter = -1 THEN 1 ELSE 0 END)
                FROM series
            """)
            total_series, chapters_read, chapters_available, not_started = cursor.fetchone()

            # Reading list: caught up vs behind, and the chapters waiting
            cursor.execute("""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN current_chapter != -1
                             AND current_chapter >= COALESCE(latest_chapter, 0) THEN 1 ELSE 0 END),
                    SUM(CASE WHEN latest_chapter > current_chapter THEN 1 ELSE 0 END),
                    COALESCE(SUM(CASE WHEN latest_chapter > current_chapter
                                      THEN latest_chapter - MAX(current_chapter, 0) END), 0)
                FROM series
                WHERE status = 'reading'
            """)
            reading_count, caught_up, behind, reading_backlog = cursor.fetchone()

            # === BREAKDOWNS ===
            def breakdown(column):
                cursor.execute(f"SELECT COALESCE({column}, 'unknown'), COUNT(*) FROM series GROUP BY 1")
                return {row[0]: row[1] for row in cursor.fetchall()}

            status_breakdown = breakdown('status')
            type_breakdown = breakdown('source_type')
            rating_breakdown = breakdown('content_rating')
            publication_breakdown = breakdown('source_status')

            cursor.execute("""
                SELECT COALESCE(source_type, 'other'), status, COUNT(*)
                FROM series GROUP BY 1, 2
            """)
            type_by_status = {}
            for content_type, status, count in cursor.fetchall():
                type_by_status.setdefault(content_type, {})[status] = count

            # === SOURCES ===
            cursor.execute("""
                SELECT source_type, COUNT(DISTINCT series_id)
                FROM series_sources GROUP BY source_type
            """)
            series_per_source = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute("""
                SELECT COUNT(*) FROM (
                    SELECT series_id FROM series_sources
                    GROUP BY series_id HAVING COUNT(*) > 1
                )
            """)
            multi_source_count = cursor.fetchone()[0]

            # === READING ACTIVITY ===
            # Every row: one per day/month/year, so the table stays small
            # (cleanup_old_stats() is never called)
            cursor.execute("""
                SELECT period_type, period_start, chapters_read, series_added
                FROM stats_history
                WHERE period_type IN ('day', 'month', 'year')
            """)
            day_reads = {}
            month_rows = {}
            year_rows = {}
            day_added = {}
            month_added_rows = {}
            year_added_rows = {}
            for period_type, period_start, read, added in cursor.fetchall():
                read = max(read or 0, 0)
                added = max(added or 0, 0)
                if period_type == 'day':
                    day_reads[period_start] = read
                    day_added[period_start] = added
                elif period_type == 'month':
                    month_rows[period_start[:7]] = read
                    month_added_rows[period_start[:7]] = added
                else:
                    year_rows[period_start[:4]] = read
                    year_added_rows[period_start[:4]] = added

            # === NEW RELEASES (series on the reading list) ===
            cursor.execute("""
                SELECT DATE(c.release_date), COUNT(DISTINCT c.series_id || ':' || c.chapter_number)
                FROM chapters c
                JOIN series s ON s.id = c.series_id
                WHERE s.status = 'reading'
                  AND c.release_date IS NOT NULL
                  AND DATE(c.release_date) >= ?
                GROUP BY 1
            """, (history_start.isoformat(),))
            day_releases = {row[0]: row[1] for row in cursor.fetchall()}

            # Reading series that have gone quiet (no release in 90+ days)
            cursor.execute("""
                SELECT id, title, source_url, source_status, current_chapter, latest_chapter,
                       latest_release, CAST(julianday('now') - julianday(latest_release) AS INTEGER)
                FROM series
                WHERE status = 'reading'
                  AND latest_release IS NOT NULL
                  AND julianday('now') - julianday(latest_release) > 90
                ORDER BY julianday(latest_release)
            """)
            quiet_series = [
                {'id': r[0], 'title': r[1], 'url': r[2], 'publication': r[3] or 'unknown',
                 'current': r[4], 'latest': r[5], 'latest_release': r[6], 'days': r[7]}
                for r in cursor.fetchall()
            ]

            # === RANKED LISTS ===
            cursor.execute("""
                SELECT id, title, MAX(current_chapter, 0), latest_chapter,
                       latest_chapter - MAX(current_chapter, 0) AS unread
                FROM series
                WHERE status = 'reading' AND latest_chapter > current_chapter
                ORDER BY unread DESC
                LIMIT 8
            """)
            biggest_backlogs = [
                {'id': r[0], 'title': r[1], 'current': r[2], 'latest': r[3], 'unread': round(r[4], 1)}
                for r in cursor.fetchall()
            ]

            cursor.execute("""
                SELECT id, title, current_chapter, latest_chapter, status
                FROM series
                WHERE current_chapter > 0
                ORDER BY current_chapter DESC
                LIMIT 8
            """)
            most_read = [
                {'id': r[0], 'title': r[1], 'current': r[2], 'latest': r[3], 'status': r[4]}
                for r in cursor.fetchall()
            ]

            cursor.execute("SELECT genres FROM series WHERE genres IS NOT NULL AND genres != ''")
            genre_rows = [row[0] for row in cursor.fetchall()]
            tag_rules = load_tag_rules(cursor)
        finally:
            release_db(conn)

        # Counted with Fixes > Tag's merges/bans applied
        tag_counts = count_tags(genre_rows, tag_rules)
        top_genres = sorted(tag_counts, key=lambda t: (-t['count'], t['tag']))[:15]

        # A Kenmei import or a bulk progress edit logs every series going from
        # "not started" to its chapter, so that day's "chapters read" is in the
        # thousands. Those days are flagged and left out of the reading totals.
        bulk_days = {d for d, read in day_reads.items() if read > BULK_DAY_CHAPTERS}

        def is_read_day(d):
            return day_reads.get(d, 0) > 0 and d not in bulk_days

        def read_since(start):
            return sum(r for d, r in day_reads.items() if d >= start and d not in bulk_days)

        # Month totals come from the daily rows (without bulk days) when the
        # month has any, else from its stored row (months from before daily
        # rows were kept). A year is the sum of its months, else its stored row.
        month_from_days = {}
        for d, read in day_reads.items():
            month_from_days.setdefault(d[:7], 0)
            if d not in bulk_days:
                month_from_days[d[:7]] += read

        def month_read(month):
            if month in month_from_days:
                return month_from_days[month]
            return month_rows.get(month, 0)

        def year_read(year):
            months = {m for m in [*month_from_days, *month_rows] if m.startswith(year)}
            if months:
                return sum(month_read(m) for m in months)
            return year_rows.get(year, 0)

        # Same rollup as reads, for series added - no bulk-day concept here,
        # a big import day is a real spike, not noise to exclude
        month_added_from_days = {}
        for d, added in day_added.items():
            month_added_from_days.setdefault(d[:7], 0)
            month_added_from_days[d[:7]] += added

        def month_added(month):
            if month in month_added_from_days:
                return month_added_from_days[month]
            return month_added_rows.get(month, 0)

        def year_added(year):
            months = {m for m in [*month_added_from_days, *month_added_rows] if m.startswith(year)}
            if months:
                return sum(month_added(m) for m in months)
            return year_added_rows.get(year, 0)

        history_days = [(history_start + timedelta(days=i)).isoformat() for i in range(HISTORY_DAYS)]
        daily_reads = [
            {'date': d, 'chapters': round(day_reads.get(d, 0), 1), 'bulk': d in bulk_days}
            for d in history_days
        ]
        daily_releases = [{'date': d, 'chapters': day_releases.get(d, 0)} for d in history_days]
        real_reads = [d for d in daily_reads if not d['bulk']]

        # Heatmap + month chart, one set per calendar year (Monday-first weeks,
        # Jan 1 to Dec 31 - or today for the current year) so the History
        # section can switch years client-side without another request.
        # Shared by both the "chapters read" and "series added" views.
        def year_calendar(y, day_values, bulk_set):
            y_int = int(y)
            start = date(y_int, 1, 1) - timedelta(days=date(y_int, 1, 1).weekday())
            end = today if y_int == today.year else date(y_int, 12, 31)
            cal = []
            day = start
            while day <= end:
                d = day.isoformat()
                cal.append({'date': d, 'chapters': round(day_values.get(d, 0), 1), 'bulk': d in bulk_set})
                day += timedelta(days=1)
            return cal

        def year_months(y, month_value_fn):
            return [
                {'month': f'{y}-{m:02d}', 'chapters': round(month_value_fn(f'{y}-{m:02d}'), 1)}
                for m in range(1, 13)
            ]

        all_years = sorted({
            k[:4] for k in [*day_reads, *month_rows, *year_rows, *day_added, *month_added_rows, *year_added_rows]
        }, reverse=True)
        years = [
            {
                'year': y,
                'chapters': round(year_read(y), 1),
                'active_days': sum(1 for d in day_reads if d.startswith(y) and is_read_day(d)),
                'months': year_months(y, month_read),
                'calendar': year_calendar(y, day_reads, bulk_days),
                'series_added': round(year_added(y)),
                'added_active_days': sum(1 for d in day_added if d.startswith(y) and day_added.get(d, 0) > 0),
                'added_months': year_months(y, month_added),
                'added_calendar': year_calendar(y, day_added, set()),
            }
            for y in all_years
        ]

        # Streaks over every daily row. No reads yet today doesn't break the
        # current streak - the day isn't over. A bulk day neither counts nor
        # breaks one.
        current_streak = 0
        day = today if is_read_day(today.isoformat()) else today - timedelta(days=1)
        while is_read_day(day.isoformat()) or day.isoformat() in bulk_days:
            current_streak += is_read_day(day.isoformat())
            day -= timedelta(days=1)
        best_streak = run = 0
        if day_reads:
            day = date.fromisoformat(min(day_reads))
            while day <= today:
                d = day.isoformat()
                if is_read_day(d):
                    run += 1
                    best_streak = max(best_streak, run)
                elif d not in bulk_days:
                    run = 0
                day += timedelta(days=1)

        best_day = max(real_reads, key=lambda d: d['chapters'], default=None)
        read_30d = sum(d['chapters'] for d in real_reads)

        return jsonify({
            'library': {
                'total_series': total_series,
                'chapters_read': int(chapters_read),
                'chapters_available': int(chapters_available),
                'not_started': not_started or 0,
                'reading': reading_count or 0,
                'caught_up': caught_up or 0,
                'behind': behind or 0,
                'reading_backlog': int(reading_backlog or 0),
                'quiet_reading': len(quiet_series),
                'multi_source': multi_source_count,
            },
            'breakdowns': {
                'status': status_breakdown,
                'type': type_breakdown,
                'rating': rating_breakdown,
                'publication': publication_breakdown,
            },
            'type_by_status': type_by_status,
            'sources': series_per_source,
            'activity': {
                'today': 0 if today.isoformat() in bulk_days else round(day_reads.get(today.isoformat(), 0), 1),
                'week': round(read_since((today - timedelta(days=today.weekday())).isoformat()), 1),
                'month': round(month_read(today.strftime('%Y-%m')), 1),
                'year': round(year_read(str(today.year)), 1),
                'bulk_days': sum(1 for d in daily_reads if d['bulk']),
                'daily_reads': daily_reads,
                'daily_releases': daily_releases,
                'current_streak': current_streak,
                'best_streak': best_streak,
                'active_days': sum(1 for d in real_reads if d['chapters'] > 0),
                'avg_per_day': round(read_30d / max(len(real_reads), 1), 1),
                'best_day': best_day if best_day and best_day['chapters'] > 0 else None,
                'releases_30d': sum(d['chapters'] for d in daily_releases),
                'years': years,
            },
            'top_genres': top_genres,
            'biggest_backlogs': biggest_backlogs,
            'most_read': most_read,
            'quiet_series': quiet_series,
        })

    except Exception as e:
        print(f"[Stats API] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/')
def root():
    from flask import redirect
    return redirect('/dashboard')

@app.route('/dashboard')
def dashboard():
    update_last_dashboard_visit()
    return render_template('index.html')

def run_server():
    init_db() 
    manga_scheduler.start_scanning()
    # threaded=True so the dev server can serve many concurrent static cover
    # requests (kagane_covers, atsu_covers) at once instead of one-at-a-time -
    # without it, a page with hundreds of locally-cached covers loads them
    # serially through this single process instead of in parallel like a CDN.
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False, threaded=True)