# backend/site_health.py
#
# Is each source site up? Every 30 minutes (and on demand from the Scheduler
# page) one series per site is fetched exactly the way a scan fetches it, and
# the site only counts as up if its chapter list actually comes back - a site
# that answers with something else (Flame Comics redirecting every page to
# its Discord, a Cloudflare wall, an error page, a changed page format) is
# down even though it "responded".
#
# The series checked is one of yours that this site has supplied chapters
# for before, so an empty answer means the site is broken rather than the
# series being empty. If it fails, a second series is tried before calling
# the whole site down - one removed series isn't a site outage.
#
# A site found down is then asked for its home page too: if that loads,
# only the API (or the series pages) is down while the website itself is
# up - Kagane, 2026-10 - and the Scheduler page shows it orange instead of
# red. Everywhere else (fallback to other sources, outage grouping) it still
# counts as down: chapters can't be fetched either way.

import asyncio
import re
import threading
import time
from datetime import datetime, timezone, timedelta

SITES = ['mangadex', 'kagane', 'atsu', 'asura', 'hive', 'flame', 'thunder', 'comix']
SITE_LABELS = {
    'mangadex': 'MangaDex', 'kagane': 'Kagane', 'atsu': 'Atsumaru',
    'asura': 'AsuraScans', 'hive': 'HiveToons', 'flame': 'Flame Comics',
    'thunder': 'Thunderscans', 'comix': 'Comix',
}
HOMEPAGES = {
    'mangadex': 'https://mangadex.org/', 'kagane': 'https://kagane.to/',
    'atsu': 'https://atsu.moe/', 'asura': 'https://asurascans.com/',
    'hive': 'https://hivetoons.org/', 'flame': 'https://flamecomics.xyz/',
    'thunder': 'https://en-thunderscans.com/', 'comix': 'https://comix.to/',
}
_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
# A fetch error meaning the site didn't answer (timed out, refused the
# connection) rather than answering something wrong about one series
_NOT_ANSWERING = re.compile(
    r"timed? ?out|timeout|didn't answer|max retries exceeded|failed to establish"
    r"|connection (?:aborted|refused|reset)|name or service not known|getaddrinfo failed",
    re.IGNORECASE)
CHECK_INTERVAL = timedelta(minutes=30)
_REFERENCES_TRIED = 2

_check_lock = threading.Lock()
_known_down = None      # sites stored as down (None until first read) - see note_site_ok
_checking = set()       # sites being checked right now
_next_check_at = None   # when the 30-minute loop runs next


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _ensure_table(cursor):
    # Created here rather than in init_db() so it exists whatever database
    # is in place (a restored backup older than this table included).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS site_health (
            source_type TEXT PRIMARY KEY,
            status TEXT,            -- 'up' / 'down'
            checked_at TEXT,
            error TEXT,
            checked_url TEXT,       -- the series link the check used
            duration_ms INTEGER,
            down_since TEXT,
            last_up_at TEXT,
            website_up INTEGER      -- when down: 1 if the home page still loads (only the API is down)
        )
    """)
    cursor.execute("PRAGMA table_info(site_health)")
    if 'website_up' not in {r[1] for r in cursor.fetchall()}:
        cursor.execute("ALTER TABLE site_health ADD COLUMN website_up INTEGER")


def _reference_sources(source_type):
    """Up to two of this site's links to check with: ones with stored
    chapters from it, healthiest first, varied between checks."""
    from .database import get_db, release_db
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ss.id, ss.series_id, ss.source_url, ss.source_type
            FROM series_sources ss
            WHERE ss.source_type = ?
              AND EXISTS (SELECT 1 FROM chapters c
                          WHERE c.series_id = ss.series_id AND c.source_type = ss.source_type)
            ORDER BY ss.consecutive_failures ASC, RANDOM()
            LIMIT ?
        """, (source_type, _REFERENCES_TRIED))
        rows = cursor.fetchall()
        if not rows:
            # Nothing with stored chapters yet - any link of this site
            cursor.execute("""
                SELECT id, series_id, source_url, source_type FROM series_sources
                WHERE source_type = ? ORDER BY consecutive_failures ASC, RANDOM() LIMIT ?
            """, (source_type, _REFERENCES_TRIED))
            rows = cursor.fetchall()
    finally:
        release_db(conn)
    return [{'id': r[0], 'series_id': r[1], 'source_url': r[2], 'source_type': r[3], 'is_primary': False}
            for r in rows]


