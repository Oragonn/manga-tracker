# backend/error_logger.py
import os
import json
from datetime import datetime, timedelta
from threading import Lock

# Use zoneinfo (Python 3.9+). For older Python, use pytz.
try:
    from zoneinfo import ZoneInfo
    PARIS_TZ = ZoneInfo("Europe/Paris")
except ImportError:
    # Fallback if zoneinfo not available (e.g., Python <3.9)
    import time
    PARIS_TZ = None  # Will use naive local time

LOG_DIR = "logs"
ERRORS_MAX_DAYS = 7

# In-memory errors
_errors = []
_errors_lock = Lock()

def _ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs("data", exist_ok=True)

def _get_now_paris():
    """Get current time in Paris timezone."""
    if PARIS_TZ:
        return datetime.now(PARIS_TZ)
    else:
        # Fallback: naive datetime (assumes system is in CET)
        return datetime.now()

def _cleanup_old_logs():
    _ensure_dirs()
    # Use UTC for cutoff to avoid DST confusion
    from datetime import timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=ERRORS_MAX_DAYS)
    for filename in os.listdir(LOG_DIR):
        if filename.startswith("error_") and filename.endswith(".log"):
            try:
                date_str = filename[6:16]
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date < cutoff:
                    os.remove(os.path.join(LOG_DIR, filename))
            except:
                pass

def log_error(source_url, error_message, series_title=None):
    _ensure_dirs()
    _cleanup_old_logs()

    now_paris = _get_now_paris()
    timestamp_str = now_paris.isoformat()

    log_entry = {
        "timestamp": timestamp_str,
        "series_title": series_title or "Unknown",
        "source_url": source_url,
        "error": str(error_message)
    }

    log_file = os.path.join(LOG_DIR, f"error_{now_paris.strftime('%Y-%m-%d')}.log")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    with _errors_lock:
        _errors.append(log_entry)
        if len(_errors) > 200:
            _errors.pop(0)

def get_last_errors_visit():
    try:
        with open("data/last_errors_visit.txt", "r") as f:
            return f.read().strip()
    except:
        return "1970-01-01T00:00:00+00:00"

def set_last_errors_visit():
    now_paris = _get_now_paris().isoformat()
    with open("data/last_errors_visit.txt", "w") as f:
        f.write(now_paris)

def get_unread_error_count():
    last_visit_str = get_last_errors_visit()
    try:
        # Safe parse with fallback
        if last_visit_str.endswith('Z'):
            last_visit_str = last_visit_str[:-1] + '+00:00'
        from datetime import timezone
        last_visit = datetime.fromisoformat(last_visit_str).astimezone(timezone.utc)
    except:
        from datetime import timezone
        last_visit = datetime(1970, 1, 1, tzinfo=timezone.utc)

    count = 0
    with _errors_lock:
        for err in _errors:
            try:
                err_ts = err['timestamp']
                if err_ts.endswith('Z'):
                    err_ts = err_ts[:-1] + '+00:00'
                err_time = datetime.fromisoformat(err_ts).astimezone(timezone.utc)
                if err_time > last_visit:
                    count += 1
            except:
                pass
    return count

def get_recent_errors(limit=50):
    with _errors_lock:
        return list(reversed(_errors[-limit:]))

def get_available_log_dates():
    _ensure_dirs()
    dates = []
    seen = set()
    today = _get_now_paris().date()

    for i in range(ERRORS_MAX_DAYS):
        date = today - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        label = date.strftime("%d/%m/%Y")  # French format
        log_file = os.path.join(LOG_DIR, f"error_{date_str}.log")
        if os.path.exists(log_file) or i == 0:
            if date_str not in seen:
                dates.append((date_str, label))
                seen.add(date_str)

    for filename in sorted(os.listdir(LOG_DIR), reverse=True):
        if filename.startswith("error_") and filename.endswith(".log"):
            date_str = filename[6:16]
            if date_str not in seen:
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                    label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y")
                    dates.append((date_str, label))
                    seen.add(date_str)
                except:
                    pass
        if len(dates) >= ERRORS_MAX_DAYS:
            break

    return dates[:ERRORS_MAX_DAYS]

def get_errors_for_date(date_str):
    log_file = os.path.join(LOG_DIR, f"error_{date_str}.log")
    errors = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        errors.append(json.loads(line))
        except:
            pass
    return list(reversed(errors))