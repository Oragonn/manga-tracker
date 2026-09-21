import os
import re
import shutil
import sqlite3
import time
import gzip
import uuid
import zlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


class BackupImportError(Exception):
    """An uploaded file can't be used as a backup. The message is meant to be
    shown to the user; `status` is the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class BackupManager:
    # Uploaded backups: the largest upload, and the largest database it may
    # unpack to, in bytes. Real backups are a few MB.
    MAX_IMPORT_BYTES = 1024 * 1024 * 1024

    _GZIP_MAGIC = b'\x1f\x8b'
    _SQLITE_MAGIC = b'SQLite format 3\x00'
    # Enough to tell a Manga Tracker database from any other SQLite file.
    _IMPORT_REQUIRED_TABLES = ('series', 'series_sources', 'chapters')
    _BACKUP_NAME = re.compile(r'tracker_backup_\d{8}_\d{6}\.db\.gz')

    def __init__(self, db_path="data/tracker.db", backup_dir="backups",
                 backup_interval_hours=1, retention_days=7, max_size_mb=2048):
        """
        Initialize backup manager.

        Args:
            db_path: Path to SQLite database file
            backup_dir: Directory to store backups
            backup_interval_hours: Hours between backups (default: 1)
            retention_days: Days to keep backups (default: 7)
            max_size_mb: Total size cap in MB; oldest backups are deleted
                         first once this is exceeded (default: 2048 / 2 GB)
        """
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.backup_interval = backup_interval_hours * 3600  # Convert to seconds
        self.retention_seconds = retention_days * 86400
        self.max_size_bytes = max_size_mb * 1024 * 1024

        self.active = True
        self.backup_thread = None
        self.cleanup_thread = None

        # Ensure backup directory exists
        os.makedirs(self.backup_dir, exist_ok=True)

        print(f"[Backup Manager] Initialized:")
        print(f"  - Database: {self.db_path}")
        print(f"  - Backup dir: {self.backup_dir}")
        print(f"  - Interval: {backup_interval_hours}h")
        print(f"  - Retention: {retention_days}d")
        print(f"  - Size cap: {max_size_mb} MB")
    
    def _get_backup_filename(self):
        """Generate timestamped backup filename."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"tracker_backup_{timestamp}.db.gz"
    
    def create_backup(self):
        """
        Create a compressed backup of the database.
        Returns True if successful, False otherwise.
        """
        try:
            # Check if database exists
            if not os.path.exists(self.db_path):
                print(f"[Backup] Database not found: {self.db_path}")
                return False
            
            # Generate backup filename
            backup_filename = self._get_backup_filename()
            backup_path = os.path.join(self.backup_dir, backup_filename)
            temp_path = backup_path + ".tmp"
            
            # Copy database file (handles WAL mode correctly)
            # Using SQLite backup API would be better, but requires DB connection
            shutil.copy2(self.db_path, temp_path.replace('.gz', ''))
            
            # Compress the backup
            with open(temp_path.replace('.gz', ''), 'rb') as f_in:
                with gzip.open(temp_path, 'wb', compresslevel=6) as f_out:
                    shutil.copyfileobj(f_in, f_out)
            
            # Remove uncompressed temp file
            os.remove(temp_path.replace('.gz', ''))
            
            # Move to final location
            shutil.move(temp_path, backup_path)
            
            # Get file size for logging
            size_mb = os.path.getsize(backup_path) / (1024 * 1024)
            print(f"[Backup] Created: {backup_filename} ({size_mb:.2f} MB)")

            self.enforce_size_limit()

            return True
            
        except Exception as e:
            print(f"[Backup] Failed to create backup: {e}")
            # Clean up temp files
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                if os.path.exists(temp_path.replace('.gz', '')):
                    os.remove(temp_path.replace('.gz', ''))
            except:
                pass
            return False
    
    def cleanup_old_backups(self):
        """Delete backups older than retention period."""
        try:
            now = time.time()
            cutoff_time = now - self.retention_seconds
            
            deleted_count = 0
            freed_space = 0
            
            # List all backup files (including safety backups)
            for filename in os.listdir(self.backup_dir):
                # Match both regular backups AND safety backups
                if not (filename.startswith("tracker_backup_") or filename.startswith("safety_before_restore_")):
                    continue
                if not filename.endswith(".db.gz"):
                    continue
                
                filepath = os.path.join(self.backup_dir, filename)
                file_mtime = os.path.getmtime(filepath)
                
                if file_mtime < cutoff_time:
                    file_size = os.path.getsize(filepath)
                    os.remove(filepath)
                    deleted_count += 1
                    freed_space += file_size
                    print(f"[Backup] Deleted old backup: {filename}")
            
            if deleted_count > 0:
                freed_mb = freed_space / (1024 * 1024)
                print(f"[Backup] Cleanup: Removed {deleted_count} backups, freed {freed_mb:.2f} MB")
            
        except Exception as e:
            print(f"[Backup] Cleanup failed: {e}")

    def enforce_size_limit(self):
        """Delete the oldest backups (regular + safety) until total size is
        back under max_size_bytes. Always keeps at least one backup."""
        try:
            entries = []
            for filename in os.listdir(self.backup_dir):
                if not (filename.startswith("tracker_backup_") or filename.startswith("safety_before_restore_")):
                    continue
                if not filename.endswith(".db.gz"):
                    continue

                filepath = os.path.join(self.backup_dir, filename)
                entries.append((os.path.getmtime(filepath), filename, filepath, os.path.getsize(filepath)))

            # Oldest first
            entries.sort(key=lambda e: e[0])
            total_size = sum(e[3] for e in entries)

            deleted_count = 0
            freed_space = 0
            i = 0
            while total_size > self.max_size_bytes and (len(entries) - i) > 1:
                _, filename, filepath, size = entries[i]
                os.remove(filepath)
                total_size -= size
                freed_space += size
                deleted_count += 1
                i += 1
                print(f"[Backup] Deleted oldest backup to stay under size cap: {filename}")

            if deleted_count > 0:
                freed_mb = freed_space / (1024 * 1024)
                print(f"[Backup] Size cap cleanup: Removed {deleted_count} backups, freed {freed_mb:.2f} MB")

        except Exception as e:
            print(f"[Backup] Size cap cleanup failed: {e}")

    def get_backup_stats(self):
        """Get statistics about current backups."""
        try:
            backups = []
            total_size = 0
            
            for filename in sorted(os.listdir(self.backup_dir), reverse=True):
                if not filename.startswith("tracker_backup_") or not filename.endswith(".db.gz"):
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
            print(f"[Backup] Failed to get stats: {e}")
            return {'count': 0, 'total_size_mb': 0, 'backups': []}
    
    def _save_upload(self, stream, path):
        """Write an uploaded stream to `path`, refusing an empty or oversized one."""
        total = 0
        with open(path, 'wb') as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.MAX_IMPORT_BYTES:
                    raise BackupImportError("That file is too large to be a backup.", 413)
                out.write(chunk)
        if total == 0:
            raise BackupImportError("The file is empty.")

    def _gunzip_capped(self, src, dst):
        """Decompress `src` into `dst`, refusing one that unpacks past the size
        cap (a small file can decompress to something enormous)."""
        try:
            with gzip.open(src, 'rb') as f_in, open(dst, 'wb') as f_out:
                total = 0
                while True:
                    chunk = f_in.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.MAX_IMPORT_BYTES:
                        raise BackupImportError("That backup is too large once unpacked.", 413)
                    f_out.write(chunk)
        except (OSError, EOFError, zlib.error) as e:
            # BadGzipFile is an OSError; EOFError means the file was cut short
            raise BackupImportError("The file is damaged or incomplete (it won't unpack).") from e

    def _check_database(self, path):
        """Confirm `path` is an intact Manga Tracker database and return how
        many series it holds. Opened read-only and immutable, so nothing is
        written next to it."""
        conn = None
        try:
            conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
            if conn.execute("PRAGMA quick_check").fetchone()[0] != 'ok':
                raise BackupImportError("The database inside is damaged (it failed an integrity check).")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            missing = [t for t in self._IMPORT_REQUIRED_TABLES if t not in tables]
            if missing:
                raise BackupImportError(
                    f"That isn't a Manga Tracker database (no '{missing[0]}' table)."
                )
            return conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise BackupImportError("The file isn't a readable database.") from e
        finally:
            if conn is not None:
                conn.close()

    def import_backup(self, stream, original_filename=None):
        """
        Add a backup made elsewhere (a .db.gz downloaded from this or another
        install, or a raw tracker.db) to the backup list, where it can be
        downloaded and restored like any other. Nothing is stored unless the
        file unpacks to an intact Manga Tracker database.

        A file still named like one of ours keeps that name (so downloading a
        backup and uploading it again is a no-op round trip); anything else
        gets a fresh timestamped name.

        Returns {'filename', 'size_mb', 'series_count'}; raises
        BackupImportError for a file that can't be used.
        """
        os.makedirs(self.backup_dir, exist_ok=True)
        token = uuid.uuid4().hex
        upload_path = os.path.join(self.backup_dir, f".upload_{token}.tmp")
        db_path = os.path.join(self.backup_dir, f".upload_{token}.db.tmp")
        gz_path = os.path.join(self.backup_dir, f".upload_{token}.gz.tmp")
        try:
            self._save_upload(stream, upload_path)
            with open(upload_path, 'rb') as f:
                head = f.read(len(self._SQLITE_MAGIC))

            if head.startswith(self._GZIP_MAGIC):
                self._gunzip_capped(upload_path, db_path)
                with open(db_path, 'rb') as f:
                    if f.read(len(self._SQLITE_MAGIC)) != self._SQLITE_MAGIC:
                        raise BackupImportError("The archive doesn't contain a database.")
                ready_gz = upload_path  # already a .gz: keep exactly what was uploaded
            elif head == self._SQLITE_MAGIC:
                os.replace(upload_path, db_path)
                ready_gz = None
            else:
                raise BackupImportError(
                    "That isn't a backup - upload a .db.gz backup or a tracker.db database."
                )

            series_count = self._check_database(db_path)

            name = os.path.basename(original_filename or '')
            if self._BACKUP_NAME.fullmatch(name):
                final_name = name
                if os.path.exists(os.path.join(self.backup_dir, final_name)):
                    raise BackupImportError(f"A backup named {final_name} is already in the list.", 409)
            else:
                stamp = datetime.now(timezone.utc)
                while os.path.exists(os.path.join(self.backup_dir, f"tracker_backup_{stamp:%Y%m%d_%H%M%S}.db.gz")):
                    stamp += timedelta(seconds=1)
                final_name = f"tracker_backup_{stamp:%Y%m%d_%H%M%S}.db.gz"

            if ready_gz is None:
                with open(db_path, 'rb') as f_in, gzip.open(gz_path, 'wb', compresslevel=6) as f_out:
                    shutil.copyfileobj(f_in, f_out)
                ready_gz = gz_path

            final_path = os.path.join(self.backup_dir, final_name)
            os.replace(ready_gz, final_path)
            # Retention counts from when the file arrived here, so an old
            # backup isn't swept away by the next cleanup.
            os.utime(final_path)

            size_mb = os.path.getsize(final_path) / (1024 * 1024)
            print(f"[Backup] Imported: {final_name} ({size_mb:.2f} MB, {series_count} series)")
            self.enforce_size_limit()
            return {'filename': final_name, 'size_mb': size_mb, 'series_count': series_count}
        finally:
            for leftover in (upload_path, db_path, gz_path):
                try:
                    if os.path.exists(leftover):
                        os.remove(leftover)
                except OSError:
                    pass

    def restore_backup(self, backup_filename):
        """
        Restore database from a backup file.
        
        WARNING: This will overwrite the current database!
        
        Args:
            backup_filename: Name of backup file to restore
        
        Returns:
            True if successful, False otherwise
        """
        try:
            backup_path = os.path.join(self.backup_dir, backup_filename)
            
            if not os.path.exists(backup_path):
                print(f"[Backup] Backup file not found: {backup_filename}")
                return False
            
            # Create safety backup of current database IN THE BACKUPS DIRECTORY
            timestamp = int(time.time())
            safety_filename = f"safety_before_restore_{timestamp}.db.gz"
            safety_backup_path = os.path.join(self.backup_dir, safety_filename)
            
            if os.path.exists(self.db_path):
                # Compress current DB and save to backups/
                temp_db_copy = f"{self.db_path}.temp_{timestamp}"
                shutil.copy2(self.db_path, temp_db_copy)
                
                with open(temp_db_copy, 'rb') as f_in:
                    with gzip.open(safety_backup_path, 'wb', compresslevel=6) as f_out:
                        shutil.copyfileobj(f_in, f_out)
                
                # Remove temp uncompressed copy
                os.remove(temp_db_copy)
                
                print(f"[Backup] Created safety backup: {safety_filename}")
            
            # Decompress and restore
            temp_restore = f"{self.db_path}.restoring"
            with gzip.open(backup_path, 'rb') as f_in:
                with open(temp_restore, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            
            # Replace current database
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            shutil.move(temp_restore, self.db_path)
            
            print(f"[Backup] Successfully restored from: {backup_filename}")
            print(f"[Backup] Safety backup available at: backups/{safety_filename}")

            self.enforce_size_limit()

            return True
            
        except Exception as e:
            print(f"[Backup] Restore failed: {e}")
            # Try to clean up
            try:
                if os.path.exists(temp_restore):
                    os.remove(temp_restore)
            except:
                pass
            return False
        
    def _backup_loop(self):
        """Background thread that creates backups periodically."""
        print(f"[Backup] Backup thread started")
        
        # Create initial backup on startup (after 60s delay to avoid startup load)
        time.sleep(60)
        self.create_backup()
        
        while self.active:
            try:
                time.sleep(self.backup_interval)
                if self.active:  # Check again after sleep
                    self.create_backup()
            except Exception as e:
                print(f"[Backup] Loop error: {e}")
                time.sleep(60)  # Wait a bit before retrying
    
    def _cleanup_loop(self):
        """Background thread that cleans up old backups."""
        print(f"[Backup] Cleanup thread started")
        
        while self.active:
            try:
                # Run cleanup every 6 hours
                time.sleep(21600)
                if self.active:
                    self.cleanup_old_backups()
                    self.enforce_size_limit()
            except Exception as e:
                print(f"[Backup] Cleanup loop error: {e}")
                time.sleep(3600)
    
    def start(self):
        """Start backup and cleanup background threads."""
        if self.backup_thread is None or not self.backup_thread.is_alive():
            self.active = True
            self.backup_thread = threading.Thread(target=self._backup_loop, daemon=True)
            self.backup_thread.start()
        
        if self.cleanup_thread is None or not self.cleanup_thread.is_alive():
            self.cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            self.cleanup_thread.start()
        
        print("[Backup Manager] Background threads started")
    
    def stop(self):
        """Stop background threads."""
        self.active = False
        print("[Backup Manager] Stopping...")
