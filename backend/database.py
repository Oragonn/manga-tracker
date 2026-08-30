# backend/database.py
import sqlite3
import os
import json
from datetime import datetime, timezone, timedelta  # ADDED: timedelta
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

def add_source_to_series(series_id, source_url, source_type, is_primary=False, cover_url=None):
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
            INSERT INTO series_sources (series_id, source_url, source_type, is_primary, cover_url)
            VALUES (?, ?, ?, ?, ?)
        """, (series_id, source_url, source_type, int(is_primary), cover_url))
        
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
            # Table exists from an earlier version of this migration -- make
            # sure newer columns added since then are present too.
            cursor.execute("PRAGMA table_info(series_sources)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            if "cover_url" not in existing_columns:
                cursor.execute("ALTER TABLE series_sources ADD COLUMN cover_url TEXT")
                print("[Migration] Added cover_url column to series_sources")
            if "consecutive_failures" not in existing_columns:
                cursor.execute("ALTER TABLE series_sources ADD COLUMN consecutive_failures INTEGER DEFAULT 0")
                cursor.execute("ALTER TABLE series_sources ADD COLUMN last_error TEXT")
                cursor.execute("ALTER TABLE series_sources ADD COLUMN last_failure_at DATETIME")
                print("[Migration] Added source-health columns to series_sources")
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
                cover_url TEXT,
                last_check DATETIME,
                added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                consecutive_failures INTEGER DEFAULT 0,
                last_error TEXT,
                last_failure_at DATETIME,
                FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
            )
        """)
        
        # 2. Create indexes for performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sources_series ON series_sources(series_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sources_primary ON series_sources(series_id, is_primary)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_url ON series_sources(source_url)")
        
        # 3. Migrate existing series.source_url to series_sources
        cursor.execute("SELECT id, source_url, last_check, cover_url FROM series WHERE source_url IS NOT NULL")
        existing_series = cursor.fetchall()

        migrated_count = 0
        for series_id, source_url, last_check, cover_url in existing_series:
            # Detect source type
            if 'mangadex.org' in source_url:
                source_type = 'mangadex'
            elif 'kagane.org' in source_url or 'kagane.to' in source_url:
                source_type = 'kagane'
            elif 'atsu.moe' in source_url:
                source_type = 'atsu'
            elif 'asurascans.com' in source_url:
                source_type = 'asura'
            else:
                source_type = 'unknown'

            cursor.execute("""
                INSERT INTO series_sources (series_id, source_url, source_type, is_primary, last_check, cover_url)
                VALUES (?, ?, ?, 1, ?, ?)
            """, (series_id, source_url, source_type, last_check, cover_url))
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
        SELECT id, source_url, source_type, is_primary, last_check, cover_url
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
            'last_check': row[4],
            'cover_url': row[5]
        })
    
    return sources


def record_source_failure(source_id, error_msg):
    """Bumps a source's consecutive-failure streak - called by the
    scheduler when a source's fetch raises. Feeds the dashboard's
    source-health indicator; doesn't touch the series-level last_check,
    which keeps advancing even while this one source is broken."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE series_sources
            SET consecutive_failures = consecutive_failures + 1,
                last_error = ?,
                last_failure_at = ?
            WHERE id = ?
        """, (str(error_msg)[:500], datetime.now(timezone.utc).isoformat(), source_id))
    finally:
        release_db(conn)


def record_source_success(source_id):
    """Resets a source's failure streak - called whenever its fetch
    succeeds, including a legitimate zero-new-chapters result (that's not
    a failure, just nothing new)."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE series_sources
            SET consecutive_failures = 0, last_error = NULL, last_failure_at = NULL
            WHERE id = ?
        """, (source_id,))
    finally:
        release_db(conn)


def get_unhealthy_sources(threshold=3):
    """Sources with >= threshold consecutive failures, joined with their
    series' title - backs the dashboard's source-health dropdown."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ss.id, ss.series_id, s.title, ss.source_type, ss.source_url,
               ss.consecutive_failures, ss.last_error, ss.last_failure_at
        FROM series_sources ss
        JOIN series s ON s.id = ss.series_id
        WHERE ss.consecutive_failures >= ?
        ORDER BY ss.consecutive_failures DESC, ss.last_failure_at DESC
    """, (threshold,))
    rows = cursor.fetchall()
    release_db(conn)
    return [
        {
            'source_id': r[0],
            'series_id': r[1],
            'series_title': r[2],
            'source_type': r[3],
            'source_url': r[4],
            'consecutive_failures': r[5],
            'last_error': r[6],
            'last_failure_at': r[7]
        }
        for r in rows
    ]


