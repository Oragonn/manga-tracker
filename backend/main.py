# backend/main.py
from .api import app, run_server
from flask import Flask, request, jsonify, render_template, send_file
import re
from .activity_logger import get_logs, mark_log_undone
from .database import add_series as db_add_series
import os
import json

@app.route('/errors')
def errors_page():
    from .error_logger import (
        get_available_log_dates,
        set_last_errors_visit,
        get_errors_for_date,
        get_last_errors_visit
    )
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        PARIS_TZ = ZoneInfo("Europe/Paris")
    except ImportError:
        PARIS_TZ = None

    # Step 1: Get last visit BEFORE marking as read
    last_visit_str = get_last_errors_visit()

    # Safe parser for ISO timestamps
    def parse_iso_utc(s):
        s = s.strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        if '.' in s:
            date_part, subsec_tz = s.split('.', 1)
            subsec = subsec_tz.split('+', 1)[0].split('Z')[0]
            tz_part = '+' + subsec_tz.split('+', 1)[1] if '+' in subsec_tz else '+00:00'
            subsec = subsec[:6]
            s = f"{date_part}.{subsec}{tz_part}"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    try:
        last_visit_dt = parse_iso_utc(last_visit_str)
    except:
        last_visit_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)

    # Load errors
    now_paris = datetime.now(PARIS_TZ) if PARIS_TZ else datetime.now()
    today = now_paris.strftime("%Y-%m-%d")
    errors = get_errors_for_date(today)
    log_dates = get_available_log_dates()

    # Annotate errors with is_new and French display time
    for err in errors:
        try:
            err_dt_utc = parse_iso_utc(err['timestamp'])
            # Convert to Paris time for display
            if PARIS_TZ:
                err_paris = err_dt_utc.astimezone(PARIS_TZ)
            else:
                err_paris = err_dt_utc.astimezone()
            err['display_time'] = err_paris.strftime("%d/%m/%Y %H:%M:%S")
            err['is_new'] = err_dt_utc > last_visit_dt
        except Exception:
            err['display_time'] = err['timestamp']
            err['is_new'] = False

    # Step 2: NOW mark as read
    set_last_errors_visit()

    return render_template(
        'errors.html',
        errors=errors,
        log_dates=log_dates,
        current_date=today
    )

# In backend/api.py
@app.route('/api/errors/<date_str>')
def api_errors_by_date(date_str):
    import re
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'error': 'Invalid date'}), 400
    try:
        from datetime import datetime
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    from .error_logger import get_errors_for_date
    errors = get_errors_for_date(date_str)
    
    # Format display_time in French (match main.py logic)
    try:
        from zoneinfo import ZoneInfo
        PARIS_TZ = ZoneInfo("Europe/Paris")
    except ImportError:
        PARIS_TZ = None

    from datetime import datetime, timezone
    def parse_iso_utc(s):
        s = s.strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        if '.' in s:
            date_part, subsec_tz = s.split('.', 1)
            subsec = subsec_tz.split('+', 1)[0].split('Z')[0]
            tz_part = '+' + subsec_tz.split('+', 1)[1] if '+' in subsec_tz else '+00:00'
            subsec = subsec[:6]
            s = f"{date_part}.{subsec}{tz_part}"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    for err in errors:
        try:
            err_dt_utc = parse_iso_utc(err['timestamp'])
            if PARIS_TZ:
                err_paris = err_dt_utc.astimezone(PARIS_TZ)
            else:
                err_paris = err_dt_utc.astimezone()
            err['display_time'] = err_paris.strftime("%d/%m/%Y %H:%M:%S")
        except:
            err['display_time'] = err['timestamp']
        err['is_new'] = False  # logs are historical — never "new"

    return jsonify({'errors': errors})

@app.route('/logs')
def logs_page():
    """Render activity logs page."""
    return render_template('logs.html')

@app.route('/api/logs')
def api_get_logs():
    """
    Get activity logs with filters.
    Query params:
      - type: all|added|deleted|progress|status|edited
      - time: all|today|week|month
      - search: series title search
    """
    type_filter = request.args.get('type', 'all')
    time_filter = request.args.get('time', 'all')
    search_query = request.args.get('search', '')
    
    logs = get_logs(type_filter, time_filter, search_query, limit=100)
    return jsonify(logs)

