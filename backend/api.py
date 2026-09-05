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


from .database import (
    init_db,
    update_series,
    add_series,
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
                url = data.get('source_url')
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

                if not (is_mangadex or is_kagane or is_atsu or is_asura or is_hive):
                    result = {'error': 'Only MangaDex, Kagane, Atsumaru, AsuraScans, or HiveToons series URLs are supported'}
                    task_processed = True
                    continue

                # === NO EARLY DUPLICATE CHECK - Let database handle it atomically ===
                # Duplicates will be caught by IntegrityError and logged there

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
                                cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ?", (url,))
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
                                    
                                    result = {'id': series_id, 'success': True, 'duplicate': True}
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
                                latest_release = max(
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
                                from .database import save_mangadex_covers
                                save_mangadex_covers(series_id, covers)
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
                        except sqlite3.IntegrityError as e:
                            error_str = str(e).lower()
                            if "source_url" in error_str or "unique" in error_str:
                                # Race condition: fetch existing series
                                conn_dup = get_db()
                                cursor_dup = conn_dup.cursor()
                                cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ?", (url,))
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
                                    
                                    result = {'id': series_id, 'success': True, 'duplicate': True}
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
                        kagane_info = get_series_info(kagane_id)
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
                                    latest_release = max(
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
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ?", (url,))
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
                                        
                                        result = {'id': series_id, 'success': True, 'duplicate': True}
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
                                    latest_release = max(
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
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ?", (url,))
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

                                        result = {'id': series_id, 'success': True, 'duplicate': True}
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
                                    latest_release = max(
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
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ?", (url,))
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

                                        result = {'id': series_id, 'success': True, 'duplicate': True}
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
                                    latest_release = max(
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
                            except sqlite3.IntegrityError as e:
                                error_str = str(e).lower()
                                if "source_url" in error_str or "unique" in error_str:
                                    # Race condition: fetch existing series
                                    conn_dup = get_db()
                                    cursor_dup = conn_dup.cursor()
                                    cursor_dup.execute("SELECT id, title FROM series WHERE source_url = ?", (url,))
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

                                        result = {'id': series_id, 'success': True, 'duplicate': True}
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
    from .database import get_db, release_db, normalize_for_search
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
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

    # Status filter
    if status_filter != 'all':
        where_parts.append("status = ?")
        params.append(status_filter)

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
        
        # Include genres - series must have ALL of these
        for g in include_genres:
            where_parts.append("genres LIKE ?")
            params.append(f'%"{g}"%')
        
        # Exclude genres - series must NOT have ANY of these
        for g in exclude_genres:
            where_parts.append("genres NOT LIKE ?")
            params.append(f'%"{g}"%')
    
    # Process ratings
    if rating_list and len(rating_list) == len(rating_modes):
        include_ratings = [rating_list[i] for i in range(len(rating_list)) if rating_modes[i] == 'include']
        exclude_ratings = [rating_list[i] for i in range(len(rating_list)) if rating_modes[i] == 'exclude']
        
        # Include ratings - series can have ANY of these (OR logic)
        if include_ratings:
            placeholders = ','.join(['?'] * len(include_ratings))
            where_parts.append(f"content_rating IN ({placeholders})")
            params.extend(include_ratings)
        
        # Exclude ratings - series must NOT have ANY of these
        if exclude_ratings:
            placeholders = ','.join(['?'] * len(exclude_ratings))
            where_parts.append(f"content_rating NOT IN ({placeholders})")
            params.extend(exclude_ratings)

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
    if search_query:
        query_words = search_query.split()
        normalized_words = []
        for word in query_words:
            norm_word = normalize_for_search(word)
            if norm_word:
                normalized_words.append(norm_word)
        for word in normalized_words:
            where_parts.append("searchable_text LIKE ?")
            params.append(f"%{word}%")
    
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
    return jsonify({
        'items': items,
        'total_pages': total_pages,
        'current_page': page
    })

@app.route('/api/genres')
def api_genres():
    try:
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
        rows = cursor.fetchall()
        release_db(conn)

        genre_set = set()
        for (genre_str,) in rows:
            try:
                parsed = json.loads(genre_str)
                if isinstance(parsed, list):
                    for g in parsed:
                        if isinstance(g, str) and g.strip():
                            genre_set.add(g.strip())
            except (json.JSONDecodeError, TypeError):
                continue

        return jsonify(sorted(genre_set))
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
    allowed_fields = {'current_chapter', 'current_volume', 'status', 'cover_url', 'title'}
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    
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
        cursor_old.execute("SELECT title, current_chapter, status FROM series WHERE id = ?", (series_id,))
        old_row = cursor_old.fetchone()
        release_db(conn_old)
        
        if old_row:
            old_title, old_chapter, old_status = old_row
            
            # Determine action type and log values
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
            elif 'status' in updates and old_status != updates['status']:
                log_activity(
                    action_type='status',
                    series_id=series_id,
                    series_title=old_title,
                    old_value={'status': old_status},
                    new_value={'status': updates['status']},
                    is_bulk=_is_bulk,
                    bulk_id=_bulk_id
                )
            elif 'title' in updates or 'cover_url' in updates:
                old_vals = {}
                new_vals = {}
                if 'title' in updates:
                    old_vals['title'] = old_title
                    new_vals['title'] = updates['title']
                if 'cover_url' in updates:
                    cursor_old = conn_old.cursor()
                    cursor_old.execute("SELECT cover_url FROM series WHERE id = ?", (series_id,))
                    old_cover = cursor_old.fetchone()
                    if old_cover:
                        old_vals['cover_url'] = old_cover[0]
                        new_vals['cover_url'] = updates['cover_url']
                
                log_activity(
                    action_type='edited',
                    series_id=series_id,
                    series_title=old_title,
                    old_value=old_vals,
                    new_value=new_vals,
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


@app.route('/api/series/<int:series_id>/mangadex-covers')
def api_get_mangadex_covers(series_id):
    """The full MangaDex cover gallery (every volume/locale variant) fetched
    when a MangaDex source was added, for the Series Settings cover picker."""
    from .database import get_mangadex_covers
    return jsonify({'covers': get_mangadex_covers(series_id)})


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
    snapshot = get_series_snapshot(series_id)
    
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
            'source_type': row[5]
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
                
                cursor.execute("""
                    SELECT old_value, new_value
                    FROM activity_log
                    WHERE action_type = 'progress'
                    AND timestamp >= ? AND timestamp <= ?
                """, (last_year_start.isoformat(), last_year_end.isoformat()))
                
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
    Get comprehensive statistics about the tracker.
    """
    try:
        # Save completed period stats before calculating current stats
        try:
            save_completed_period_stats()
        except Exception as save_err:
            print(f"[Stats] Failed to save period stats (continuing anyway): {save_err}")
            import traceback
            traceback.print_exc()
        
        from datetime import datetime, timezone, timedelta
        from .database import get_db, release_db
        
        conn = get_db()
        cursor = conn.cursor()
        
        # === CORE STATS ===
        
        # Total series
        cursor.execute("SELECT COUNT(*) FROM series")
        total_series = cursor.fetchone()[0]
        
        # Total series with last chapter read (caught up)
        cursor.execute("""
            SELECT COUNT(*) FROM series 
            WHERE current_chapter >= COALESCE(latest_chapter, 0) 
            AND current_chapter != -1
        """)
        caught_up = cursor.fetchone()[0]
        
        # Total series started but not finished
        cursor.execute("""
            SELECT COUNT(*) FROM series 
            WHERE current_chapter != -1 
            AND current_chapter < COALESCE(latest_chapter, 0)
        """)
        started_not_finished = cursor.fetchone()[0]
        
        # Total chapters (sum of all latest_chapter across all series)
        cursor.execute("SELECT COALESCE(SUM(latest_chapter), 0) FROM series WHERE latest_chapter IS NOT NULL")
        total_chapters = int(cursor.fetchone()[0])
        
        # Total chapters read (sum of current_chapter where not -1)
        cursor.execute("""
            SELECT COALESCE(SUM(current_chapter), 0) FROM series 
            WHERE current_chapter != -1
        """)
        total_chapters_read = int(cursor.fetchone()[0])
        
        # === CONTENT TYPE BREAKDOWN ===
        cursor.execute("""
            SELECT 
                source_type,
                COUNT(*) as count
            FROM series
            GROUP BY source_type
        """)
        content_type_breakdown = {row[0]: row[1] for row in cursor.fetchall()}
        
        # === READING STATUS BREAKDOWN ===
        cursor.execute("""
            SELECT 
                status,
                COUNT(*) as count
            FROM series
            GROUP BY status
        """)
        status_breakdown = {row[0]: row[1] for row in cursor.fetchall()}
        
        # === ADDITIONAL STATS ===
        
        # Average chapters per series (for started series)
        cursor.execute("SELECT COUNT(*) FROM series WHERE current_chapter != -1")
        series_started_count = cursor.fetchone()[0]
        avg_chapters_per_series = round(total_chapters_read / series_started_count, 1) if series_started_count > 0 else 0
        
        # Completion rate
        cursor.execute("SELECT COUNT(*) FROM series WHERE status = 'completed'")
        completed_count = cursor.fetchone()[0]
        completion_rate = round((completed_count / total_series * 100), 1) if total_series > 0 else 0
        
        # Most read content type
        most_read_type = max(content_type_breakdown.items(), key=lambda x: x[1])[0] if content_type_breakdown else 'N/A'
        
        # CHANGED: Read from stats_history instead of recalculating
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_str = today_start.date().isoformat()
        
        cursor.execute("""
            SELECT series_added, chapters_read 
            FROM stats_history 
            WHERE period_type = 'day' AND period_start = ?
        """, (today_str,))
        today_stats = cursor.fetchone()
        
        if today_stats:
            series_added_today = today_stats[0]
            chapters_read_today = today_stats[1]
        else:
            # Fallback: calculate if not in stats_history yet (use DATE comparison)
            cursor.execute("""
                SELECT COUNT(*) FROM series 
                WHERE DATE(created_at) = DATE(?)
            """, (now.isoformat(),))
            series_added_today = cursor.fetchone()[0] or 0
            chapters_read_today = 0
        
        # Series added this week/month/year (CALENDAR PERIODS)
        now = datetime.now(timezone.utc)
        
        # CHANGED: Read from stats_history instead of recalculating
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        
        week_str = week_start.date().isoformat()
        month_str = month_start.date().isoformat()
        year_str = year_start.date().isoformat()
        
        # Get week stats
        cursor.execute("""
            SELECT series_added, chapters_read 
            FROM stats_history 
            WHERE period_type = 'week' AND period_start = ?
        """, (week_str,))
        week_stats = cursor.fetchone()
        series_added_week = week_stats[0] if week_stats else 0
        chapters_read_week = week_stats[1] if week_stats else 0
        
        # Get month stats
        cursor.execute("""
            SELECT series_added, chapters_read 
            FROM stats_history 
            WHERE period_type = 'month' AND period_start = ?
        """, (month_str,))
        month_stats = cursor.fetchone()
        series_added_month = month_stats[0] if month_stats else 0
        chapters_read_month = month_stats[1] if month_stats else 0
        
        # Get year stats
        cursor.execute("""
            SELECT series_added, chapters_read 
            FROM stats_history 
            WHERE period_type = 'year' AND period_start = ?
        """, (year_str,))
        year_stats = cursor.fetchone()
        series_added_year = year_stats[0] if year_stats else 0
        chapters_read_year = year_stats[1] if year_stats else 0
        
        # Series per source
        cursor.execute("""
            SELECT 
                source_type,
                COUNT(DISTINCT series_id) as count
            FROM series_sources
            GROUP BY source_type
        """)
        series_per_source = {row[0]: row[1] for row in cursor.fetchall()}
        
        # Most used source
        most_used_source = max(series_per_source.items(), key=lambda x: x[1])[0] if series_per_source else 'N/A'
        
        # Series with multiple sources
        cursor.execute("""
            SELECT COUNT(*) FROM (
                SELECT series_id FROM series_sources
                GROUP BY series_id
                HAVING COUNT(*) > 1
            )
        """)
        multi_source_count = cursor.fetchone()[0]
        
        # Average unread chapters (across reading series)
        cursor.execute("""
            SELECT AVG(COALESCE(latest_chapter, 0) - current_chapter)
            FROM series
            WHERE status = 'reading' 
            AND current_chapter != -1
            AND latest_chapter > current_chapter
        """)
        avg_unread = cursor.fetchone()[0]
        avg_unread_chapters = round(avg_unread, 1) if avg_unread else 0
        
        # Total unread chapters
        cursor.execute("""
            SELECT SUM(COALESCE(latest_chapter, 0) - current_chapter)
            FROM series
            WHERE current_chapter != -1
            AND latest_chapter > current_chapter
        """)
        total_unread = cursor.fetchone()[0]
        total_unread_chapters = int(total_unread) if total_unread else 0
        
        # Content rating breakdown
        cursor.execute("""
            SELECT 
                content_rating,
                COUNT(*) as count
            FROM series
            GROUP BY content_rating
        """)
        rating_breakdown = {row[0]: row[1] for row in cursor.fetchall()}
        
        # Most common genre
        cursor.execute("SELECT genres FROM series WHERE genres IS NOT NULL AND genres != ''")
        all_genres = []
        for row in cursor.fetchall():
            try:
                genre_list = json.loads(row[0])
                if isinstance(genre_list, list):
                    all_genres.extend(genre_list)
            except:
                pass
        
        if all_genres:
            genre_counts = {}
            for genre in all_genres:
                genre_counts[genre] = genre_counts.get(genre, 0) + 1
            most_common_genre = max(genre_counts.items(), key=lambda x: x[1])[0]
        else:
            most_common_genre = 'N/A'
        
        release_db(conn)
        
        # === RETURN STATS ===
        return jsonify({
            'core': {
                'total_series': total_series,
                'caught_up': caught_up,
                'started_not_finished': started_not_finished,
                'total_chapters': total_chapters,
                'total_chapters_read': total_chapters_read
            },
            'content_type': content_type_breakdown,
            'status': status_breakdown,
            'additional': {
                'avg_chapters_per_series': avg_chapters_per_series,
                'completion_rate': completion_rate,
                'most_read_type': most_read_type,
                'series_added_today': series_added_today,
                'chapters_read_today': chapters_read_today,
                'series_added_week': series_added_week,
                'series_added_month': series_added_month,
                'series_added_year': series_added_year,
                'chapters_read_week': chapters_read_week,
                'chapters_read_month': chapters_read_month,
                'chapters_read_year': chapters_read_year,
                'series_per_source': series_per_source,
                'most_used_source': most_used_source,
                'multi_source_count': multi_source_count,
                'avg_unread_chapters': avg_unread_chapters,
                'total_unread_chapters': total_unread_chapters,
                'most_common_genre': most_common_genre
            },
            'rating_breakdown': rating_breakdown
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
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)