def check_site(source_type, fetch):
    """Check one site now and store the result. `fetch(source)` is the
    scan's own per-source fetch (MangaScheduler._fetch_source_chapters).
    Returns the stored result, or None if no series uses this site."""
    with _check_lock:
        if source_type in _checking:
            return None
        _checking.add(source_type)
    try:
        references = _reference_sources(source_type)
        if not references:
            return None
        started = time.time()
        status, error, used = 'down', None, references[0]['source_url']
        for source in references:
            used = source['source_url']
            try:
                result = fetch(source)
                chapters = result[0] if result else None
                if chapters:
                    status, error = 'up', None
                    break
                error = error or 'Answered, but without a chapter list'
            except Exception as e:
                error = error or (str(e) or type(e).__name__)
                # The site isn't answering at all: another series won't do
                # better, and each try waits out its timeouts again
                if _NOT_ANSWERING.search(str(e)):
                    break
        duration_ms = int((time.time() - started) * 1000)
        website_up = website_loads(source_type) if status == 'down' else None
        _store(source_type, status, (error or '')[:500] or None, used, duration_ms, website_up)
        return get_site_health().get(source_type)
    finally:
        with _check_lock:
            _checking.discard(source_type)


async def browser_page_loads(page, url, timeout=30):
    """Whether `url` loads in a camoufox page (run on that browser's own
    loop): its document must come back below HTTP 400 once any Cloudflare
    challenge has cleared, without being sent off to another site."""
    from .trackers.redirects import redirect_error
    statuses = []

    def on_response(resp):
        try:
            if resp.request.is_navigation_request() and resp.frame == page.main_frame:
                statuses.append(resp.status)
        except Exception:
            pass

    page.on('response', on_response)
    try:
        try:
            await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
        except Exception:
            return False
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if redirect_error(url, page.url):
                return False
            last = statuses[-1] if statuses else None
            if last is not None and last < 400:
                return True
            # 403/503 is Cloudflare's challenge, which reloads the page once
            # it clears; anything else (a 521 "web server is down") is final
            if last is not None and last not in (403, 503):
                return False
            await asyncio.sleep(0.5)
        return False
    finally:
        page.remove_listener('response', on_response)


def website_loads(source_type):
    """Whether the site's home page loads. Never raises."""
    url = HOMEPAGES.get(source_type)
    if not url:
        return False
    try:
        # Behind a Cloudflare challenge plain requests can't pass: loaded in
        # the site's own camoufox browser instead
        if source_type == 'kagane':
            from .camoufox_kagane import kagane_browser
            return kagane_browser.website_loads(url)
        if source_type == 'comix':
            from .camoufox_comix import get_client
            return get_client().website_loads(url)
        import requests
        from .trackers.redirects import redirect_error
        resp = requests.get(url, headers={'User-Agent': _USER_AGENT}, timeout=10)
        return resp.status_code < 400 and not redirect_error(url, resp.url)
    except Exception as e:
        print(f"[Site Health] {SITE_LABELS.get(source_type, source_type)} home page check failed: {e}")
        return False


def _store(source_type, status, error, checked_url, duration_ms, website_up=None):
    from .database import get_db, release_db
    now = _now()
    conn = get_db()
    try:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute("SELECT status, down_since FROM site_health WHERE source_type = ?", (source_type,))
        row = cursor.fetchone()
        was_down = row and row[0] == 'down'
        down_since = (row[1] if was_down else now) if status == 'down' else None
        cursor.execute("""
            INSERT INTO site_health (source_type, status, checked_at, error, checked_url, duration_ms, down_since, last_up_at, website_up)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type) DO UPDATE SET
                status = excluded.status, checked_at = excluded.checked_at, error = excluded.error,
                checked_url = excluded.checked_url, duration_ms = excluded.duration_ms,
                down_since = excluded.down_since,
                last_up_at = COALESCE(excluded.last_up_at, site_health.last_up_at),
                website_up = excluded.website_up
        """, (source_type, status, now, error, checked_url, duration_ms, down_since,
              now if status == 'up' else None,
              (1 if website_up else 0) if status == 'down' else None))
    finally:
        release_db(conn)
    _remember(source_type, status == 'down')
    if status == 'down':
        what = 'API is DOWN (website still loads)' if website_up else 'is DOWN'
        print(f"[Site Health] {SITE_LABELS.get(source_type, source_type)} {what}: {error}")


