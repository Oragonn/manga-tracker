# backend/activity_logger.py
"""
Activity logging system with undo support.
Logs all user actions (add, delete, status changes, progress updates).
"""

import json
from datetime import datetime, timezone, timedelta
from .database import get_db, release_db

def init_activity_log_table():
    """Create activity_log table if it doesn't exist."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            action_type TEXT NOT NULL,
            series_id INTEGER,
            series_title TEXT,
            old_value TEXT,
            new_value TEXT,
            is_bulk BOOLEAN DEFAULT 0,
            bulk_id TEXT,
            can_undo BOOLEAN DEFAULT 1,
            FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE SET NULL
        )
    """)
    
    # Create indexes for performance
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_type ON activity_log(action_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_bulk ON activity_log(bulk_id)")
    
    release_db(conn)

def detect_source_type(url):
    """Detect source type from URL."""
    if 'mangadex.org' in url:
        return 'MangaDex'
    elif 'kagane.org' in url or 'kagane.to' in url:
        return 'Kagane'
    elif 'atsu.moe' in url:
        return 'Atsumaru'
    elif 'asurascans.com' in url:
        return 'AsuraScans'
    elif 'hivetoons.org' in url:
        return 'HiveToons'
    return 'Unknown'

def log_activity(action_type, series_id=None, series_title=None, old_value=None, new_value=None,
                 is_bulk=False, bulk_id=None, can_undo=True):
    """
    Log an activity to the database.

    Args:
        action_type: 'added', 'deleted', 'progress', 'status', 'edited',
                     'source_added', 'source_removed',
                     'bookmark_added', 'bookmark_updated', 'bookmark_deleted'
        series_id: ID of the series (None if deleted, or not series-scoped)
        series_title: Title of the series (or bookmark name, for bookmark events)
        old_value: Dict of old values (will be JSON-encoded)
        new_value: Dict of new values (will be JSON-encoded)
        is_bulk: Whether this is part of a bulk operation
        bulk_id: Unique ID grouping bulk operations
        can_undo: Whether the undo endpoint knows how to revert this action type.
                  Set False for action types /api/logs/undo doesn't have a branch for.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        old_json = json.dumps(old_value, ensure_ascii=False) if old_value else None
        new_json = json.dumps(new_value, ensure_ascii=False) if new_value else None

        timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        cursor.execute("""
            INSERT INTO activity_log (
                timestamp, action_type, series_id, series_title,
                old_value, new_value, is_bulk, bulk_id, can_undo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (timestamp, action_type, series_id, series_title, old_json, new_json,
              int(is_bulk), bulk_id, int(can_undo)))
        
        release_db(conn)
    except Exception as e:
        print(f"[Activity Log] Failed to log {action_type}: {e}")
        # Don't crash the app if logging fails
        try:
            release_db(conn)
        except:
            pass

def get_series_snapshot(series_id):
    """
    Get a complete snapshot of a series for logging.
    Returns a dict suitable for old_value in delete operations.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM series WHERE id = ?", (series_id,))
        row = cursor.fetchone()

        if not row:
            release_db(conn)
            return None

        # Get column names
        columns = [desc[0] for desc in cursor.description]
        series_dict = dict(zip(columns, row))

        # All sources, not just the legacy primary column -- series_sources
        # rows cascade-delete with the series, so anything not captured here
        # is unrecoverable by undo. Primary first (matches the historical
        # sources[0]-is-primary assumption the undo handler relies on).
        cursor.execute("""
            SELECT source_url, source_type, is_primary, cover_url
            FROM series_sources WHERE series_id = ?
            ORDER BY is_primary DESC, id ASC
        """, (series_id,))
        source_rows = cursor.fetchall()

        if source_rows:
            sources = [{
                'url': r[0], 'type': r[1], 'is_primary': bool(r[2]), 'cover_url': r[3]
            } for r in source_rows]
        else:
            # Pre-multi-source series (or the migration hasn't run): fall
            # back to the legacy single source_url column.
            sources = [{
                'url': series_dict.get('source_url'),
                'type': detect_source_type(series_dict.get('source_url', '')),
                'is_primary': True,
                'cover_url': None
            }]
        # current_chapter/latest_chapter are series-level, not per-source --
        # attach them to the primary entry since that's what the undo
        # handler reads them off of.
        sources[0]['current_chapter'] = series_dict.get('current_chapter')
        sources[0]['latest_chapter'] = series_dict.get('latest_chapter')

        # Custom tags cascade-delete with the series too. Store names, not
        # ids -- create_custom_tag() is idempotent by name, so restoring
        # doesn't care whether the original tag row still exists.
        cursor.execute("""
            SELECT ct.name FROM series_custom_tags sct
            JOIN custom_tags ct ON ct.id = sct.tag_id
            WHERE sct.series_id = ?
        """, (series_id,))
        custom_tags = [r[0] for r in cursor.fetchall()]

        # Store as future-proof multi-source format
        snapshot = {
            'title': series_dict.get('title'),
            'sources': sources,
            'custom_tags': custom_tags,
            'status': series_dict.get('status'),
            'cover_url': series_dict.get('cover_url'),
            'banner_url': series_dict.get('banner_url'),
            'anilist_id': series_dict.get('anilist_id'),
            'title_en': series_dict.get('title_en'),
            'title_romaji': series_dict.get('title_romaji'),
            'title_native': series_dict.get('title_native'),
            'source_status': series_dict.get('source_status'),
            'source_type': series_dict.get('source_type'),
            'alt_titles': series_dict.get('alt_titles'),
            'genres': series_dict.get('genres'),
            'content_rating': series_dict.get('content_rating', 'unknown')
        }

        release_db(conn)
        return snapshot
    except Exception as e:
        print(f"[Activity Log] Failed to get series snapshot: {e}")
        try:
            release_db(conn)
        except:
            pass
        return None

def cleanup_old_logs():
    """Delete logs older than 14 days."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        cutoff_str = cutoff.isoformat().replace('+00:00', 'Z')
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM activity_log WHERE timestamp < ?", (cutoff_str,))
        deleted = cursor.rowcount
        release_db(conn)
        
        if deleted > 0:
            print(f"[Activity Log] Cleaned up {deleted} old log entries")
    except Exception as e:
        print(f"[Activity Log] Cleanup error: {e}")
        try:
            release_db(conn)
        except:
            pass

def mark_log_undone(log_id=None, bulk_id=None):
    """Mark a log entry (or bulk group) as undone (can_undo = 0)."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if bulk_id:
            cursor.execute("UPDATE activity_log SET can_undo = 0 WHERE bulk_id = ?", (bulk_id,))
        elif log_id:
            cursor.execute("UPDATE activity_log SET can_undo = 0 WHERE id = ?", (log_id,))
        
        release_db(conn)
    except Exception as e:
        print(f"[Activity Log] Failed to mark as undone: {e}")
        try:
            release_db(conn)
        except:
            pass

def get_logs(type_filter='all', time_filter='all', search_query='', limit=100):
    """
    Get activity logs with filters.
    
    Returns list of dicts with formatted display data.
    Groups bulk operations into single entries.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        where_parts = []
        params = []
        
        # Type filter. 'source' and 'bookmark' are grouped filters covering
        # multiple underlying action_type values (added/removed/updated).
        if type_filter == 'source':
            where_parts.append("action_type IN ('source_added', 'source_removed')")
        elif type_filter == 'bookmark':
            where_parts.append("action_type IN ('bookmark_added', 'bookmark_updated', 'bookmark_deleted')")
        elif type_filter != 'all':
            where_parts.append("action_type = ?")
            params.append(type_filter)
        
        # Time filter
        if time_filter != 'all':
            now = datetime.now(timezone.utc)
            if time_filter == 'today':
                cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif time_filter == 'week':
                cutoff = now - timedelta(days=7)
            elif time_filter == 'month':
                cutoff = now - timedelta(days=30)
            else:
                cutoff = None
            
            if cutoff:
                cutoff_str = cutoff.isoformat().replace('+00:00', 'Z')
                where_parts.append("timestamp >= ?")
                params.append(cutoff_str)
        
        # Search filter
        if search_query:
            where_parts.append("series_title LIKE ?")
            params.append(f"%{search_query}%")
        
        where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""
        
        # *** FIX: Get distinct bulk_ids first, then fetch one representative entry per bulk_id ***
        query = f"""
            SELECT 
                MIN(id) as id,
                timestamp,
                action_type,
                series_id,
                series_title,
                old_value,
                new_value,
                is_bulk,
                bulk_id,
                can_undo
            FROM activity_log
            {where_clause}
            GROUP BY COALESCE(bulk_id, 'single_' || id)
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params.append(limit)
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Format results
        results = []
        now = datetime.now(timezone.utc)
        undo_cutoff = now - timedelta(days=7)
        
        for row in rows:
            log_id, timestamp_str, action_type, series_id, series_title, old_value, new_value, is_bulk, bulk_id, can_undo = row
            
            # Parse timestamp
            try:
                timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            except:
                timestamp = now
            
            # Check if still within undo window
            can_undo_time = timestamp >= undo_cutoff
            
            # Format display time
            diff = now - timestamp
            if diff.total_seconds() < 60:
                display_time = "Just now"
            elif diff.total_seconds() < 3600:
                mins = int(diff.total_seconds() / 60)
                display_time = f"{mins}m ago"
            elif diff.total_seconds() < 86400:
                hours = int(diff.total_seconds() / 3600)
                display_time = f"{hours}h ago"
            else:
                days = diff.days
                display_time = f"{days}d ago"
            
            # Get series list for bulk operations
            series_list = None
            affected_count = 1
            if is_bulk and bulk_id:
                # Get all series in this bulk operation
                cursor.execute("""
                    SELECT series_title FROM activity_log 
                    WHERE bulk_id = ? 
                    ORDER BY timestamp
                """, (bulk_id,))
                bulk_series = cursor.fetchall()
                affected_count = len(bulk_series)
                
                if affected_count <= 5:
                    series_list = "<br>".join([f"• {s[0]}" for s in bulk_series])
                else:
                    series_list = "<br>".join([f"• {s[0]}" for s in bulk_series[:3]])
                    series_list += f"<br>• ... and {affected_count - 3} more"
            
            results.append({
                'id': log_id,
                'timestamp': timestamp_str,
                'display_time': display_time,
                'action_type': action_type,
                'series_title': series_title,
                'old_value': old_value,
                'new_value': new_value,
                'is_bulk': bool(is_bulk),
                'bulk_id': bulk_id,
                'can_undo': bool(can_undo),
                'can_undo_time': can_undo_time,
                'series_list': series_list,
                'affected_count': affected_count
            })
        
        release_db(conn)
        return results
    except Exception as e:
        print(f"[Activity Log] Failed to get logs: {e}")
        try:
            release_db(conn)
        except:
            pass
        return []