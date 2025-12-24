# backend/database.py
import sqlite3
import os
import json
from datetime import datetime, timezone
from threading import Lock
import glob
import re
import unicodedata

def normalize_for_search(text):
    """Normalize text for robust searching: lowercase, remove punctuation, strip diacritics."""
    if not text:
        return ""
    # Lowercase
    text = text.lower()
    # Remove common punctuation that doesn't affect meaning
    for char in "''.-_":
        text = text.replace(char, "")
    # Remove diacritics (é → e, ñ → n, etc.)
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    # Collapse whitespace (though less needed now)
    text = ''.join(text.split())
    return text

DB_PATH = "data/tracker.db"
_db_lock = Lock()

# Ensure data dir exists early
os.makedirs("data", exist_ok=True)

def get_db():
    _db_lock.acquire()
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")  # 10s wait for lock
    # *** FIX 1: Ensure foreign keys are enabled to trigger CASCADE deletes ***
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def release_db(conn):
    """Commit, close, and release global lock."""
    try:
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()
        _db_lock.release()

def add_source_to_series(series_id, source_url, source_type, is_primary=False):
    """
    Add a new source to an existing series.
    If is_primary=True, demotes the current primary source.
    """
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # If setting as primary, demote current primary
        if is_primary:
            cursor.execute("""
                UPDATE series_sources 
                SET is_primary = 0 
                WHERE series_id = ?
            """, (series_id,))
        
        # Add new source
        cursor.execute("""
            INSERT INTO series_sources (series_id, source_url, source_type, is_primary)
            VALUES (?, ?, ?, ?)
        """, (series_id, source_url, source_type, int(is_primary)))
        
        source_id = cursor.lastrowid
        release_db(conn)
        return source_id
        
    except Exception as e:
        print(f"[Database] Failed to add source: {e}")
        try:
            release_db(conn)
        except:
            pass
        return None


def migrate_to_multi_source():
    """
    Migrate from single source_url to multi-source architecture.
    Creates series_sources table and migrates existing data.
    """
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Check if migration already done
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='series_sources'")
        if cursor.fetchone():
            print("[Migration] Multi-source tables already exist, skipping migration")
            release_db(conn)
            return
        
        print("[Migration] Starting multi-source migration...")
        
        # 1. Create new series_sources table
        cursor.execute("""
            CREATE TABLE series_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER NOT NULL,
                source_url TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL,
                is_primary BOOLEAN DEFAULT 0,
                last_check DATETIME,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        """)
        
        # 2. Create indexes for performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sources_series ON series_sources(series_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sources_primary ON series_sources(series_id, is_primary)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_url ON series_sources(source_url)")
        
        # 3. Migrate existing series.source_url to series_sources
        cursor.execute("SELECT id, source_url, last_check FROM series WHERE source_url IS NOT NULL")
        existing_series = cursor.fetchall()
        
        migrated_count = 0
        for series_id, source_url, last_check in existing_series:
            # Detect source type
            if 'mangadex.org' in source_url:
                source_type = 'mangadex'
            elif 'kagane.org' in source_url:
                source_type = 'kagane'
            else:
                source_type = 'unknown'
            
            cursor.execute("""
                INSERT INTO series_sources (series_id, source_url, source_type, is_primary, last_check)
                VALUES (?, ?, ?, 1, ?)
            """, (series_id, source_url, source_type, last_check))
            migrated_count += 1
        
        print(f"[Migration] Migrated {migrated_count} series to multi-source format")
        
        # 4. Keep source_url column for backward compatibility (will be deprecated later)
        # Don't drop it yet to avoid breaking existing code during transition
        
        release_db(conn)
        print("[Migration] Multi-source migration completed successfully!")
        
    except Exception as e:
        print(f"[Migration] Failed: {e}")
        try:
            release_db(conn)
        except:
            pass
        raise

def get_series_sources(series_id):
    """Get all sources for a series, ordered by primary first."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, source_url, source_type, is_primary, last_check
        FROM series_sources
        WHERE series_id = ?
        ORDER BY is_primary DESC, added_at ASC
    """, (series_id,))
    
    rows = cursor.fetchall()
    release_db(conn)
    
    sources = []
    for row in rows:
        sources.append({
            'id': row[0],
            'source_url': row[1],
            'source_type': row[2],
            'is_primary': bool(row[3]),
            'last_check': row[4]
        })
    
    return sources


