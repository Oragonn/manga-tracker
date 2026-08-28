import threading
import time
import sqlite3
import os
import concurrent.futures
from datetime import datetime, timezone, timedelta
from .database import get_db, release_db
from .trackers.mangadex import extract_manga_id, get_latest_chapters
from .trackers.kagane import extract_series_id, get_series_info
from .trackers import atsu as atsu_tracker
from .trackers import asura as asura_tracker
from .backup_manager import BackupManager
from .series_backup_manager import SeriesBackupManager

class MangaScheduler:
    def __init__(self):
        self.active = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.cleanup_thread = threading.Thread(target=self._cleanup_logs, daemon=True)

        # ✅ FIX: Use absolute paths
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "data", "tracker.db")
        backups_root = os.path.join(project_root, "backups")

        self.backup_manager = BackupManager(
            db_path=db_path,
            backup_dir=os.path.join(backups_root, "database"),
            backup_interval_hours=1,  # Backup every hour
            retention_days=7,         # Keep for 7 days
            max_size_mb=2048          # ...but never let total size pass 2 GB either
        )

        self.series_backup_manager = SeriesBackupManager(
            backup_dir=os.path.join(backups_root, "series_csv"),
            backup_interval_hours=24,  # Kenmei-style CSV snapshot once a day
            retention_days=30
        )

    def _cleanup_logs(self):
        """Background thread to cleanup old activity logs every 24 hours."""
        while self.active:
            try:
                from .activity_logger import cleanup_old_logs
                cleanup_old_logs()
                # Sleep for 24 hours
                time.sleep(86400)
            except Exception as e:
                print(f"[Cleanup] Error: {e}")
                time.sleep(3600)  # Retry in 1 hour on error

    def get_check_interval(self, status):
        intervals = {
            'reading': 1800,
            'plan_to_read': 10800,
            'on_hold': 86400,
            'dropped': 604800,
            'completed': 604800
        }
        return intervals.get(status, 604800)

    # Lower number = scanned first. A freshly bulk-imported library (or a
    # restart after being offline a while) can put hundreds/thousands of
    # series in "due" state simultaneously (every series starts with
    # last_check=NULL, which is always due); without this, the time-sensitive
    # "reading" tier would just wait behind whatever order the DB happens to
    # return, potentially for a long time.
    STATUS_SCAN_PRIORITY = {
        'reading': 0,
        'on_hold': 1,
        'plan_to_read': 2,
        'dropped': 3,
        'completed': 3,
    }

    # Cap how long a single tick spends scanning, so a large backlog spreads
    # across multiple ticks (60s apart) instead of one long uninterrupted
    # burst hammering external APIs. Combined with the priority sort above,
    # "reading" series always get first crack at this budget every tick.
    MAX_SCAN_SECONDS_PER_TICK = 30

    def _run(self):
        from .database import get_db, release_db  # Ensure consistent import
        while self.active:
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("SELECT id, status, last_check FROM series")
                series_list = cursor.fetchall()
                release_db(conn)

                now = datetime.now(timezone.utc)

                due = []
                for (sid, status, last_check) in series_list:
                    if last_check:
                        last = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                        next_check = last + timedelta(seconds=self.get_check_interval(status))
                        if now < next_check:
                            continue
                    due.append((sid, status, last_check))

                # Reading first, then most-overdue within the same status
                # (NULL last_check -- never checked -- sorts as oldest).
                due.sort(key=lambda row: (
                    self.STATUS_SCAN_PRIORITY.get(row[1], 4),
                    row[2] or ''
                ))

                tick_start = time.time()
                for i, (sid, status, last_check) in enumerate(due):
                    if not self.active:
                        break
                    if time.time() - tick_start > self.MAX_SCAN_SECONDS_PER_TICK:
                        print(f"[Scheduler] Tick budget reached, deferring "
                              f"{len(due) - i} due series to next tick")
                        break
                    self.scan_series(sid)
                    time.sleep(0.4)

                time.sleep(60)
            except Exception as e:
                print(f"[Scheduler] Background error: {e}")
                time.sleep(60)

    def _update_last_check(self, series_id, conn):
        timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        cursor = conn.cursor()
        cursor.execute("UPDATE series SET last_check = ? WHERE id = ?", (timestamp, series_id))

    def _process_chapters(self, series_id, chapters, conn):
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
        for ch in chapters:
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
        if chapters:
            latest_ch = max(ch['chapter_number'] for ch in chapters)
            latest_release = max(
                (ch['release_date'] for ch in chapters if ch['release_date']),
                default=''
            )
            cursor.execute("""
                UPDATE series
                SET latest_chapter = ?, latest_release = ?, total_chapters = ?, last_check = ?
                WHERE id = ?
            """, (
                latest_ch,
                latest_release,
                len(chapters),
                datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                series_id
            ))
        else:
            self._update_last_check(series_id, conn)
        conn.commit()

    def _fetch_source_chapters(self, source):
        """Fetch the chapter list for a single source. Runs on a worker
        thread from scan_series's ThreadPoolExecutor -- raises on failure
        so the caller's future.result() surfaces the error per-source."""
        source_url = source['source_url']
        source_type = source['source_type']

        print(f"[Scheduler] Fetching from {source_type}: {source_url}")

        if source_type == 'mangadex':
            manga_id = extract_manga_id(source_url)
            return get_latest_chapters(manga_id, limit=100) if manga_id else None
        elif source_type == 'kagane':
            kagane_id = extract_series_id(source_url)
            if not kagane_id:
                return None
            kagane_info = get_series_info(kagane_id)
            return kagane_info['chapters'] if kagane_info else None
        elif source_type == 'atsu':
            atsu_id = atsu_tracker.extract_series_id(source_url)
            if not atsu_id:
                return None
            atsu_info = atsu_tracker.get_series_info(atsu_id)
            return atsu_info['chapters'] if atsu_info else None
        elif source_type == 'asura':
            asura_id = asura_tracker.extract_series_id(source_url)
            if not asura_id:
                return None
            asura_info = asura_tracker.get_series_info(asura_id)
            return asura_info['chapters'] if asura_info else None
        return None

    def scan_series(self, series_id):
        """
        Scan all sources for a series and merge chapters.
        Fix for Bug #2: Now properly merges chapters from multiple sources.
        """
        try:
            from .database import get_series_sources, get_db, release_db
            
            # Get all sources for this series
            sources = get_series_sources(series_id)
            
            if not sources:
                print(f"[Scheduler] No sources found for series {series_id}")
                # *** FIX: Try to fetch from legacy source_url column ***
                conn_legacy = get_db()
                cursor_legacy = conn_legacy.cursor()
                cursor_legacy.execute("SELECT source_url FROM series WHERE id = ?", (series_id,))
                row = cursor_legacy.fetchone()
                release_db(conn_legacy)
                
                if row and row[0]:
                    print(f"[Scheduler] Found legacy source_url, creating source entry...")
                    # Create missing source entry
                    from .database import add_source_to_series
                    source_url = row[0]
                    source_type = 'mangadex' if 'mangadex.org' in source_url else 'kagane' if ('kagane.org' in source_url or 'kagane.to' in source_url) else 'atsu' if 'atsu.moe' in source_url else 'asura' if 'asurascans.com' in source_url else 'unknown'
                    add_source_to_series(series_id, source_url, source_type, is_primary=True)
                    # Retry getting sources
                    sources = get_series_sources(series_id)
                
                if not sources:
                    return
            
            all_chapters = []
            successful_sources = 0

            # Fetch chapters from all sources concurrently instead of one at a
            # time -- each source is an independent network call, and the
            # merge step below is already order-independent (it compares
            # release dates rather than relying on iteration order).
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(sources))) as executor:
                future_to_source = {
                    executor.submit(self._fetch_source_chapters, source): source
                    for source in sources
                }
                for future in concurrent.futures.as_completed(future_to_source):
                    source = future_to_source[future]
                    source_type = source['source_type']
                    try:
                        chapters = future.result()
                    except Exception as source_error:
                        print(f"[Scheduler] Error fetching from {source_type}: {source_error}")
                        continue

                    if chapters:
                        # Tag chapters with source info
                        for ch in chapters:
                            ch['source_id'] = source['id']
                            ch['source_type'] = source_type
                            ch['source_url'] = source['source_url']
                        all_chapters.extend(chapters)
                        successful_sources += 1
                        print(f"[Scheduler] Got {len(chapters)} chapters from {source_type}")
                    else:
                        print(f"[Scheduler] No chapters from {source_type}")

            print(f"[Scheduler] Total raw chapters: {len(all_chapters)} from {successful_sources} sources")
            
            # *** FIX: Improved chapter merging logic ***
            if not all_chapters:
                # No chapters found, just update last_check
                conn = get_db()
                try:
                    self._update_last_check(series_id, conn)
                finally:
                    release_db(conn)
                return
            
            # Merge chapters (deduplicate by chapter_number, keep most recent)
            merged_chapters = {}
            for ch in all_chapters:
                ch_num = ch['chapter_number']
                
                if ch_num not in merged_chapters:
                    merged_chapters[ch_num] = ch
                else:
                    # Keep the one with the most recent release date
                    existing = merged_chapters[ch_num]
                    ch_date = ch.get('release_date') or ''
                    existing_date = existing.get('release_date') or ''
                    
                    # Safe comparison: treat None/empty as oldest
                    if ch_date and existing_date:
                        if ch_date > existing_date:
                            print(f"[Scheduler] Ch.{ch_num}: Using {ch['source_type']} (newer: {ch_date} vs {existing_date})")
                            merged_chapters[ch_num] = ch
                    elif ch_date and not existing_date:
                        # New chapter has date, existing doesn't -> prefer new
                        merged_chapters[ch_num] = ch
                    # else: keep existing (either both have no date, or existing has date and new doesn't)
            
            # Convert back to list and sort
            final_chapters = list(merged_chapters.values())
            final_chapters.sort(key=lambda x: x['chapter_number'])
            
            print(f"[Scheduler] Final merged chapters: {len(final_chapters)}")
            
            # Save to database
            conn = get_db()
            try:
                self._process_chapters(series_id, final_chapters, conn)
                print(f"[Scheduler] Successfully saved {len(final_chapters)} chapters for series {series_id}")
            finally:
                release_db(conn)
                        
        except Exception as e:
            from .error_logger import log_error
            try:
                conn_err = get_db()
                cursor_err = conn_err.cursor()
                cursor_err.execute("SELECT title FROM series WHERE id = ?", (series_id,))
                row_err = cursor_err.fetchone()
                title = row_err[0] if row_err else f"Series #{series_id}"
                release_db(conn_err)
            except:
                title = f"Series #{series_id}"
            log_error(
                source_url=f"series:{series_id}",
                error_message=str(e),
                series_title=title
            )
            print(f"[Scheduler] Scan error for {series_id}: {e}")


    def start_scanning(self):
        self.thread.start()
        self.cleanup_thread.start()
        self.backup_manager.start()
        self.series_backup_manager.start()

    def stop(self):
        self.active = False