def _remember(source_type, is_down):
    global _known_down
    with _check_lock:
        if _known_down is not None:
            (_known_down.add if is_down else _known_down.discard)(source_type)


def note_site_ok(source_type):
    """A scan just got chapters from this site: if the last check called it
    down, it's back - say so now instead of at the next 30-minute check.
    Called on every successful fetch, so it's a set lookup unless the site
    was down."""
    global _known_down
    with _check_lock:
        loaded = _known_down is not None
        down = loaded and source_type in _known_down
    if not loaded:
        current = down_sites()
        with _check_lock:
            _known_down = current
        down = source_type in current
    if not down:
        return
    from .database import get_db, release_db
    now = _now()
    conn = get_db()
    try:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute("""
            UPDATE site_health SET status = 'up', error = NULL, down_since = NULL,
                   website_up = NULL, checked_at = ?, last_up_at = ?
            WHERE source_type = ? AND status = 'down'
        """, (now, now, source_type))
    finally:
        release_db(conn)
    _remember(source_type, False)
    print(f"[Site Health] {SITE_LABELS.get(source_type, source_type)} is back up (a scan just worked)")


def get_site_health():
    """{source_type: {...}} for every site with a stored check."""
    from .database import get_db, release_db
    conn = get_db()
    try:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute("""
            SELECT source_type, status, checked_at, error, checked_url, duration_ms, down_since, last_up_at, website_up
            FROM site_health
        """)
        rows = cursor.fetchall()
    finally:
        release_db(conn)
    keys = ('source_type', 'status', 'checked_at', 'error', 'checked_url', 'duration_ms', 'down_since', 'last_up_at', 'website_up')
    out = {r[0]: dict(zip(keys, r)) for r in rows}
    for h in out.values():
        h['website_up'] = bool(h['website_up'])
    return out


def down_sites():
    return {t for t, h in get_site_health().items() if h['status'] == 'down'}


def summary():
    """Every site for the Scheduler page: stored result, how many series
    use it, whether it's being checked now, and when the next check is."""
    from .database import get_db, release_db
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT source_type, COUNT(DISTINCT series_id) FROM series_sources GROUP BY source_type")
        counts = dict(cursor.fetchall())
    finally:
        release_db(conn)
    health = get_site_health()
    with _check_lock:
        checking = set(_checking)
    return {
        'next_check_at': _next_check_at.isoformat().replace('+00:00', 'Z') if _next_check_at else None,
        'sites': [
            {**(health.get(t) or {'source_type': t, 'status': None}),
             'label': SITE_LABELS[t], 'series_count': counts.get(t, 0), 'checking': t in checking}
            for t in SITES
        ],
    }


def check_all(fetch, only=None):
    """Check every site (or just `only`), each on its own thread - a slow
    Kagane browser fetch shouldn't hold up the others."""
    threads = [threading.Thread(target=check_site, args=(t, fetch), daemon=True)
               for t in SITES if only in (None, t)]
    for th in threads:
        th.start()
    return threads


def run_loop(fetch, is_active, first_delay=30):
    """The 30-minute loop, started with the scheduler."""
    global _next_check_at
    _next_check_at = datetime.now(timezone.utc) + timedelta(seconds=first_delay)
    while is_active():
        wait = (_next_check_at - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(min(wait, 5))
            continue
        _next_check_at = datetime.now(timezone.utc) + CHECK_INTERVAL
        try:
            for th in check_all(fetch):
                th.join()
        except Exception as e:
            print(f"[Site Health] Check failed: {e}")
