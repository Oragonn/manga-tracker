import threading
import time
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from .database import get_db, release_db
from .trackers.mangadex import extract_manga_id, get_latest_chapters
from .trackers.kagane import extract_series_id, get_series_info
from .backup_manager import BackupManager

class MangaScheduler:
    def __init__(self):
        self.active = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.cleanup_thread = threading.Thread(target=self._cleanup_logs, daemon=True)
        
        # ✅ FIX: Use absolute paths
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "data", "tracker.db")
        backup_dir = os.path.join(project_root, "backups")
        
        self.backup_manager = BackupManager(
            db_path=db_path,
            backup_dir=backup_dir,
            backup_interval_hours=1,  # Backup every hour
            retention_days=7          # Keep for 7 days
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

                for (sid, status, last_check) in series_list:
                    if not self.active:
                        break
                    if last_check:
                        last = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                        next_check = last + timedelta(seconds=self.get_check_interval(status))
                        if now < next_check:
                            continue
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

    def scan_series(self, series_id):
        try:
            from .database import get_series_sources
            
            # Get all sources for this series
            sources = get_series_sources(series_id)
            
            if not sources:
                print(f"[Scheduler] No sources found for series {series_id}")
                return
            
            all_chapters = []
            
            # Fetch chapters from each source
            for source in sources:
                source_url = source['source_url']
                source_type = source['source_type']
                
                chapters = None
                
                if source_type == 'mangadex':
                    manga_id = extract_manga_id(source_url)
                    if manga_id:
                        chapters = get_latest_chapters(manga_id, limit=100)
                elif source_type == 'kagane':
                    kagane_id = extract_series_id(source_url)
                    if kagane_id:
                        kagane_info = get_series_info(kagane_id)
                        if kagane_info:
                            chapters = kagane_info['chapters']
                
                if chapters:
                    # Tag chapters with source info
                    for ch in chapters:
                        ch['source_id'] = source['id']
                        ch['source_type'] = source_type
                        ch['source_url'] = source_url
                    all_chapters.extend(chapters)
            
            # Merge chapters (deduplicate by chapter_number, keep most recent)
            merged_chapters = {}
            for ch in all_chapters:
                ch_num = ch['chapter_number']
                if ch_num not in merged_chapters:
                    merged_chapters[ch_num] = ch
                else:
                    # Keep the one with the most recent release date
                    existing = merged_chapters[ch_num]
                    if ch.get('release_date', '') > existing.get('release_date', ''):
                        merged_chapters[ch_num] = ch
            
            # Convert back to list and sort
            final_chapters = list(merged_chapters.values())
            final_chapters.sort(key=lambda x: x['chapter_number'])
            
            # Save to database
            if final_chapters:
                conn = get_db()
                try:
                    self._process_chapters(series_id, final_chapters, conn)
                finally:
                    release_db(conn)
            else:
                conn = get_db()
                try:
                    self._update_last_check(series_id, conn)
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

    def stop(self):
        self.active = False