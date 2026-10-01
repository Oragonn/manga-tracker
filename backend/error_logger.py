# backend/error_logger.py
import os
import json
from datetime import datetime, timedelta
from threading import Lock
from urllib.parse import urlparse

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

# In-memory errors (the unread-count badge reads these). Seeded from the
# log files on first use, so a restart doesn't reset the badge to 0 while
# unread errors are still sitting in today's log.
_errors = []
_errors_lock = Lock()
_errors_loaded = False

# Acknowledged sites: their errors are still logged and listed on /errors but
# no longer count toward the badge, so an outage doesn't add one per series
# every scan. {site: {"muted_at": iso, "failing": [normalised urls]}} -
# "failing" is the links that errored while acknowledged; the first of them to
# fetch fine again means the site is back, and lifts the acknowledgement.
MUTES_FILE = os.path.join("data", "muted_error_sites.json")
_mutes = None
_mutes_lock = Lock()

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

    with _errors_lock:
        # Seed from the log files before this entry is written to them, or
        # the first error after a restart gets counted twice
        _load_recent_errors()
        log_file = os.path.join(LOG_DIR, f"error_{now_paris.strftime('%Y-%m-%d')}.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        _errors.append(log_entry)
        if len(_errors) > 200:
            _errors.pop(0)

    site = error_site(source_url)
    if site:
        with _mutes_lock:
            mute = _load_mutes().get(site)
            url = _norm_url(source_url)
            if mute is not None and url not in mute["failing"]:
                mute["failing"].append(url)
                _save_mutes()


def error_site(url):
    """The site an error belongs to ('atsu.moe'), or '' for a source_url
    that isn't a link (e.g. 'series:123')."""
    try:
        host = (urlparse(str(url or '').strip()).hostname or '').lower()
    except ValueError:
        return ''
    return host[4:] if host.startswith('www.') else host


def _norm_url(url):
    try:
        u = urlparse(str(url or '').strip())
    except ValueError:
        return str(url or '').strip()
    return f"{error_site(url)}{u.path.rstrip('/')}"


def _load_mutes():
    """Call with _mutes_lock held."""
    global _mutes
    if _mutes is None:
        _mutes = {}
        try:
            with open(MUTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for site, mute in (data if isinstance(data, dict) else {}).items():
                if isinstance(mute, dict):
                    _mutes[site] = {"muted_at": mute.get("muted_at"),
                                    "failing": list(mute.get("failing") or [])}
        except (OSError, ValueError):
            pass
    return _mutes


def _save_mutes():
    """Call with _mutes_lock held."""
    _ensure_dirs()
    tmp = MUTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_mutes, f, ensure_ascii=False)
    os.replace(tmp, MUTES_FILE)


def get_muted_sites():
    """{site: muted_at} of the acknowledged sites."""
    with _mutes_lock:
        return {site: m["muted_at"] for site, m in _load_mutes().items()}


def mute_site(site, failing_urls=()):
    """Acknowledge a site's errors: they stop counting toward the badge
    until one of its failing links (failing_urls, plus any that error
    from now on) fetches fine again."""
    site = (site or '').strip().lower()
    if not site:
        return False
    failing = {_norm_url(u) for u in failing_urls if error_site(u) == site}
    with _mutes_lock:
        mutes = _load_mutes()
        mute = mutes.setdefault(site, {"muted_at": _get_now_paris().isoformat(), "failing": []})
        mute["failing"] = sorted(failing | set(mute["failing"]))
        _save_mutes()
    return True


def unmute_site(site):
    with _mutes_lock:
        if _load_mutes().pop((site or '').strip().lower(), None) is None:
            return False
        _save_mutes()
    return True


def note_source_ok(source_url):
    """A source fetched fine: if it was one of an acknowledged site's failing
    links, the site is back, so its errors count toward the badge again."""
    site = error_site(source_url)
    if not site:
        return
    with _mutes_lock:
        mutes = _load_mutes()
        mute = mutes.get(site)
        if mute is None or _norm_url(source_url) not in mute["failing"]:
            return
        del mutes[site]
        _save_mutes()
    print(f"[Errors] {site} is fetching again - its errors count toward the badge again")


def _load_recent_errors():
    """Fill _errors from the last few days' log files, once. Call with
    _errors_lock held."""
    global _errors_loaded
    if _errors_loaded:
        return
    _errors_loaded = True
    today = _get_now_paris().date()
    entries = []
    for days_ago in range(ERRORS_MAX_DAYS - 1, -1, -1):
        date_str = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        entries.extend(reversed(get_errors_for_date(date_str)))  # oldest first
    _errors[:0] = entries[-200:]
    del _errors[:-200]

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

    muted = get_muted_sites()
    count = 0
    with _errors_lock:
        _load_recent_errors()
        for err in _errors:
            if muted and error_site(err.get('source_url')) in muted:
                continue
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
        _load_recent_errors()
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
                    if not line.strip():
                        continue
                    # One damaged line (two scan threads writing at once, a
                    # crash mid-write) is skipped rather than hiding every
                    # error after it
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict):
                        errors.append(entry)
        except OSError:
            pass
    return list(reversed(errors))