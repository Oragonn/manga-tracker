# backend/visitor_logger.py
import json
import os
from datetime import datetime, timedelta, timezone
from threading import Lock

LOG_DIR = "logs"
SEEN_FILE = os.path.join(LOG_DIR, "visitor_ips_seen.json")
DEDUP_WINDOW = timedelta(days=7)
VISITOR_LOG_MAX_DAYS = 30

_lock = Lock()
_seen_cache = None


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _load_seen():
    global _seen_cache
    if _seen_cache is not None:
        return _seen_cache
    _ensure_dir()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            _seen_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _seen_cache = {}
    return _seen_cache


def _save_seen(seen):
    _ensure_dir()
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


def _cleanup_old_logs():
    cutoff = datetime.now(timezone.utc) - timedelta(days=VISITOR_LOG_MAX_DAYS)
    for filename in os.listdir(LOG_DIR):
        if filename.startswith("visitors_") and filename.endswith(".log"):
            try:
                date_str = filename[len("visitors_"):-len(".log")]
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date < cutoff:
                    os.remove(os.path.join(LOG_DIR, filename))
            except Exception:
                pass


def log_visitor_ip(ip):
    """Logs a non-LAN visitor's IP to logs/visitors_YYYY-MM-DD.log, but only
    once per 7-day window per IP so repeat requests from the same visitor
    don't flood the log."""
    if not ip:
        return
    now = datetime.now(timezone.utc)
    with _lock:
        seen = _load_seen()
        last_seen_str = seen.get(ip)
        if last_seen_str:
            try:
                last_seen = datetime.fromisoformat(last_seen_str)
                if now - last_seen < DEDUP_WINDOW:
                    return
            except ValueError:
                pass

        _ensure_dir()
        _cleanup_old_logs()
        log_file = os.path.join(LOG_DIR, f"visitors_{now.strftime('%Y-%m-%d')}.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{now.isoformat()} VISITOR ip={ip}\n")

        seen[ip] = now.isoformat()
        _save_seen(seen)
