from flask import Flask, request, jsonify, render_template
from datetime import datetime, timezone
import sqlite3
import json
import threading
import time
import uuid
from queue import Queue, Empty

from .database import (
    init_db,
    update_series,
    add_series,
    update_last_dashboard_visit,
    get_unread_reading_count,
    get_db,
    release_db
)
from .trackers.mangadex import extract_manga_id, get_manga_info_with_anilist, get_latest_chapters
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
                is_kagane = url.startswith("https://kagane.org/series/")

                if not (is_mangadex or is_kagane):
                    result = {'error': 'Only MangaDex or Kagane series URLs are supported'}
                    task_processed = True
                    continue

                # === EARLY DUPLICATE CHECK ===
                conn_check = get_db()
                cursor_check = conn_check.cursor()
                cursor_check.execute("SELECT id, title FROM series WHERE source_url = ?", (url,))
                existing = cursor_check.fetchone()
                release_db(conn_check)

                if existing:
                    series_id, existing_title = existing
                    error_msg = f"Duplicate add attempt: already tracking '{existing_title}'"
                    print(f"[Add Queue] {error_msg}")
                    from .error_logger import log_error
                    log_error(url, error_msg, series_title=existing_title)
                    result = {'id': series_id, 'success': True, 'duplicate': True}
                    task_processed = True
                    continue
                # === END DUPLICATE CHECK ===

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
                            task_processed = True
                        except sqlite3.IntegrityError as e:
                            if "source_url" in str(e):
                                result = {'error': 'This series is already in your tracker.'}
                            else:
                                result = {'error': 'Database integrity error.'}
                            task_processed = True
                        except Exception as e:
                            result = {'error': str(e)}
                            task_processed = True
                    else:
                        info = get_manga_info_with_anilist(manga_id)
                        if info:
                            title = info['title']
                            cover_url = info['cover_url']
                            mangadex_status = info['status']
                            alt_titles = info['alt_titles']
                        else:
                            title = "Unknown Manga"
                            cover_url = None
                            mangadex_status = None
                            alt_titles = None

                        info = get_manga_info_with_anilist(manga_id)
                        if info:
                            title = info['title']
                            cover_url = info['cover_url']
                            mangadex_status = info['status']
                            alt_titles = info['alt_titles']
                            # ✅ Extract AniList-enriched fields directly:
                            title_en = info.get('title_en')
                            title_romaji = info.get('title_romaji')
                            title_native = info.get('title_native')
                            banner_url = info.get('banner_url')  # ← this is from AniList!
                            anilist_id = None  # you don't store it, so leave as None

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
                                genres=info.get('genres', []),
                                content_rating=info.get('content_rating', 'unknown'),
                                source_type=info.get('source_type', 'other')
                            )

                            # Inject chapters
                            conn = get_db()
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
                            for ch in chapters_to_save:
                                cursor.execute("""
                                    INSERT INTO chapters (
                                        series_id, volume, raw_chapter, chapter_number,
                                        release_date, chapter_url, is_oneshot
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    series_id,
                                    ch.get('volume'),
                                    ch.get('raw_chapter', str(ch['chapter_number'])),
                                    ch['chapter_number'],
                                    ch['release_date'],
                                    ch['chapter_url'],
                                    int(ch.get('is_oneshot', False))
                                ))
                            if chapters_to_save:
                                latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                latest_release = max(
                                    (ch['release_date'] for ch in chapters_to_save if ch['release_date']),
                                    default=''
                                )
                                cursor.execute("""
                                    UPDATE series
                                    SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                    WHERE id = ?
                                """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                            release_db(conn)
                            result = {'id': series_id, 'success': True}
                            task_processed = True
                        except sqlite3.IntegrityError as e:
                            if "source_url" in str(e):
                                result = {'error': 'This series is already in your tracker.'}
                            else:
                                result = {'error': 'Database integrity error.'}
                            task_processed = True
                        except Exception as e:
                            result = {'error': str(e)}
                            task_processed = True
                #
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
                                            release_date, chapter_url, is_oneshot
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        series_id,
                                        None,
                                        str(ch['chapter_number']),
                                        ch['chapter_number'],
                                        ch['release_date'],
                                        ch['chapter_url'],
                                        0
                                    ))
                                if chapters_to_save:
                                    latest_ch = max(ch['chapter_number'] for ch in chapters_to_save)
                                    latest_release = max(
                                        (ch['release_date'] for ch in chapters_to_save if ch['release_date']),
                                        default=''
                                    )
                                    cursor.execute("""
                                        UPDATE series
                                        SET latest_chapter = ?, latest_release = ?, total_chapters = ?
                                        WHERE id = ?
                                    """, (latest_ch, latest_release, len(chapters_to_save), series_id))
                                release_db(conn)
                                result = {'id': series_id, 'success': True}
                                task_processed = True
                            except sqlite3.IntegrityError as e:
                                if "source_url" in str(e):
                                    result = {'error': 'This series is already in your tracker.'}
                                else:
                                    result = {'error': 'Database integrity error.'}
                                task_processed = True
                            except Exception as e:
                                result = {'error': str(e)}
                                task_processed = True

            except Exception as e:
                error_msg = str(e)
                print(f"[Add Queue] Task crashed: {error_msg}")
                result = {'error': error_msg}
                try:
                    from .error_logger import log_error
                    title_guess = data.get('title') or "Unknown"
                    log_error(url, error_msg, series_title=title_guess)
                except:
                    pass  # never crash the logger

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
            print(f"[Add Worker] Recovered from outer crash: {e}")
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

    valid_sorts = ['unread_first', 'title', 'latest_release', 'last_added', 'total_chapters']
    if sort_order not in valid_sorts:
        sort_order = 'unread_first'

    if sort_order in ('latest_release', 'last_added'):
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

    # Genre filter (multi-select - AND logic)
    genre_filter = request.args.get('genre', '').strip()
    if genre_filter:
        genre_list = [g.strip() for g in genre_filter.split(',') if g.strip()]
        if genre_list:
            # For AND: each condition must be true
            for g in genre_list:
                where_parts.append("genres LIKE ?")
                params.append(f'%"{g}"%')

    # Content Rating filter (multi-select)
    rating_filter = request.args.get('rating', '').strip()
    if rating_filter:
        rating_list = [r.strip() for r in rating_filter.split(',') if r.strip()]
        if rating_list:
            placeholders = ','.join(['?'] * len(rating_list))
            where_parts.append(f"content_rating IN ({placeholders})")
            params.extend(rating_list)

    # Publication Status filter (multi-select)
    pub_status_filter = request.args.get('pub_status', '').strip()
    if pub_status_filter:
        pub_status_list = [p.strip() for p in pub_status_filter.split(',') if p.strip()]
        if pub_status_list:
            placeholders = ','.join(['?'] * len(pub_status_list))
            where_parts.append(f"source_status IN ({placeholders})")
            params.extend(pub_status_list)

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

    if sort_order == 'unread_first':
        inverted_dir = 'asc' if effective_dir == 'desc' else 'desc'
        order_by = f"""
        ORDER BY
          (COALESCE(latest_chapter, -1) > current_chapter) DESC,
          CASE WHEN (COALESCE(latest_chapter, -1) > current_chapter) THEN latest_release END {inverted_dir.upper()},
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
    release_db(conn)

    total_pages = (total + per_page - 1) // per_page
    return jsonify({
        'items': [dict(row) for row in rows],
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

@app.route('/api/unread-reading-count')
def api_unread_count():
    count = get_unread_reading_count()
    return jsonify({'count': count})

@app.route('/api/series/<int:series_id>', methods=['PATCH'])
def api_update_series(series_id):
    data = request.get_json()
    allowed_fields = {'current_chapter', 'current_volume', 'status', 'cover_url', 'source_url', 'title'}
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
    update_series(series_id, updates)
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
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM series WHERE id = ?", (series_id,))
        release_db(conn)
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
            'is_oneshot': bool(row[4])
        }
        result.append(r)
    return jsonify(result)

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
    app.run(host='127.0.0.1', port=8080, debug=True, use_reloader=False)