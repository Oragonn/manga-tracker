"""
One-time backfill: fetches the full cover gallery (every volume/language
variant) for every MangaDex, Atsumaru and Kagane source already in the
library, same as what now happens automatically whenever one of those
sources is added. Existing series never got this retroactively, so this
catches them up. Also safe to re-run later to pick up covers a site has
added since (and to finish a Kagane gallery that hit its download time
budget - already-downloaded covers are skipped instantly).

Usage: venv\\Scripts\\python.exe scripts\\backfill_gallery_covers.py [mangadex] [atsu] [kagane]
(or venv/bin/python3 scripts/backfill_gallery_covers.py ... on Linux)

With no arguments all three run. Name one or more to run only those, e.g.
`... backfill_gallery_covers.py atsu kagane` if MangaDex is already done.

Safe to re-run - save_gallery_covers() uses INSERT OR IGNORE, so already-
fetched covers are just skipped, not duplicated. Atsumaru and Kagane covers
are downloaded into web/static/uploads/ (their CDNs block hotlinking), which
takes a while for a big library - Kagane in particular fetches each series
through a real browser session.
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import init_db, get_db, release_db, save_gallery_covers


def fetch_mangadex(source_url):
    from backend.trackers.mangadex import extract_manga_id, get_all_covers
    manga_id = extract_manga_id(source_url)
    return get_all_covers(manga_id) if manga_id else None


def fetch_atsu(source_url):
    from backend.trackers.atsu import extract_series_id, get_gallery
    manga_id = extract_series_id(source_url)
    return get_gallery(manga_id) if manga_id else None


def fetch_kagane(source_url):
    # Imported here, not at the top: importing the Kagane tracker starts a
    # headless browser, which a MangaDex-only or Atsumaru-only run shouldn't.
    from backend.trackers.kagane import extract_series_id, get_series_info
    kagane_id = extract_series_id(source_url)
    if not kagane_id:
        return None
    return get_series_info(kagane_id, with_gallery=True)['gallery_covers']


# source_type -> (label, fetcher returning a list of covers or None if the URL has no usable id,
#                 extra sleep between series on top of the tracker's own throttle)
SOURCES = {
    'mangadex': ('MangaDex', fetch_mangadex, 0.2),
    'atsu': ('Atsumaru', fetch_atsu, 0),
    'kagane': ('Kagane', fetch_kagane, 0),
}


def backfill(source_type):
    label, fetch, pause = SOURCES[source_type]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ss.series_id, ss.source_url, s.title
        FROM series_sources ss
        JOIN series s ON s.id = ss.series_id
        WHERE ss.source_type = ?
    """, (source_type,))
    rows = cursor.fetchall()
    release_db(conn)

    print(f"\n=== {label}: {len(rows)} source(s) in the library ===\n")

    ok = 0
    failed = 0
    for series_id, source_url, title in rows:
        try:
            covers = fetch(source_url)
        except Exception as e:
            print(f"[fail] {title}: {e}")
            failed += 1
            continue

        if covers is None:
            print(f"[skip] {title}: couldn't extract an id from {source_url}")
            failed += 1
            continue

        save_gallery_covers(series_id, source_type, covers)
        print(f"[ok]   {title}: saved {len(covers)} cover(s)")
        ok += 1

        if pause:
            time.sleep(pause)

    print(f"\n{label} done: {ok} succeeded, {failed} failed.")


def main():
    requested = sys.argv[1:] or list(SOURCES)
    unknown = [s for s in requested if s not in SOURCES]
    if unknown:
        sys.exit(f"Unknown source(s): {', '.join(unknown)}. Choose from: {', '.join(SOURCES)}")

    init_db()
    for source_type in requested:
        backfill(source_type)


if __name__ == "__main__":
    main()
