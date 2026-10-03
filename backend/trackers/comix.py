# backend/trackers/comix.py
#
# Comix (comix.to) is an aggregator: every scanlation group's upload of a
# chapter is its own row, so a series with ~50 chapters can list 750
# uploads. Its API is only usable through a browser (see
# backend/camoufox_comix.py), at up to 100 uploads per call.
#
# Fetching all of that on every scan would be wasteful, so the uploads seen
# so far are kept per series in comix_uploads:
#   - the first fetch (and a weekly resync) reads the whole list;
#   - a rescan reads uploads newest-first and stops at the first one it
#     already has, so a chapter posted anywhere in the list - a new latest
#     chapter, or a missing older one filled in later - is picked up from
#     the first page or two;
#   - if the site's upload count then doesn't match what's stored (uploads
#     deleted, or more new ones than the pages read), the whole list is
#     read again.
#
# Comix only gives upload times as "6h ago" / "3d ago" / "8mos ago". Each
# upload gets an absolute date the first time it's seen, which is then kept,
# so dates don't drift between scans. Uploads first seen on a rescan are
# accurate to the hour; ones from the first full read can be months coarse.

import re
from datetime import datetime, timedelta, timezone

SITE_BASE = "https://comix.to"

_PAGE_SIZE = 100          # the API's maximum (500 is refused with a 422)
_MAX_INCREMENTAL_PAGES = 3
_FULL_RESYNC_EVERY = timedelta(days=7)

_HID_RE = re.compile(r'comix\.to/title/([A-Za-z0-9]+)(?:-[^/?#]*)?', re.I)


def extract_series_id(url):
    """The Comix series id ("hid") from a series or chapter link:
    https://comix.to/title/n93ny-the-crown-ill-claim[/11437213-chapter-49]."""
    match = _HID_RE.search(url or '')
    return match.group(1) if match else None


def canonical_url(url):
    """A series link without a chapter part, query or fragment."""
    match = _HID_RE.search(url or '')
    return f"{SITE_BASE}/title/{match.group(0).split('/title/', 1)[1]}" if match else url


# --- upload cache ---

def _ensure_tables(cursor):
    # Created here rather than in init_db() so they exist whatever database
    # is in place (a restored backup older than them included).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS comix_uploads (
            hid TEXT NOT NULL,
            upload_id INTEGER NOT NULL,
            chapter_number REAL,
            volume REAL,
            name TEXT,
            language TEXT,
            group_name TEXT,
            chapter_url TEXT,
            posted_at TEXT,          -- absolute date given when first seen
            PRIMARY KEY (hid, upload_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS comix_sync (
            hid TEXT PRIMARY KEY,
            total INTEGER,           -- the site's upload count at the last fetch
            full_synced_at TEXT
        )
    """)


def _load_cache(hid):
    from ..database import get_db, release_db
    conn = get_db()
    try:
        cursor = conn.cursor()
        _ensure_tables(cursor)
        cursor.execute("""
            SELECT upload_id, chapter_number, volume, name, language, group_name, chapter_url, posted_at
            FROM comix_uploads WHERE hid = ?
        """, (hid,))
        keys = ('upload_id', 'chapter_number', 'volume', 'name', 'language', 'group_name', 'chapter_url', 'posted_at')
        uploads = {r[0]: dict(zip(keys, r)) for r in cursor.fetchall()}
        cursor.execute("SELECT total, full_synced_at FROM comix_sync WHERE hid = ?", (hid,))
        sync = cursor.fetchone()
    finally:
        release_db(conn)
    return uploads, sync


def _save_cache(hid, uploads, total, full_sync, replace):
    from ..database import get_db, release_db
    now = _iso(datetime.now(timezone.utc))
    conn = get_db()
    try:
        cursor = conn.cursor()
        _ensure_tables(cursor)
        if replace:
            cursor.execute("DELETE FROM comix_uploads WHERE hid = ?", (hid,))
        cursor.executemany("""
            INSERT OR REPLACE INTO comix_uploads
                (hid, upload_id, chapter_number, volume, name, language, group_name, chapter_url, posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(hid, u['upload_id'], u['chapter_number'], u['volume'], u['name'], u['language'],
               u['group_name'], u['chapter_url'], u['posted_at']) for u in uploads])
        if full_sync:
            cursor.execute("""
                INSERT INTO comix_sync (hid, total, full_synced_at) VALUES (?, ?, ?)
                ON CONFLICT(hid) DO UPDATE SET total = excluded.total, full_synced_at = excluded.full_synced_at
            """, (hid, total, now))
        else:
            cursor.execute("UPDATE comix_sync SET total = ? WHERE hid = ?", (total, hid))
        conn.commit()
    finally:
        release_db(conn)


