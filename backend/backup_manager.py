import os
import shutil
import time
import gzip
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

class BackupManager:
    def __init__(self, db_path="data/tracker.db", backup_dir="backups", 
                 backup_interval_hours=1, retention_days=7):
        """
        Initialize backup manager.
        
        Args:
            db_path: Path to SQLite database file
            backup_dir: Directory to store backups
            backup_interval_hours: Hours between backups (default: 1)
            retention_days: Days to keep backups (default: 7)
        """
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.backup_interval = backup_interval_hours * 3600  # Convert to seconds
        self.retention_seconds = retention_days * 86400
        
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
            
            # List all backup files
            for filename in os.listdir(self.backup_dir):
                if not filename.startswith("tracker_backup_") or not filename.endswith(".db.gz"):
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
            
            # Create safety backup of current database
            safety_backup = f"{self.db_path}.before_restore_{int(time.time())}"
            if os.path.exists(self.db_path):
                shutil.copy2(self.db_path, safety_backup)
                print(f"[Backup] Created safety backup: {safety_backup}")
            
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
            print(f"[Backup] Safety backup available at: {safety_backup}")
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
