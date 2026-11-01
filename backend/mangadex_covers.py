# backend/mangadex_covers.py
#
# MangaDex covers, kept on disk and served by the tracker. Since 2026-11
# MangaDex sends the browser's own requests to uploads.mangadex.org to a bot
# check, so the page can't hotlink them anymore (see download_cover_image()
# in trackers/mangadex.py). The database keeps the real uploads.mangadex.org
# URLs; the page asks for /mangadex-cover/<manga id>/<file> instead
# (displayCoverUrl() in dashboard.js), and a file is fetched the first time
# it's asked for - or before that, by warm_covers(), so a cover is already
# here when it's first shown. A cover's filename is its own uuid, so a file
# fetched once never changes.

import os
import re
import threading
import time
import uuid

from .database import get_db, release_db

COVER_DIR = os.path.join(os.path.dirname(__file__), '..', 'web', 'static', 'uploads', 'mangadex_covers')

_MANGA_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_FILE = re.compile(r'^[0-9a-f-]{36}\.(jpe?g|png|gif|webp)(\.(256|512)\.jpg)?$', re.I)
_URL = re.compile(r'^https://uploads\.mangadex\.org/covers/([^/]+)/([^/?#]+)$', re.I)


def valid_name(manga_id, filename):
    return bool(_MANGA_ID.match(manga_id) and _FILE.match(filename))


def parse_cover_url(url):
    """(manga id, filename) of an uploads.mangadex.org cover URL, else None."""
    match = _URL.match((url or '').strip())
    if not match or not valid_name(match.group(1), match.group(2)):
        return None
    return match.group(1), match.group(2)


def cover_path(manga_id, filename):
    return os.path.join(COVER_DIR, manga_id, filename)


def ensure_cover(manga_id, filename):
    """Path of this cover on disk, fetched from MangaDex first if it isn't
    there yet. Raises if MangaDex doesn't hand it over."""
    from .trackers.mangadex import download_cover_image
    path = cover_path(manga_id, filename)
    if not os.path.exists(path):
        content, _ = download_cover_image(manga_id, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Written aside then moved in, so the page asking for the same cover
        # while it's being fetched never gets half a file
        temp_path = f"{path}.{uuid.uuid4().hex}.part"
        with open(temp_path, 'wb') as f:
            f.write(content)
        os.replace(temp_path, path)
    return path


def wanted_covers():
    """Every (manga id, filename) the pages show, in the sizes they show
    them: a series' own cover full size (Series Settings) and 512px (its
    card); source, gallery and related-series covers 256px (thumbnails)."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT cover_url FROM series WHERE cover_url LIKE 'https://uploads.mangadex.org/%'")
        series_covers = [row[0] for row in cursor.fetchall()]
        cursor.execute("""
            SELECT cover_url FROM series_sources WHERE cover_url LIKE 'https://uploads.mangadex.org/%'
            UNION SELECT cover_url FROM gallery_covers WHERE cover_url LIKE 'https://uploads.mangadex.org/%'
            UNION SELECT cover_url FROM related_series WHERE cover_url LIKE 'https://uploads.mangadex.org/%'
        """)
        thumbs = [row[0] for row in cursor.fetchall()]
    finally:
        release_db(conn, commit=False)
    wanted = {}
    for url in series_covers:
        parsed = parse_cover_url(url)
        if parsed:
            manga_id, filename = parsed
            wanted[(manga_id, filename)] = None
            if not filename.endswith(('.256.jpg', '.512.jpg')):
                wanted[(manga_id, f"{filename}.512.jpg")] = None
    for url in thumbs:
        parsed = parse_cover_url(url)
        if parsed:
            manga_id, filename = parsed
            if not filename.endswith(('.256.jpg', '.512.jpg')):
                filename = f"{filename}.256.jpg"
            wanted[(manga_id, filename)] = None
    return list(wanted)


# Covers MangaDex wouldn't hand over (deleted since, mostly) aren't tried
# again by every warm-up - the page still asks for them itself if shown
_failed = set()
_failed_lock = threading.Lock()


def warm_covers(pause=0.2, log=print):
    """Fetch every wanted cover that isn't on disk yet, one at a time with a
    short pause between them (the page's own requests come first). Returns
    (fetched, failed, already there). Stops early if MangaDex is paused or
    turns us away entirely."""
    from .site_pause import SitePaused
    wanted = wanted_covers()
    missing = [name for name in wanted if not os.path.exists(cover_path(*name))]
    with _failed_lock:
        missing = [name for name in missing if name not in _failed]
    if not missing:
        return 0, 0, len(wanted)
    log(f"[MangaDex covers] Fetching {len(missing)} of {len(wanted)} covers not saved yet...")
    fetched = failed = in_a_row = 0
    for manga_id, filename in missing:
        try:
            ensure_cover(manga_id, filename)
            fetched += 1
            in_a_row = 0
        except SitePaused:
            log("[MangaDex covers] MangaDex is paused, stopping")
            break
        except Exception as e:
            failed += 1
            in_a_row += 1
            with _failed_lock:
                _failed.add((manga_id, filename))
            log(f"[MangaDex covers] {e}")
            # Every cover failing = MangaDex is down or turning us away, not
            # a few deleted covers: try again at the next warm-up
            if in_a_row >= 10:
                with _failed_lock:
                    _failed.difference_update(missing)
                log("[MangaDex covers] 10 failures in a row, stopping until the next try")
                break
        time.sleep(pause)
    log(f"[MangaDex covers] Done: {fetched} fetched, {failed} failed, {len(wanted) - len(missing)} already saved")
    return fetched, failed, len(wanted) - len(missing)


_warm_lock = threading.Lock()
_warm_running = False
_warm_again = False


def warm_covers_soon(delay=0):
    """warm_covers() in the background. Asked again while one is running,
    it runs once more after that one instead of twice at once."""
    global _warm_running, _warm_again
    with _warm_lock:
        if _warm_running:
            _warm_again = True
            return
        _warm_running = True

    def run():
        global _warm_running, _warm_again
        time.sleep(delay)
        while True:
            try:
                warm_covers()
            except Exception as e:
                print(f"[MangaDex covers] Warm-up failed: {e}")
            with _warm_lock:
                if not _warm_again:
                    _warm_running = False
                    return
                _warm_again = False

    threading.Thread(target=run, daemon=True).start()


def start_hourly_warmups():
    """A warm-up now and every hour after, catching every cover added any
    way at all (imports, scans that change a cover, related series) - an
    hour with nothing new costs one query and a few hundred file checks."""
    def loop():
        while True:
            warm_covers_soon()
            time.sleep(3600)

    threading.Thread(target=loop, daemon=True).start()