# --- dates ---

_UNIT_SECONDS = {
    's': 1, 'sec': 1, 'secs': 1, 'second': 1, 'seconds': 1,
    'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60,
    'h': 3600, 'hr': 3600, 'hrs': 3600, 'hour': 3600, 'hours': 3600,
    'd': 86400, 'day': 86400, 'days': 86400,
    'w': 7 * 86400, 'wk': 7 * 86400, 'wks': 7 * 86400, 'week': 7 * 86400, 'weeks': 7 * 86400,
    'mo': 30 * 86400, 'mos': 30 * 86400, 'month': 30 * 86400, 'months': 30 * 86400,
    'y': 365 * 86400, 'yr': 365 * 86400, 'yrs': 365 * 86400, 'year': 365 * 86400, 'years': 365 * 86400,
}
_RELATIVE_RE = re.compile(r'(\d+)\s*([a-z]+)\s+ago', re.I)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def parse_relative(text, now=None):
    """'6h ago' -> an ISO date that many hours before now. None if unknown."""
    now = now or datetime.now(timezone.utc)
    text = (text or '').strip().lower()
    if text in ('just now', 'now', 'today'):
        return _iso(now)
    if text == 'yesterday':
        return _iso(now - timedelta(days=1))
    match = _RELATIVE_RE.search(text)
    if not match:
        return None
    seconds = _UNIT_SECONDS.get(match.group(2))
    if seconds is None:
        return None
    return _iso(now - timedelta(seconds=int(match.group(1)) * seconds))


# --- fetching ---

def _client():
    from ..camoufox_comix import get_client
    return get_client()


def _upload_row(item, now, known):
    upload_id = item.get('id')
    old = known.get(upload_id)
    url = item.get('url') or ''
    try:
        number = float(item['number']) if item.get('number') is not None else None
    except (TypeError, ValueError):
        number = None
    try:
        volume = float(item['volume']) if item.get('volume') else None
    except (TypeError, ValueError):
        volume = None
    return {
        'upload_id': upload_id,
        'chapter_number': number,
        'volume': volume,
        'name': (item.get('name') or '').strip() or None,
        'language': item.get('language'),
        'group_name': (item.get('group') or {}).get('name'),
        'chapter_url': SITE_BASE + url if url.startswith('/') else url,
        # first-seen date wins - see the note at the top
        'posted_at': (old or {}).get('posted_at') or parse_relative(item.get('createdAtFormatted'), now),
    }


def _chapter_page(client, hid, page, order):
    data = client.api(hid, f"/manga/{hid}/chapters", {'page': page, 'limit': _PAGE_SIZE, 'order': {order: 'desc'}})
    return (data or {}).get('items') or [], (data or {}).get('meta') or {}


def _sync_uploads(client, hid):
    """Bring comix_uploads up to date for `hid`; returns every stored upload."""
    known, sync = _load_cache(hid)
    now = datetime.now(timezone.utc)
    full = not sync or not known
    if not full and sync[1]:
        try:
            last_full = datetime.fromisoformat(sync[1].replace('Z', '+00:00'))
            full = now - last_full > _FULL_RESYNC_EVERY
        except ValueError:
            full = True

    if not full:
        new_rows, total = [], None
        for page in range(1, _MAX_INCREMENTAL_PAGES + 1):
            items, meta = _chapter_page(client, hid, page, 'created_at')
            total = meta.get('total')
            unseen = [i for i in items if i.get('id') not in known]
            new_rows.extend(_upload_row(i, now, known) for i in unseen)
            if len(unseen) < len(items) or not meta.get('hasNext'):
                break
        else:
            full = True  # more new uploads than the pages read
        if not full and total is not None and total != len(known) + len(new_rows):
            full = True  # uploads were deleted, or added out of order
        if not full:
            if new_rows:
                print(f"[Comix] {hid}: {len(new_rows)} new upload(s)")
            _save_cache(hid, new_rows, total, full_sync=False, replace=False)
            known.update({r['upload_id']: r for r in new_rows})
            return list(known.values())

    rows, page, total = [], 1, 0
    while True:
        items, meta = _chapter_page(client, hid, page, 'number')
        rows.extend(_upload_row(i, now, known) for i in items)
        total = meta.get('total', len(rows))
        if not meta.get('hasNext') or not items:
            break
        page += 1
    print(f"[Comix] {hid}: full read, {len(rows)} uploads over {page} page(s)")
    _save_cache(hid, rows, total, full_sync=True, replace=True)
    return rows