def get_all_series_for_backup():
    """Get all series with their primary source, shaped for the Kenmei-style CSV backup."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT s.title, s.status, s.current_chapter, src.source_type, src.source_url
        FROM series s
        LEFT JOIN series_sources src
            ON src.series_id = s.id AND src.is_primary = 1
        ORDER BY s.title COLLATE NOCASE
    """)

    rows = cursor.fetchall()
    release_db(conn)

    series = []
    for row in rows:
        series.append({
            'title': row[0],
            'status': row[1],
            'current_chapter': row[2],
            'source_type': row[3],
            'source_url': row[4]
        })

    return series


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


def add_series_cover(series_id, cover_url):
    """Record an uploaded cover so it can be picked again later without re-uploading."""
    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO series_covers (series_id, cover_url)
            VALUES (?, ?)
        """, (series_id, cover_url))
        cover_id = cursor.lastrowid
        release_db(conn)
        return cover_id
    except Exception as e:
        print(f"[Database] Failed to record series cover: {e}")
        try:
            release_db(conn)
        except:
            pass
        return None


def get_series_covers(series_id):
    """Get all covers previously uploaded for a series, most recent first."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, cover_url, uploaded_at
        FROM series_covers
        WHERE series_id = ?
        ORDER BY uploaded_at DESC, id DESC
    """, (series_id,))

    rows = cursor.fetchall()
    release_db(conn)

    return [
        {'id': row[0], 'cover_url': row[1], 'uploaded_at': row[2]}
        for row in rows
    ]


def delete_series_cover(cover_id, series_id):
    """Delete a previously-uploaded cover record. Returns its cover_url (so
    the caller can also remove the file on disk) or None if not found."""
    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT cover_url FROM series_covers WHERE id = ? AND series_id = ?",
            (cover_id, series_id)
        )
        row = cursor.fetchone()
        if not row:
            release_db(conn)
            return None

        cursor.execute("DELETE FROM series_covers WHERE id = ?", (cover_id,))
        release_db(conn)
        return row[0]
    except Exception as e:
        print(f"[Database] Failed to delete series cover: {e}")
        try:
            release_db(conn)
        except:
            pass
        return None


def get_custom_tags():
    """All custom tags, alphabetical."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM custom_tags ORDER BY name COLLATE NOCASE")
    rows = cursor.fetchall()
    release_db(conn)
    return [{'id': row[0], 'name': row[1]} for row in rows]


def create_custom_tag(name):
    """Create a tag, or return the id of the existing one with that name
    (case-insensitive) so re-typing an existing tag never creates a dup."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM custom_tags WHERE name = ? COLLATE NOCASE", (name,))
        existing = cursor.fetchone()
        if existing:
            release_db(conn)
            return existing[0]

        cursor.execute("INSERT INTO custom_tags (name) VALUES (?)", (name,))
        tag_id = cursor.lastrowid
        release_db(conn)
        return tag_id
    except Exception as e:
        print(f"[Database] Failed to create custom tag: {e}")
        try:
            release_db(conn)
        except:
            pass
        return None


def delete_custom_tag(tag_id):
    """Delete a tag globally -- series_custom_tags rows cascade with it."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM custom_tags WHERE id = ?", (tag_id,))
        deleted = cursor.rowcount > 0
        release_db(conn)
        return deleted
    except Exception as e:
        print(f"[Database] Failed to delete custom tag: {e}")
        try:
            release_db(conn)
        except:
            pass
        return False