def set_primary_source(series_id, source_id):
    """Set a source as primary for a series."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Demote all sources for this series
        cursor.execute("""
            UPDATE series_sources 
            SET is_primary = 0 
            WHERE series_id = ?
        """, (series_id,))
        
        # Promote the selected source
        cursor.execute("""
            UPDATE series_sources 
            SET is_primary = 1 
            WHERE id = ? AND series_id = ?
        """, (source_id, series_id))
        
        release_db(conn)
        return True
        
    except Exception as e:
        print(f"[Database] Failed to set primary source: {e}")
        try:
            release_db(conn)
        except:
            pass
        return False


def remove_source(source_id):
    """Remove a source (only if not primary or not last source)."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Check if primary
        cursor.execute("SELECT series_id, is_primary FROM series_sources WHERE id = ?", (source_id,))
        row = cursor.fetchone()
        
        if not row:
            release_db(conn)
            return False
        
        series_id, is_primary = row
        
        # Count total sources for this series
        cursor.execute("SELECT COUNT(*) FROM series_sources WHERE series_id = ?", (series_id,))
        count = cursor.fetchone()[0]
        
        # Don't allow removing the last source
        if count <= 1:
            release_db(conn)
            return False
        
        # Don't allow removing primary source (must change primary first)
        if is_primary:
            release_db(conn)
            return False
        
        # Delete the source
        cursor.execute("DELETE FROM series_sources WHERE id = ?", (source_id,))
        
        release_db(conn)
        return True
        
    except Exception as e:
        print(f"[Database] Failed to remove source: {e}")
        try:
            release_db(conn)
        except:
            pass
        return False

def init_db():
    os.makedirs("data", exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")

    try:
        from .activity_logger import init_activity_log_table
        release_db(conn)
        init_activity_log_table()
        conn = get_db()
        cursor = conn.cursor()
    except Exception as e:
        print(f"[Init DB] Activity log init failed: {e}")


    # --- 1. Create tables if not exist ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            title_en TEXT,
            title_romaji TEXT,
            title_native TEXT,
            source_url TEXT NOT NULL UNIQUE,
            cover_url TEXT,
            banner_url TEXT,
            status TEXT NOT NULL CHECK(status IN ('reading', 'plan_to_read', 'on_hold', 'dropped', 'completed')),
            source_status TEXT,
            source_type TEXT DEFAULT 'other',
            anilist_id INTEGER,
            alt_titles TEXT,
            current_chapter REAL NOT NULL DEFAULT -1,
            current_volume TEXT,       
            latest_chapter REAL,
            total_chapters INTEGER DEFAULT 0, 
            latest_release DATETIME,
            last_check DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            genres TEXT,
            content_rating TEXT DEFAULT 'unknown'
        )
    """)

    # *** FIX 1: Ensure chapters table has proper CASCADE delete ***
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL,
            volume TEXT,
            raw_chapter TEXT,
            chapter_number REAL NOT NULL,
            title TEXT,
            release_date DATETIME,
            chapter_url TEXT,
            is_oneshot BOOLEAN DEFAULT 0,
            FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # --- 2. Add missing columns to existing tables ---
    cursor.execute("PRAGMA table_info(series)")
    columns = {row[1] for row in cursor.fetchall()}

    if "searchable_text" not in columns:
        cursor.execute("ALTER TABLE series ADD COLUMN searchable_text TEXT")

    if "genres" not in columns:
        cursor.execute("ALTER TABLE series ADD COLUMN genres TEXT")

    if "content_rating" not in columns:
        cursor.execute("ALTER TABLE series ADD COLUMN content_rating TEXT DEFAULT 'unknown'")

    if "source_type" not in columns:
        cursor.execute("ALTER TABLE series ADD COLUMN source_type TEXT DEFAULT 'other'")
        
    # --- 3. Set default meta values ---
    cursor.execute("""
        INSERT OR IGNORE INTO meta (key, value)
        VALUES 
            ('last_dashboard_visit', '1970-01-01T00:00:00Z'),
            ('schema_version', '2')
    """)

    # --- 4. Create indexes ---
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_series_status ON series(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_series_title ON series(title)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chapters_series ON chapters(series_id)")

    release_db(conn)

    # ✅ RUN MULTI-SOURCE MIGRATION
    migrate_to_multi_source()

    # Run backfill AFTER releasing DB lock
    backfill_searchable_text()

def update_last_dashboard_visit():
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE meta SET value = ? WHERE key = 'last_dashboard_visit'",
        (now,)
    )
    release_db(conn)

def get_series_page(page=1, per_page=50):
    offset = (page - 1) * per_page
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT *,
               COALESCE(latest_chapter, 0) - current_chapter AS unread_count
        FROM series
        ORDER BY title
        LIMIT ? OFFSET ?
    """, (per_page, offset))
    rows = cursor.fetchall()
    release_db(conn)
    return [dict(row) for row in rows]