@app.route('/api/logs/undo/<int:log_id>', methods=['POST'])
def api_undo_log(log_id):
    """Undo a single log entry."""
    try:
        from datetime import datetime, timezone, timedelta
        from .database import get_db, release_db, update_series
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get log entry
        cursor.execute("""
            SELECT action_type, series_id, series_title, old_value, new_value, 
                   timestamp, can_undo
            FROM activity_log WHERE id = ?
        """, (log_id,))
        log = cursor.fetchone()
        
        if not log:
            release_db(conn)
            return jsonify({'error': 'Log not found'}), 404
        
        action_type, series_id, series_title, old_value_str, new_value_str, timestamp_str, can_undo = log
        
        # Check if can undo
        if not can_undo:
            release_db(conn)
            return jsonify({'error': 'Already undone'}), 400
        
        # Check 7-day window
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            if (now - timestamp).days > 7:
                release_db(conn)
                return jsonify({'error': 'Undo window expired (7 days)'}), 400
        except:
            pass
        
        release_db(conn)
        
        # Parse values
        import json
        old_value = json.loads(old_value_str) if old_value_str else None
        new_value = json.loads(new_value_str) if new_value_str else None
        
        # Track if we need to check now
        need_check_now = False
        restored_series_id = None
        
        # Perform undo based on action type
        if action_type == 'deleted':
            # Re-add the series
            if old_value and old_value.get('sources'):
                source = old_value['sources'][0]  # Primary source
                
                # *** FIX: Check if series already exists before trying to restore ***
                conn_check = get_db()
                cursor_check = conn_check.cursor()
                cursor_check.execute("SELECT id FROM series WHERE source_url = ?", (source['url'],))
                existing = cursor_check.fetchone()
                release_db(conn_check)
                
                if existing:
                    # Series already exists, skip restoration but mark for check-now
                    print(f"[Undo] Skipping '{old_value.get('title')}' - already exists")
                    restored_series_id = existing[0]
                    need_check_now = True
                else:
                    # Series doesn't exist, safe to restore
                    try:
                        series_id = db_add_series(
                            title=old_value.get('title', 'Restored Series'),
                            source_url=source['url'],
                            status=old_value.get('status', 'reading'),
                            cover_url=old_value.get('cover_url'),
                            banner_url=old_value.get('banner_url'),
                            anilist_id=old_value.get('anilist_id'),
                            title_en=old_value.get('title_en'),
                            title_romaji=old_value.get('title_romaji'),
                            title_native=old_value.get('title_native'),
                            source_status=old_value.get('source_status'),
                            alt_titles=old_value.get('alt_titles'),
                            genres=old_value.get('genres'),
                            content_rating=old_value.get('content_rating', 'unknown'),
                            source_type=old_value.get('source_type', 'other')
                        )
                        
                        # Restore chapter progress if available
                        if 'current_chapter' in source and source['current_chapter'] is not None:
                            update_series(series_id, {'current_chapter': source['current_chapter']})
                        
                        need_check_now = True
                        restored_series_id = series_id
                    except Exception as e:
                        print(f"[Undo] Failed to restore series: {e}")
                        return jsonify({'error': f'Failed to restore series: {str(e)}'}), 500
        
        elif action_type == 'progress' and series_id:
            # Revert chapter progress
            if old_value and 'chapter' in old_value:
                try:
                    update_series(series_id, {'current_chapter': old_value['chapter']})
                except Exception as e:
                    print(f"[Undo] Failed to revert progress: {e}")
                    return jsonify({'error': f'Failed to revert progress: {str(e)}'}), 500
        
        elif action_type == 'status' and series_id:
            # Revert status
            if old_value and 'status' in old_value:
                try:
                    update_series(series_id, {'status': old_value['status']})
                except Exception as e:
                    print(f"[Undo] Failed to revert status: {e}")
                    return jsonify({'error': f'Failed to revert status: {str(e)}'}), 500
        
        elif action_type == 'edited' and series_id:
            # Revert edits
            if old_value:
                try:
                    updates = {}
                    if 'title' in old_value:
                        updates['title'] = old_value['title']
                    if 'cover_url' in old_value:
                        updates['cover_url'] = old_value['cover_url']
                    if updates:
                        update_series(series_id, updates)
                except Exception as e:
                    print(f"[Undo] Failed to revert edits: {e}")
                    return jsonify({'error': f'Failed to revert edits: {str(e)}'}), 500
        
        # Mark as undone
        mark_log_undone(log_id=log_id)
        
        # Trigger check-now for restored series
        if need_check_now and restored_series_id:
            try:
                from .scheduler import MangaScheduler
                from . import api
                if hasattr(api, 'manga_scheduler'):
                    api.manga_scheduler.scan_series(restored_series_id)
                    print(f"[Undo] Triggered check-now for restored series {restored_series_id}")
            except Exception as e:
                print(f"[Undo] Failed to trigger check-now: {e}")
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"[Undo] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/logs/undo-bulk/<bulk_id>', methods=['POST'])
def api_undo_bulk(bulk_id):
    """Undo all entries in a bulk operation."""
    try:
        from datetime import datetime, timezone
        from .database import get_db, release_db, update_series
        import json
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all logs in this bulk operation
        cursor.execute("""
            SELECT id, action_type, series_id, series_title, old_value, new_value, 
                   timestamp, can_undo
            FROM activity_log 
            WHERE bulk_id = ?
            ORDER BY id
        """, (bulk_id,))
        logs = cursor.fetchall()
        
        if not logs:
            release_db(conn)
            return jsonify({'error': 'Bulk operation not found'}), 404
        
        # Check if any can undo
        first_log = logs[0]
        can_undo = first_log[7]
        timestamp_str = first_log[6]
        
        if not can_undo:
            release_db(conn)
            return jsonify({'error': 'Already undone'}), 400
        
        # Check 7-day window
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            if (now - timestamp).days > 7:
                release_db(conn)
                return jsonify({'error': 'Undo window expired (7 days)'}), 400
        except:
            pass
        
        release_db(conn)
        
        # Track restored series for check-now
        restored_series_ids = []
        
        # Undo each entry
        for log in logs:
            log_id, action_type, series_id, series_title, old_value_str, new_value_str = log[:6]
            
            old_value = json.loads(old_value_str) if old_value_str else None
            new_value = json.loads(new_value_str) if new_value_str else None
            
            if action_type == 'deleted':
                if old_value and old_value.get('sources'):
                    source = old_value['sources'][0]
                    
                    # *** FIX: Check if series already exists before trying to restore ***
                    conn_check = get_db()
                    cursor_check = conn_check.cursor()
                    cursor_check.execute("SELECT id FROM series WHERE source_url = ?", (source['url'],))
                    existing = cursor_check.fetchone()
                    release_db(conn_check)
                    
                    if existing:
                        # Series already exists, skip restoration but track for check-now
                        print(f"[Undo Bulk] Skipping '{old_value.get('title')}' - already exists")
                        restored_series_ids.append(existing[0])
                        continue
                    
                    # Series doesn't exist, safe to restore
                    try:
                        new_series_id = db_add_series(
                            title=old_value.get('title', 'Restored Series'),
                            source_url=source['url'],
                            status=old_value.get('status', 'reading'),
                            cover_url=old_value.get('cover_url'),
                            banner_url=old_value.get('banner_url'),
                            anilist_id=old_value.get('anilist_id'),
                            title_en=old_value.get('title_en'),
                            title_romaji=old_value.get('title_romaji'),
                            title_native=old_value.get('title_native'),
                            source_status=old_value.get('source_status'),
                            alt_titles=old_value.get('alt_titles'),
                            genres=old_value.get('genres'),
                            content_rating=old_value.get('content_rating', 'unknown'),
                            source_type=old_value.get('source_type', 'other')
                        )
                        
                        if 'current_chapter' in source and source['current_chapter'] is not None:
                            update_series(new_series_id, {'current_chapter': source['current_chapter']})
                        
                        restored_series_ids.append(new_series_id)
                    except Exception as e:
                        print(f"[Undo Bulk] Failed to restore '{old_value.get('title')}': {e}")
                        continue
            
            elif action_type == 'progress' and series_id:
                if old_value and 'chapter' in old_value:
                    try:
                        update_series(series_id, {'current_chapter': old_value['chapter']})
                    except Exception as e:
                        print(f"[Undo Bulk] Failed to revert progress for series {series_id}: {e}")
            
            elif action_type == 'status' and series_id:
                if old_value and 'status' in old_value:
                    try:
                        update_series(series_id, {'status': old_value['status']})
                    except Exception as e:
                        print(f"[Undo Bulk] Failed to revert status for series {series_id}: {e}")
            
            elif action_type == 'edited' and series_id:
                if old_value:
                    try:
                        updates = {}
                        if 'title' in old_value:
                            updates['title'] = old_value['title']
                        if 'cover_url' in old_value:
                            updates['cover_url'] = old_value['cover_url']
                        if updates:
                            update_series(series_id, updates)
                    except Exception as e:
                        print(f"[Undo Bulk] Failed to revert edits for series {series_id}: {e}")
        
        # Mark entire bulk as undone
        mark_log_undone(bulk_id=bulk_id)
        
        # Trigger check-now for all restored series
        if restored_series_ids:
            try:
                from .scheduler import MangaScheduler
                from . import api
                if hasattr(api, 'manga_scheduler'):
                    for sid in restored_series_ids:
                        api.manga_scheduler.scan_series(sid)
                    print(f"[Undo Bulk] Triggered check-now for {len(restored_series_ids)} restored series")
            except Exception as e:
                print(f"[Undo Bulk] Failed to trigger check-now: {e}")
        
        return jsonify({'success': True, 'restored_count': len(restored_series_ids)})
    
    except Exception as e:
        print(f"[Undo Bulk] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# List all backups
@app.route('/api/backups')
def api_list_backups():
    try:
        from . import api
        if hasattr(api, 'manga_scheduler'):
            stats = api.manga_scheduler.backup_manager.get_backup_stats()
            return jsonify(stats)
        return jsonify({'error': 'Backup manager not available'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Create manual backup
@app.route('/api/backups/create', methods=['POST'])
def api_create_backup():
    try:
        from . import api
        if hasattr(api, 'manga_scheduler'):
            success = api.manga_scheduler.backup_manager.create_backup()
            if success:
                return jsonify({'success': True})
            return jsonify({'error': 'Backup failed'}), 500
        return jsonify({'error': 'Backup manager not available'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Download backup
@app.route('/api/backups/download/<filename>')
def api_download_backup(filename):
    try:
        from . import api
        if hasattr(api, 'manga_scheduler'):
            backup_path = os.path.join(
                api.manga_scheduler.backup_manager.backup_dir, 
                filename
            )
            if os.path.exists(backup_path):
                return send_file(backup_path, as_attachment=True)
            return jsonify({'error': 'Backup not found'}), 404
        return jsonify({'error': 'Backup manager not available'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Restore from backup (DANGEROUS - use with caution)
@app.route('/api/backups/restore/<filename>', methods=['POST'])
def api_restore_backup(filename):
    try:
        from . import api
        if hasattr(api, 'manga_scheduler'):
            success = api.manga_scheduler.backup_manager.restore_backup(filename)
            if success:
                return jsonify({
                    'success': True, 
                    'message': 'Restored. Please restart the app.'
                })
            return jsonify({'error': 'Restore failed'}), 500
        return jsonify({'error': 'Backup manager not available'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/backups')
def backups_page():
    """Render backups management page."""
    return render_template('backups.html')

@app.route('/api/series/<int:series_id>/sources')
def api_get_sources(series_id):
    """Get all sources for a series."""
    try:
        from .database import get_series_sources
        sources = get_series_sources(series_id)
        return jsonify({'sources': sources})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<int:series_id>/sources', methods=['POST'])
def api_add_source(series_id):
    """
    Add a new source to a series AND merge metadata (alt_titles, genres, searchable_text).
    Fix for Bug #3: Properly merges lists instead of concatenating JSON strings.
    """
    try:
        data = request.get_json()
        source_url = data.get('source_url')
        
        if not source_url:
            return jsonify({'error': 'source_url required'}), 400
        
        # Detect source type
        if 'mangadex.org' in source_url:
            source_type = 'mangadex'
        elif 'kagane.org' in source_url or 'kagane.to' in source_url:
            source_type = 'kagane'
        else:
            source_type = 'unknown'
        
        # *** FIX: Fetch metadata from new source ***
        new_metadata = None
        
        if source_type == 'mangadex':
            from .trackers.mangadex import extract_manga_id, get_manga_info_with_anilist
            manga_id = extract_manga_id(source_url)
            if manga_id:
                new_metadata = get_manga_info_with_anilist(manga_id)
        elif source_type == 'kagane':
            from .trackers.kagane import extract_series_id, get_series_info
            kagane_id = extract_series_id(source_url)
            if kagane_id:
                new_metadata = get_series_info(kagane_id)
        
        # Add source to database
        from .database import add_source_to_series, get_db, release_db
        source_id = add_source_to_series(
            series_id, 
            source_url, 
            source_type, 
            is_primary=False
        )
        
        if not source_id:
            return jsonify({'error': 'Failed to add source'}), 500
        
        # *** FIX: Merge metadata with existing series ***
        if new_metadata:
            conn = get_db()
            cursor = conn.cursor()
            
            # Get existing series data
            cursor.execute("""
                SELECT alt_titles, genres, searchable_text 
                FROM series WHERE id = ?
            """, (series_id,))
            row = cursor.fetchone()
            
            if row:
                existing_alt_titles_json, existing_genres_json, existing_searchable_text = row
                
                # Parse existing data
                try:
                    existing_alt_titles = json.loads(existing_alt_titles_json) if existing_alt_titles_json else []
                except:
                    existing_alt_titles = []
                
                try:
                    existing_genres = json.loads(existing_genres_json) if existing_genres_json else []
                except:
                    existing_genres = []
                
                # Get new data
                new_alt_titles = new_metadata.get('alt_titles', [])
                new_genres = new_metadata.get('genres', [])
                
                # *** CRITICAL FIX: Merge lists properly ***
                # Convert to sets to remove duplicates, then back to sorted lists
                if isinstance(new_alt_titles, dict):
                    new_alt_titles = list(new_alt_titles.values())
                elif not isinstance(new_alt_titles, list):
                    new_alt_titles = [str(new_alt_titles)] if new_alt_titles else []
                
                merged_alt_titles = list(set(existing_alt_titles + new_alt_titles))
                merged_genres = list(set(existing_genres + new_genres))
                
                # Rebuild searchable text from ALL titles
                cursor.execute("""
                    SELECT title, title_en, title_romaji, title_native 
                    FROM series WHERE id = ?
                """, (series_id,))
                title_row = cursor.fetchone()
                
                if title_row:
                    all_titles = [
                        title_row[0],  # title
                        title_row[1],  # title_en
                        title_row[2],  # title_romaji
                        title_row[3],  # title_native
                    ] + merged_alt_titles
                    
                    # Remove None/empty values
                    all_titles = [t for t in all_titles if t and isinstance(t, str)]
                    
                    # Normalize and deduplicate
                    from .database import normalize_for_search
                    searchable_text = normalize_for_search(" ".join(all_titles))
                    
                    # Update database with merged data
                    cursor.execute("""
                        UPDATE series 
                        SET alt_titles = ?, 
                            genres = ?, 
                            searchable_text = ?
                        WHERE id = ?
                    """, (
                        json.dumps(merged_alt_titles, ensure_ascii=False),
                        json.dumps(merged_genres, ensure_ascii=False),
                        searchable_text,
                        series_id
                    ))
                    
                    print(f"[Add Source] Merged metadata for series {series_id}")
                    print(f"  - Alt titles: {len(existing_alt_titles)} + {len(new_alt_titles)} = {len(merged_alt_titles)}")
                    print(f"  - Genres: {len(existing_genres)} + {len(new_genres)} = {len(merged_genres)}")
            
            release_db(conn)
        
        # Trigger chapter fetch for new source
        try:
            from . import api
            if hasattr(api, 'manga_scheduler'):
                api.manga_scheduler.scan_series(series_id)
        except Exception as e:
            print(f"[Add Source] Failed to trigger scan: {e}")
        
        return jsonify({'success': True, 'source_id': source_id})
            
    except Exception as e:
        print(f"[Add Source] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<int:series_id>/sources/<int:source_id>/primary', methods=['POST'])
def api_set_primary_source(series_id, source_id):
    """Set a source as primary."""
    try:
        from .database import set_primary_source
        success = set_primary_source(series_id, source_id)
        
        if success:
            # Log the change
            try:
                from .activity_logger import log_activity
                from .database import get_db, release_db
                
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("SELECT title FROM series WHERE id = ?", (series_id,))
                title = cursor.fetchone()
                cursor.execute("SELECT source_url, source_type FROM series_sources WHERE id = ?", (source_id,))
                source_info = cursor.fetchone()
                release_db(conn)
                
                if title and source_info:
                    log_activity(
                        action_type='edited',
                        series_id=series_id,
                        series_title=title[0],
                        old_value={'primary_source': 'changed'},
                        new_value={
                            'primary_source': source_info[0],
                            'source_type': source_info[1]
                        }
                    )
            except Exception as log_err:
                print(f"[Set Primary] Logging failed: {log_err}")
            
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Failed to set primary source'}), 500
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/series/<int:series_id>/sources/<int:source_id>', methods=['DELETE'])
def api_remove_source(series_id, source_id):
    """Remove a source from a series."""
    try:
        from .database import remove_source
        success = remove_source(source_id)
        
        if success:
            # Rescan chapters from remaining sources
            try:
                from . import api
                if hasattr(api, 'manga_scheduler'):
                    api.manga_scheduler.scan_series(series_id)
            except Exception as e:
                print(f"[Remove Source] Failed to trigger rescan: {e}")
            
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Cannot remove primary or last source'}), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500
  
if __name__ == '__main__':
    run_server()