def get_series_custom_tag_ids(series_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT tag_id FROM series_custom_tags WHERE series_id = ?", (series_id,))
    rows = cursor.fetchall()
    release_db(conn)
    return [row[0] for row in rows]


def add_custom_tag_to_series(series_id, tag_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO series_custom_tags (series_id, tag_id) VALUES (?, ?)",
            (series_id, tag_id)
        )
        release_db(conn)
        return True
    except Exception as e:
        print(f"[Database] Failed to attach custom tag: {e}")
        try:
            release_db(conn)
        except:
            pass
        return False


def remove_custom_tag_from_series(series_id, tag_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM series_custom_tags WHERE series_id = ? AND tag_id = ?",
            (series_id, tag_id)
        )
        release_db(conn)
        return True
    except Exception as e:
        print(f"[Database] Failed to detach custom tag: {e}")
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

    # Covers a user has uploaded for a series, kept around (even after the
    # series moves on to a different cover) so the settings-modal cover
    # picker can offer "one you already uploaded" without re-uploading it.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS series_covers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER NOT NULL,
            cover_url TEXT NOT NULL,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_series_covers_series ON series_covers(series_id)")

    # User-defined tags (distinct from the scraped `genres` column) -- a
    # shared, reusable vocabulary the user builds up from the Series Settings
    # modal, filterable from the same dropdown as genres/rating.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS series_custom_tags (
            series_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (series_id, tag_id),
            FOREIGN KEY (series_id) REFERENCES series(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES custom_tags(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_series_custom_tags_series ON series_custom_tags(series_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_series_custom_tags_tag ON series_custom_tags(tag_id)")

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
    
    if "created_at" not in columns:
        cursor.execute("ALTER TABLE series ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
        # Backfill existing series with a reasonable timestamp (use last_check or now)
        cursor.execute("""
            UPDATE series 
            SET created_at = COALESCE(last_check, CURRENT_TIMESTAMP)
            WHERE created_at IS NULL
        """)
        print("[Database] Added and backfilled created_at column")
        
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
    
    # ✅ INITIALIZE STATS HISTORY TABLE
    init_stats_history_table()

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
    """
    Add a new series AND create its primary source entry.
    This is the critical fix for Bug #1.
    """
    conn = get_db()
    cursor = conn.cursor()
    
    try:
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
        
        # *** FIX: Create primary source entry ***
        # Detect source type from URL
        if 'mangadex.org' in source_url:
            detected_source_type = 'mangadex'
        elif 'kagane.org' in source_url or 'kagane.to' in source_url:
            detected_source_type = 'kagane'
        elif 'atsu.moe' in source_url:
            detected_source_type = 'atsu'
        elif 'asurascans.com' in source_url:
            detected_source_type = 'asura'
        else:
            detected_source_type = 'unknown'
        
        cursor.execute("""
            INSERT INTO series_sources (series_id, source_url, source_type, is_primary, cover_url)
            VALUES (?, ?, ?, 1, ?)
        """, (series_id, source_url, detected_source_type, cover_url))
        
        release_db(conn)
        return series_id
        
    except sqlite3.IntegrityError as e:
        # CRITICAL: Release the lock before re-raising
        try:
            release_db(conn)
        except:
            pass
        # Re-raise so caller can handle it
        raise
    except Exception as e:
        # CRITICAL: Release the lock on ANY error
        try:
            release_db(conn)
        except:
            pass
        raise

def delete_series(series_id):
    """
    Delete a series and all its chapters (CASCADE).
    Returns True if successful, False otherwise.
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # CHANGED: Get series info BEFORE deleting for stats tracking
        cursor.execute("SELECT created_at FROM series WHERE id = ?", (series_id,))
        series_data = cursor.fetchone()
        
        if not series_data:
            release_db(conn)
            return False
        
        created_at = series_data[0]
        
        # Delete series (chapters will be auto-deleted via CASCADE)
        cursor.execute("DELETE FROM series WHERE id = ?", (series_id,))
        deleted = cursor.rowcount > 0
        
        release_db(conn)
        
        # ADDED: Update stats to reflect deletion
        if deleted and created_at:
            try:
                adjust_stats_for_deletion(created_at)
            except Exception as stats_err:
                print(f"[Database] Failed to adjust stats for deletion: {stats_err}")
        
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

# === HISTORICAL STATS TRACKING ===

def init_stats_history_table():
    """Create table for storing historical reading statistics."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stats_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_type TEXT NOT NULL,  -- 'day', 'week', 'month', 'year'
            period_start TEXT NOT NULL,  -- ISO format date
            period_end TEXT NOT NULL,    -- ISO format date
            series_added INTEGER DEFAULT 0,
            chapters_read INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(period_type, period_start)
        )
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_stats_period ON stats_history(period_type, period_start)
    """)
    
    release_db(conn)
    print("[Database] Stats history table initialized")


def save_period_stats(period_type, period_start, period_end, series_added, chapters_read):
    """Save statistics for a completed period."""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO stats_history 
            (period_type, period_start, period_end, series_added, chapters_read)
            VALUES (?, ?, ?, ?, ?)
        """, (period_type, period_start, period_end, series_added, chapters_read))
        
        release_db(conn)
        return True
    except Exception as e:
        print(f"[Database] Failed to save period stats: {e}")
        try:
            release_db(conn)
        except:
            pass
        return False


def get_stats_history(period_type=None, limit=100):
    """Retrieve historical stats, optionally filtered by period type."""
    conn = get_db()
    cursor = conn.cursor()
    
    if period_type:
        cursor.execute("""
            SELECT period_type, period_start, period_end, series_added, chapters_read, created_at
            FROM stats_history
            WHERE period_type = ?
            ORDER BY period_start DESC
            LIMIT ?
        """, (period_type, limit))
    else:
        cursor.execute("""
            SELECT period_type, period_start, period_end, series_added, chapters_read, created_at
            FROM stats_history
            ORDER BY period_start DESC
            LIMIT ?
        """, (limit,))
    
    rows = cursor.fetchall()
    release_db(conn)
    
    history = []
    for row in rows:
        history.append({
            'period_type': row[0],
            'period_start': row[1],
            'period_end': row[2],
            'series_added': row[3],
            'chapters_read': row[4],
            'created_at': row[5]
        })
    
    return history


def get_yearly_totals():
    """Get total series added and chapters read for each year."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            substr(period_start, 1, 4) as year,
            SUM(series_added) as total_series_added,
            SUM(chapters_read) as total_chapters_read
        FROM stats_history
        WHERE period_type = 'year'
        GROUP BY year
        ORDER BY year DESC
    """)
    
    rows = cursor.fetchall()
    release_db(conn)
    
    yearly_totals = []
    for row in rows:
        yearly_totals.append({
            'year': row[0],
            'series_added': row[1],
            'chapters_read': row[2]
        })

    return yearly_totals


def update_current_period_stats():
    """
    Update stats for current day/week/month/year.
    Call this whenever a series is added or chapters are read.
    """
    from datetime import datetime, timezone, timedelta
    import json
    
    conn = None
    try:
        now = datetime.now(timezone.utc)
        conn = get_db()
        cursor = conn.cursor()
        
        # === TODAY ===
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        today_str = today_start.date().isoformat()
        
        # Count series added today (use DATE comparison for reliability)
        cursor.execute("""
            SELECT COUNT(*) FROM series 
            WHERE DATE(created_at) = DATE(?)
        """, (now.isoformat(),))
        series_added_today = cursor.fetchone()[0] or 0
        
        # Count chapters read today
        cursor.execute("""
            SELECT old_value, new_value
            FROM activity_log
            WHERE action_type = 'progress'
            AND timestamp >= ?
        """, (today_start.isoformat(),))
        
        chapters_read_today = 0
        for old_str, new_str in cursor.fetchall():
            try:
                old_val = json.loads(old_str) if old_str else {}
                new_val = json.loads(new_str) if new_str else {}
                old_ch = old_val.get('chapter', -1)
                new_ch = new_val.get('chapter', -1)
                
                # CHANGED: Handle both forward and backward progress
                if new_ch >= 0:  # New chapter is valid
                    if old_ch == -1:
                        # Started reading: count from 0 to new_ch
                        chapters_read_today += float(new_ch)
                    elif old_ch >= 0:
                        # Normal progress: count difference (can be negative if unread)
                        chapters_read_today += float(new_ch) - float(old_ch)
                elif new_ch == -1 and old_ch >= 0:
                    # ADDED: Reset to "Not started" - subtract all read chapters
                    chapters_read_today -= float(old_ch)
            except:
                continue
        
        chapters_read_today = round(chapters_read_today, 1)
        
        # Save today's stats
        cursor.execute("""
            INSERT OR REPLACE INTO stats_history 
            (period_type, period_start, period_end, series_added, chapters_read)
            VALUES (?, ?, ?, ?, ?)
        """, ('day', today_str, today_str, series_added_today, chapters_read_today))
        
        # === THIS WEEK ===
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = (week_start + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=999999)
        week_str = week_start.date().isoformat()
        
        cursor.execute("""
            SELECT COUNT(*) FROM series
            WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) <= DATETIME(?)
        """, (week_start.isoformat(), now.isoformat()))
        series_added_week = cursor.fetchone()[0] or 0
        
        cursor.execute("""
            SELECT old_value, new_value
            FROM activity_log
            WHERE action_type = 'progress'
            AND timestamp >= ? AND timestamp <= ?
        """, (week_start.isoformat(), now.isoformat()))
        
        chapters_read_week = 0
        for old_str, new_str in cursor.fetchall():
            try:
                old_val = json.loads(old_str) if old_str else {}
                new_val = json.loads(new_str) if new_str else {}
                old_ch = old_val.get('chapter', -1)
                new_ch = new_val.get('chapter', -1)
                
                # CHANGED: Handle both forward and backward progress
                if new_ch >= 0:
                    if old_ch == -1:
                        chapters_read_week += float(new_ch)
                    elif old_ch >= 0:
                        chapters_read_week += float(new_ch) - float(old_ch)
                elif new_ch == -1 and old_ch >= 0:
                    # ADDED: Reset to "Not started" - subtract all read chapters
                    chapters_read_week -= float(old_ch)
            except:
                continue
        
        chapters_read_week = round(chapters_read_week, 1)
        
        cursor.execute("""
            INSERT OR REPLACE INTO stats_history 
            (period_type, period_start, period_end, series_added, chapters_read)
            VALUES (?, ?, ?, ?, ?)
        """, ('week', week_str, week_end.date().isoformat(), series_added_week, chapters_read_week))
        
        # === THIS MONTH ===
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
        month_str = month_start.date().isoformat()
        
        cursor.execute("""
            SELECT COUNT(*) FROM series
            WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) <= DATETIME(?)
        """, (month_start.isoformat(), now.isoformat()))
        series_added_month = cursor.fetchone()[0] or 0
        
        cursor.execute("""
            SELECT old_value, new_value
            FROM activity_log
            WHERE action_type = 'progress'
            AND timestamp >= ? AND timestamp <= ?
        """, (month_start.isoformat(), now.isoformat()))
        
        chapters_read_month = 0
        for old_str, new_str in cursor.fetchall():
            try:
                old_val = json.loads(old_str) if old_str else {}
                new_val = json.loads(new_str) if new_str else {}
                old_ch = old_val.get('chapter', -1)
                new_ch = new_val.get('chapter', -1)
                
                # CHANGED: Handle both forward and backward progress
                if new_ch >= 0:
                    if old_ch == -1:
                        chapters_read_month += float(new_ch)
                    elif old_ch >= 0:
                        chapters_read_month += float(new_ch) - float(old_ch)
                elif new_ch == -1 and old_ch >= 0:
                    # ADDED: Reset to "Not started" - subtract all read chapters
                    chapters_read_month -= float(old_ch)
            except:
                continue
        
        chapters_read_month = round(chapters_read_month, 1)
        
        cursor.execute("""
            INSERT OR REPLACE INTO stats_history 
            (period_type, period_start, period_end, series_added, chapters_read)
            VALUES (?, ?, ?, ?, ?)
        """, ('month', month_str, month_end.date().isoformat(), series_added_month, chapters_read_month))
        
        # === THIS YEAR ===
        year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        year_end = year_start.replace(year=year_start.year + 1) - timedelta(seconds=1)
        year_str = year_start.date().isoformat()
        
        cursor.execute("""
            SELECT COUNT(*) FROM series
            WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) <= DATETIME(?)
        """, (year_start.isoformat(), now.isoformat()))
        series_added_year = cursor.fetchone()[0] or 0
        
        cursor.execute("""
            SELECT old_value, new_value
            FROM activity_log
            WHERE action_type = 'progress'
            AND timestamp >= ? AND timestamp <= ?
        """, (year_start.isoformat(), now.isoformat()))
        
        chapters_read_year = 0
        for old_str, new_str in cursor.fetchall():
            try:
                old_val = json.loads(old_str) if old_str else {}
                new_val = json.loads(new_str) if new_str else {}
                old_ch = old_val.get('chapter', -1)
                new_ch = new_val.get('chapter', -1)
                
                # CHANGED: Handle both forward and backward progress
                if new_ch >= 0:
                    if old_ch == -1:
                        chapters_read_year += float(new_ch)
                    elif old_ch >= 0:
                        chapters_read_year += float(new_ch) - float(old_ch)
                elif new_ch == -1 and old_ch >= 0:
                    # ADDED: Reset to "Not started" - subtract all read chapters
                    chapters_read_year -= float(old_ch)
            except:
                continue
        
        chapters_read_year = round(chapters_read_year, 1)
        
        cursor.execute("""
            INSERT OR REPLACE INTO stats_history 
            (period_type, period_start, period_end, series_added, chapters_read)
            VALUES (?, ?, ?, ?, ?)
        """, ('year', year_str, year_end.date().isoformat(), series_added_year, chapters_read_year))
        
        print(f"[Stats] Updated current period stats: Today={series_added_today}s/{chapters_read_today}ch, Week={series_added_week}s/{chapters_read_week}ch, Month={series_added_month}s/{chapters_read_month}ch, Year={series_added_year}s/{chapters_read_year}ch")
        
    except Exception as e:
        print(f"[Stats] Failed to update current period stats: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if conn is not None:
            try:
                release_db(conn)
            except Exception as release_err:
                print(f"[Stats] Failed to release DB in update_current_period_stats: {release_err}")

def adjust_stats_for_deletion(created_at_str):
    """
    Adjust stats when a series is deleted.
    Decrements series_added for the period when it was originally added.
    """
    from datetime import datetime, timezone
    import json
    
    conn = None
    try:
        # Parse created_at to determine which period to adjust. Series added
        # via add_series() get created_at from SQLite's DEFAULT
        # CURRENT_TIMESTAMP, which is UTC but stored with no timezone marker
        # (e.g. "2026-08-28 15:30:00", not ISO 8601) -- fromisoformat() on
        # that produces a naive datetime, which can't be compared against
        # the timezone-aware "now" below without attaching UTC explicitly.
        created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Determine if deletion affects current periods
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Adjust TODAY if series was added today
        if created_at >= today_start:
            today_str = today_start.date().isoformat()
            cursor.execute("""
                UPDATE stats_history 
                SET series_added = MAX(0, series_added - 1)
                WHERE period_type = 'day' AND period_start = ?
            """, (today_str,))
        
        # Adjust WEEK if series was added this week
        if created_at >= week_start:
            week_str = week_start.date().isoformat()
            cursor.execute("""
                UPDATE stats_history 
                SET series_added = MAX(0, series_added - 1)
                WHERE period_type = 'week' AND period_start = ?
            """, (week_str,))
        
        # Adjust MONTH if series was added this month
        if created_at >= month_start:
            month_str = month_start.date().isoformat()
            cursor.execute("""
                UPDATE stats_history 
                SET series_added = MAX(0, series_added - 1)
                WHERE period_type = 'month' AND period_start = ?
            """, (month_str,))
        
        # Adjust YEAR if series was added this year
        if created_at >= year_start:
            year_str = year_start.date().isoformat()
            cursor.execute("""
                UPDATE stats_history 
                SET series_added = MAX(0, series_added - 1)
                WHERE period_type = 'year' AND period_start = ?
            """, (year_str,))
        
        print(f"[Stats] Adjusted stats for deleted series (created: {created_at.date()})")
        
    except Exception as e:
        print(f"[Stats] Failed to adjust stats for deletion: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if conn is not None:
            try:
                release_db(conn)
            except Exception as release_err:
                print(f"[Stats] Failed to release DB in adjust_stats_for_deletion: {release_err}")

def cleanup_old_stats(keep_days=90, keep_years=True):
    """
    Clean up old statistics to prevent database bloat.
    
    Args:
        keep_days: How many days of daily stats to keep (default: 90)
        keep_years: If True, never delete yearly stats (default: True)
    """
    from datetime import datetime, timezone, timedelta
    
    conn = None
    try:
        now = datetime.now(timezone.utc)
        cutoff_date = (now - timedelta(days=keep_days)).date().isoformat()
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Delete old daily stats (keep last 90 days)
        cursor.execute("""
            DELETE FROM stats_history 
            WHERE period_type = 'day' 
            AND period_start < ?
        """, (cutoff_date,))
        deleted_days = cursor.rowcount
        
        # Delete old weekly stats (keep last ~1 year = 52 weeks)
        week_cutoff = (now - timedelta(days=365)).date().isoformat()
        cursor.execute("""
            DELETE FROM stats_history 
            WHERE period_type = 'week' 
            AND period_start < ?
        """, (week_cutoff,))
        deleted_weeks = cursor.rowcount
        
        # Delete old monthly stats (keep last 2 years = 24 months)
        month_cutoff = (now - timedelta(days=730)).date().isoformat()
        cursor.execute("""
            DELETE FROM stats_history 
            WHERE period_type = 'month' 
            AND period_start < ?
        """, (month_cutoff,))
        deleted_months = cursor.rowcount
        
        # NEVER delete yearly stats (keep forever)
        
        release_db(conn)
        print(f"[Stats Cleanup] Removed {deleted_days} old daily stats, {deleted_weeks} old weekly stats, {deleted_months} old monthly stats")
        
    except Exception as e:
        print(f"[Stats Cleanup] Failed: {e}")
        if conn:
            try:
                release_db(conn)
            except:
                pass