"""
One-time migration: re-fetches covers from MangaDex for every series that
has a MangaDex source, replacing whatever cover is currently stored (which
may have come from the old AniList fallback). Also updates the per-source
cover_url in series_sources.

Safe to re-run: series whose MangaDex cover hasn't changed are simply
skipped (the UPDATE is a no-op).

Usage: venv\\Scripts\\python.exe scripts\\reapply_mangadex_covers.py
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import init_db, get_db, release_db


def get_mangadex_series():
    """All series that have a MangaDex source, with their current cover."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.id, s.title, s.cover_url, ss.id as source_id, ss.source_url
        FROM series s
        JOIN series_sources ss ON ss.series_id = s.id
        WHERE ss.source_type = 'mangadex'
        ORDER BY s.title COLLATE NOCASE
    """)
    rows = cursor.fetchall()
    release_db(conn)
    return rows


def fetch_mangadex_cover(source_url):
    """Fetch the current MangaDex cover URL for a series. Returns None on failure."""
    from backend.trackers.mangadex import extract_manga_id, get_manga_info

    manga_id = extract_manga_id(source_url)
    if not manga_id:
        return None
    info = get_manga_info(manga_id)
    return info.get('cover_url')


def update_cover(series_id, source_id, new_cover):
    """Update both series.cover_url and series_sources.cover_url."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE series SET cover_url = ? WHERE id = ?", (new_cover, series_id))
        cursor.execute("UPDATE series_sources SET cover_url = ? WHERE id = ?", (new_cover, source_id))
    finally:
        release_db(conn)


def main():
    init_db()

    rows = get_mangadex_series()
    print(f"\n=== {len(rows)} series with a MangaDex source ===\n")

    updated = 0
    skipped = 0
    failed = 0

    for series_id, title, old_cover, source_id, source_url in rows:
        try:
            new_cover = fetch_mangadex_cover(source_url)
        except Exception as e:
            print(f"[fail] {title}: {e}")
            failed += 1
            continue

        if not new_cover:
            print(f"[skip] {title}: no MangaDex cover returned")
            skipped += 1
            continue

        if old_cover == new_cover:
            print(f"[same] {title}: cover already up to date")
            skipped += 1
            continue

        update_cover(series_id, source_id, new_cover)
        print(f"[ok]   {title}: cover updated")
        updated += 1

        # Be nice to the MangaDex API
        time.sleep(0.5)

    print(f"\nDone: {updated} updated, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    main()