def get_series_count():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM series")
    count = cursor.fetchone()[0]
    release_db(conn)
    return count

def update_series(series_id, updates):
    conn = get_db()
    cursor = conn.cursor()
    set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values()) + [series_id]
    cursor.execute(f"UPDATE series SET {set_clause} WHERE id = ?", values)
    release_db(conn)

def add_series(title, source_url, status="plan_to_read", cover_url=None, banner_url=None, anilist_id=None,
               title_en=None, title_romaji=None, title_native=None, source_status=None, alt_titles=None,
               genres=None, content_rating=None, source_type=None):
    conn = get_db()
    cursor = conn.cursor()
    
    # Ensure table has required columns (for backward compatibility)
    cursor.execute("PRAGMA table_info(series)")
    columns = {row[1] for row in cursor.fetchall()}
    
    alt_titles_json = json.dumps(alt_titles, ensure_ascii=False) if alt_titles else None
    genres_json = json.dumps(genres, ensure_ascii=False) if genres else None
    
    # Build searchable text from all title fields
    all_titles = [title, title_en, title_romaji, title_native]
    if alt_titles:
        if isinstance(alt_titles, dict):
            all_titles.extend(alt_titles.values())
        elif isinstance(alt_titles, list):
            all_titles.extend(alt_titles)
        else:
            all_titles.append(str(alt_titles))

    searchable_parts = []
    for t in all_titles:
        if t and isinstance(t, str):
            searchable_parts.append(t)
    searchable_text = normalize_for_search(" ".join(searchable_parts))

    # Build column list and values dynamically
    cols = [
        "title", "title_en", "title_romaji", "title_native",
        "source_url", "cover_url", "banner_url", "anilist_id",
        "status", "source_status", "alt_titles", "searchable_text"
    ]
    vals = [
        title, title_en, title_romaji, title_native,
        source_url, cover_url, banner_url, anilist_id,
        status, source_status, alt_titles_json, searchable_text
    ]
    
    # Add optional fields if columns exist
    if "genres" in columns:
        cols.append("genres")
        vals.append(genres_json)
    if "content_rating" in columns:
        cols.append("content_rating")
        vals.append(content_rating)
    if "source_type" in columns:
        cols.append("source_type")
        vals.append(source_type)
    
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    
    cursor.execute(f"""
        INSERT INTO series ({col_names})
        VALUES ({placeholders})
    """, vals)
    
    series_id = cursor.lastrowid
    release_db(conn)
    return series_id

def delete_series(series_id):
    """
    Delete a series and all its chapters (CASCADE).
    Returns True if successful, False otherwise.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Verify series exists
        cursor.execute("SELECT id FROM series WHERE id = ?", (series_id,))
        if not cursor.fetchone():
            release_db(conn)
            return False
        
        # Delete series (chapters will be auto-deleted via CASCADE)
        cursor.execute("DELETE FROM series WHERE id = ?", (series_id,))
        deleted = cursor.rowcount > 0
        
        release_db(conn)
        return deleted
    except Exception as e:
        print(f"[Database] Delete series {series_id} failed: {e}")
        try:
            release_db(conn)
        except:
            pass
        return False

def backfill_searchable_text():
    """Populate searchable_text for existing series (run once)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, title_en, title_romaji, title_native, alt_titles FROM series WHERE searchable_text IS NULL OR searchable_text = ''")
    rows = cursor.fetchall()
    for row in rows:
        series_id, title, title_en, title_romaji, title_native, alt_titles_json = row
        try:
            alt_titles = json.loads(alt_titles_json) if alt_titles_json else None
        except:
            alt_titles = None

        all_titles = [title, title_en, title_romaji, title_native]
        if alt_titles:
            if isinstance(alt_titles, dict):
                all_titles.extend(alt_titles.values())
            elif isinstance(alt_titles, list):
                all_titles.extend(alt_titles)
            else:
                all_titles.append(str(alt_titles))

        searchable_parts = []
        for t in all_titles:
            if t and isinstance(t, str):
                searchable_parts.append(t)
        searchable_text = normalize_for_search(" ".join(searchable_parts))

        cursor.execute("UPDATE series SET searchable_text = ? WHERE id = ?", (searchable_text, series_id))
    conn.commit()
    release_db(conn)

def get_unread_reading_count():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM meta WHERE key = 'last_dashboard_visit'")
    last_visit = cursor.fetchone()[0]
    cursor.execute("""
        SELECT COUNT(*)
        FROM series s
        WHERE s.status = 'reading'
          AND s.latest_chapter > s.current_chapter
          AND s.latest_release > ?
    """, (last_visit,))
    count = cursor.fetchone()[0]
    release_db(conn)
    return count