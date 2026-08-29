# backend/auth_logger.py
import os
from datetime import datetime, timedelta, timezone

LOG_DIR = "logs"
AUTH_LOG_MAX_DAYS = 30


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _cleanup_old_logs():
    _ensure_dir()
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUTH_LOG_MAX_DAYS)
    for filename in os.listdir(LOG_DIR):
        if filename.startswith("auth_") and filename.endswith(".log"):
            try:
                date_str = filename[5:15]
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date < cutoff:
                    os.remove(os.path.join(LOG_DIR, filename))
            except Exception:
                pass


def _write(ip, event):
    _ensure_dir()
    _cleanup_old_logs()
    now = datetime.now(timezone.utc)
    log_file = os.path.join(LOG_DIR, f"auth_{now.strftime('%Y-%m-%d')}.log")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"{now.isoformat()} {event} ip={ip}\n")


def log_failed_login(ip):
    _write(ip, "FAILED_LOGIN")


def log_lockout(ip):
    _write(ip, "LOCKOUT")