def _chapters_from_uploads(uploads, status):
    """One chapter per number: the earliest English upload of it (its link
    and date), the way the other trackers keep the earliest posting."""
    by_number = {}
    for u in uploads:
        if u['chapter_number'] is None or (u['language'] or 'en') != 'en' or not u['chapter_url']:
            continue
        best = by_number.get(u['chapter_number'])
        key = (u['posted_at'] or '9999', u['upload_id'])
        if best is None or key < (best['posted_at'] or '9999', best['upload_id']):
            by_number[u['chapter_number']] = u
    chapters = [{
        'chapter_number': u['chapter_number'],
        'volume': u['volume'],
        'title': u['name'],
        'release_date': u['posted_at'],
        'chapter_url': u['chapter_url'],
        'provider': u['group_name'],
        'is_oneshot': False,
    } for u in sorted(by_number.values(), key=lambda u: u['chapter_number'])]
    if len(chapters) == 1 and chapters[0]['chapter_number'] == 0 and status == 'completed':
        chapters[0]['is_oneshot'] = True
    return chapters


def _map_status(raw):
    raw = (raw or '').lower()
    if 'releas' in raw or 'ongoing' in raw or 'publishing' in raw:
        return 'reading'
    if 'finish' in raw or 'complet' in raw or 'ended' in raw:
        return 'completed'
    if 'hiatus' in raw:
        return 'on_hold'
    if 'discontinu' in raw or 'cancel' in raw or 'dropped' in raw:
        return 'dropped'
    return 'plan_to_read'  # "not yet released" / unknown: no information


_CONTENT_RATING = {'safe': 'safe', 'suggestive': 'mild', 'erotica': 'mature', 'pornographic': 'explicit'}


def get_series_info(hid):
    """
    Fetch series metadata and chapters from Comix. Returns a dict shaped
    like the other trackers' get_series_info: {title, cover_url, status,
    chapters, alt_titles, genres, content_rating, source_type}.
    Raises on a genuine fetch failure so callers can tell "broken" from
    "legitimately nothing new".
    """
    if not hid:
        raise ValueError("hid is required")
    try:
        client = _client()
        detail = client.api(hid, f"/manga/{hid}")
        if not detail or not detail.get('title'):
            raise Exception(f"Comix returned no series data for {hid}")

        status = _map_status(detail.get('status'))
        chapters = _chapters_from_uploads(_sync_uploads(client, hid), status)

        title = (detail.get('title') or '').strip() or 'Unknown Title'
        alt_titles = []
        for t in detail.get('altTitles') or []:
            t = (t or '').strip()
            if t and t.casefold() != title.casefold() and t not in alt_titles:
                alt_titles.append(t)

        genres = []
        for group in ('genres', 'demographics', 'tags'):
            for g in detail.get(group) or []:
                name = (g.get('title') or '').strip() if isinstance(g, dict) else ''
                if name and name not in genres:
                    genres.append(name)

        source_type = (detail.get('type') or '').lower()
        if source_type not in ('manga', 'manhwa', 'manhua'):
            source_type = 'other'

        poster = detail.get('poster') or {}
        cover_url = client.download_cover(hid, poster.get('large') or poster.get('medium'))

        return {
            'title': title,
            'cover_url': cover_url,
            'status': status,
            'chapters': chapters,
            'alt_titles': alt_titles,
            'genres': genres,
            'content_rating': _CONTENT_RATING.get((detail.get('contentRating') or '').lower(), 'safe'),
            'source_type': source_type,
        }
    except Exception as e:
        print(f"[Comix] Error fetching series {hid}: {e}")
        raise
