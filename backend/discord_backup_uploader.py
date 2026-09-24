import os
import time
import threading
from datetime import datetime

import requests

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Paris")
except ImportError:
    LOCAL_TZ = None


class DiscordBackupUploader:
    """Posts the most recent compressed database backup to a Discord webhook
    once a day, as an off-site copy alongside the local backups/ directory.

    Skips entirely if DISCORD_DB_BACKUP_WEBHOOK_URL isn't set in .env.
    Checks the backup's size against Discord's attachment limit before
    posting (DISCORD_DB_BACKUP_MAX_MB, default 10 -- raise it to match your
    server's boost level) so an oversized backup gets a text warning instead
    of a failed upload.
    """

    CHECK_INTERVAL_SECONDS = 3600  # checked hourly; posts once per local day, at/after the target hour

    def __init__(self, backup_manager):
        self.backup_manager = backup_manager
        self.webhook_url = os.environ.get("DISCORD_DB_BACKUP_WEBHOOK_URL")
        self.max_upload_mb = float(os.environ.get("DISCORD_DB_BACKUP_MAX_MB", "10"))
        # Hour of the day (Europe/Paris, 0-23) to post at -- not "on the first
        # check after startup", so restarting mid-day doesn't fire it early.
        self.target_hour = int(os.environ.get("DISCORD_DB_BACKUP_HOUR", "4"))
        self.marker_path = os.path.join(backup_manager.backup_dir, ".last_discord_post")

        self.active = True
        self.thread = None

        if self.webhook_url:
            print(f"[Discord Backup] Initialized: posting the latest DB backup to Discord once a day "
                  f"around {self.target_hour:02d}:00 Europe/Paris (limit {self.max_upload_mb:.0f} MB)")
        else:
            print("[Discord Backup] DISCORD_DB_BACKUP_WEBHOOK_URL not set in .env -- daily Discord upload disabled")

    def _now_local(self):
        return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()

    def _today(self):
        return self._now_local().strftime("%Y-%m-%d")

    def _already_posted_today(self):
        try:
            with open(self.marker_path, 'r') as f:
                return f.read().strip() == self._today()
        except OSError:
            return False

    def _mark_posted_today(self):
        try:
            with open(self.marker_path, 'w') as f:
                f.write(self._today())
        except OSError as e:
            print(f"[Discord Backup] Couldn't record post date: {e}")

    def _latest_backup(self):
        stats = self.backup_manager.get_backup_stats()
        real = [b for b in stats['backups'] if not b['is_safety']]
        if not real:
            return None
        return real[0]  # get_backup_stats() sorts newest first

    def post_latest_backup(self, force=False):
        """Post today's backup to Discord. Returns True if a file was
        uploaded. `force=True` bypasses the once-a-day guard (manual testing)."""
        if not self.webhook_url:
            return False
        if not force:
            if self._already_posted_today():
                return False
            if self._now_local().hour < self.target_hour:
                return False

        latest = self._latest_backup()
        if latest is None:
            print("[Discord Backup] No backup found yet -- will retry")
            return False

        size_mb = latest['size_mb']

        if size_mb > self.max_upload_mb:
            print(f"[Discord Backup] {latest['filename']} is {size_mb:.1f} MB, "
                  f"over the {self.max_upload_mb:.0f} MB limit -- skipping upload")
            try:
                requests.post(
                    self.webhook_url,
                    json={"content": f":warning: Today's database backup is {size_mb:.1f} MB, over the "
                                      f"{self.max_upload_mb:.0f} MB Discord upload limit, so it wasn't posted. "
                                      f"Grab it from the Backups page instead ({latest['filename']})."},
                    timeout=15,
                )
            except Exception as e:
                print(f"[Discord Backup] Failed to post the size-warning message: {e}")
            self._mark_posted_today()  # handled today either way -- don't re-warn hourly
            return False

        path = os.path.join(self.backup_manager.backup_dir, latest['filename'])
        try:
            with open(path, 'rb') as f:
                resp = requests.post(
                    self.webhook_url,
                    data={"content": f"Daily database backup -- {self._today()} ({size_mb:.1f} MB)"},
                    files={"file": (latest['filename'], f, "application/gzip")},
                    timeout=60,
                )
            resp.raise_for_status()
            print(f"[Discord Backup] Uploaded {latest['filename']} ({size_mb:.1f} MB)")
            self._mark_posted_today()
            return True
        except Exception as e:
            print(f"[Discord Backup] Failed to upload backup: {e}")
            return False

    def _loop(self):
        print("[Discord Backup] Upload thread started")
        while self.active:
            try:
                self.post_latest_backup()
            except Exception as e:
                print(f"[Discord Backup] Loop error: {e}")
            time.sleep(self.CHECK_INTERVAL_SECONDS)

    def start(self):
        if not self.webhook_url:
            return
        if self.thread is None or not self.thread.is_alive():
            self.active = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.active = False
