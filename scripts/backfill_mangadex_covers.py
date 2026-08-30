"""
One-time backfill: fetches the full MangaDex cover gallery (every
volume/locale variant) for every MangaDex source already in the library,
same as what now happens automatically whenever a MangaDex source is added.
Existing series never got this retroactively, so this catches them up.

Usage: venv\\Scripts\\python.exe scripts\\backfill_mangadex_covers.py
(or venv/bin/python3 scripts/backfill_mangadex_covers.py on Linux)

Safe to re-run - save_mangadex_covers() uses INSERT OR IGNORE, so already-
fetched covers are just skipped, not duplicated.
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import init_db, get_db, release_db, save_mangadex_covers
from backend.trackers.mangadex import extract_manga_id, get_all_covers


def main():
    init_db()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ss.series_id, ss.source_url, s.title
        FROM series_sources ss
        JOIN series s ON s.id = ss.series_id
        WHERE ss.source_type = 'mangadex'
    """)
    rows = cursor.fetchall()
    release_db(conn)

    print(f"Found {len(rows)} MangaDex source(s) in the library.\n")

    ok = 0
    failed = 0
    for series_id, source_url, title in rows:
        manga_id = extract_manga_id(source_url)
        if not manga_id:
            print(f"[skip] {title}: couldn't extract a manga id from {source_url}")
            failed += 1
            continue

        try:
            covers = get_all_covers(manga_id)
            save_mangadex_covers(series_id, covers)
            print(f"[ok]   {title}: saved {len(covers)} cover(s)")
            ok += 1
        except Exception as e:
            print(f"[fail] {title}: {e}")
            failed += 1

        time.sleep(0.2)  # extra breathing room on top of the tracker's own throttle

    print(f"\nDone: {ok} succeeded, {failed} failed.")


if __name__ == "__main__":
    main()
