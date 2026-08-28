import os
import csv
import time
import threading
from datetime import datetime, timezone

from .database import get_all_series_for_backup


class SeriesBackupManager:
    """Periodic CSV export of the series list, shaped like a Kenmei export
    (title/status/last_chapter_read/tracked_site) so it can be round-tripped
    through the existing /import-kenmei page if the database itself is lost.
    """

    def __init__(self, backup_dir="backups/series_csv",
                 backup_interval_hours=24, retention_days=30):
        self.backup_dir = backup_dir
        self.backup_interval = backup_interval_hours * 3600
        self.retention_seconds = retention_days * 86400

        self.active = True
        self.backup_thread = None

        os.makedirs(self.backup_dir, exist_ok=True)

        print(f"[Series Backup] Initialized:")
        print(f"  - Backup dir: {self.backup_dir}")
        print(f"  - Interval: {backup_interval_hours}h")
        print(f"  - Retention: {retention_days}d")

    def _get_backup_filename(self):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"series_backup_{timestamp}.csv"

    def create_backup(self):
        """Write a CSV snapshot of every series. Returns True if successful."""
        try:
            series = get_all_series_for_backup()

            filename = self._get_backup_filename()
            backup_path = os.path.join(self.backup_dir, filename)
            temp_path = backup_path + ".tmp"

            with open(temp_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['title', 'status', 'last_chapter_read', 'tracked_site', 'url'])
                for row in series:
                    chapter = row['current_chapter']
                    chapter_out = '' if chapter is None or chapter < 0 else chapter
                    writer.writerow([
                        row['title'],
                        row['status'],
                        chapter_out,
                        row['source_type'] or '',
                        row['source_url'] or '',
                    ])

            os.replace(temp_path, backup_path)

            size_kb = os.path.getsize(backup_path) / 1024
            print(f"[Series Backup] Created: {filename} ({len(series)} series, {size_kb:.1f} KB)")
            return True

        except Exception as e:
            print(f"[Series Backup] Failed to create backup: {e}")
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            return False

    def cleanup_old_backups(self):
        """Delete backups older than the retention period."""
        try:
            now = time.time()
            cutoff_time = now - self.retention_seconds

            deleted_count = 0
            for filename in os.listdir(self.backup_dir):
                if not (filename.startswith("series_backup_") and filename.endswith(".csv")):
                    continue

                filepath = os.path.join(self.backup_dir, filename)
                if os.path.getmtime(filepath) < cutoff_time:
                    os.remove(filepath)
                    deleted_count += 1
                    print(f"[Series Backup] Deleted old backup: {filename}")

            if deleted_count > 0:
                print(f"[Series Backup] Cleanup: Removed {deleted_count} backups")

        except Exception as e:
            print(f"[Series Backup] Cleanup failed: {e}")

    def get_backup_stats(self):
        """Get statistics about current series backups."""
        try:
            backups = []
            total_size = 0

            for filename in sorted(os.listdir(self.backup_dir), reverse=True):
                if not (filename.startswith("series_backup_") and filename.endswith(".csv")):
                    continue

                filepath = os.path.join(self.backup_dir, filename)
                file_size = os.path.getsize(filepath)
                file_mtime = os.path.getmtime(filepath)

                backups.append({
                    'filename': filename,
                    'size_mb': file_size / (1024 * 1024),
                    'created': datetime.fromtimestamp(file_mtime, tz=timezone.utc),
                    'age_hours': (time.time() - file_mtime) / 3600
                })
                total_size += file_size

            return {
                'count': len(backups),
                'total_size_mb': total_size / (1024 * 1024),
                'backups': backups
            }
        except Exception as e:
            print(f"[Series Backup] Failed to get stats: {e}")
            return {'count': 0, 'total_size_mb': 0, 'backups': []}

    def _backup_loop(self):
        print(f"[Series Backup] Backup thread started")

        time.sleep(90)
        self.create_backup()
        self.cleanup_old_backups()

        while self.active:
            try:
                time.sleep(self.backup_interval)
                if self.active:
                    self.create_backup()
                    self.cleanup_old_backups()
            except Exception as e:
                print(f"[Series Backup] Loop error: {e}")
                time.sleep(60)

    def start(self):
        if self.backup_thread is None or not self.backup_thread.is_alive():
            self.active = True
            self.backup_thread = threading.Thread(target=self._backup_loop, daemon=True)
            self.backup_thread.start()
        print("[Series Backup Manager] Background thread started")

    def stop(self):
        self.active = False
        print("[Series Backup Manager] Stopping...")
