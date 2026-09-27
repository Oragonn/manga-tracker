"""
One-time cleanup after the AniList cover fallback was removed: replaces
every AniList cover that fallback left behind with the real MangaDex cover.

Two places can hold one:
  - series_sources.cover_url of a MangaDex source (the per-source cover the
    Series Settings cover picker offers as "MangaDex")
  - series.cover_url, the cover actually shown

Only covers whose URL is an AniList one are touched. A cover you picked
yourself (an Atsumaru/Kagane/Asura cover, a gallery cover, an upload, ...)
is never changed - that was the bug in the first version of this script,
see scripts/restore_covers_from_backup.py to repair what it overwrote.

Covers are fetched 100 series per MangaDex request, so it's a few requests
for the whole library instead of two per series.

Dry run by default - prints what would change. Add --apply to write it.
Every series.cover_url change is logged as one bulk 'edited' activity, so
it can be undone from the Logs page like any other bulk edit.

Usage: venv\\Scripts\\python.exe scripts\\reapply_mangadex_covers.py [--apply]
(or venv/bin/python3 scripts/reapply_mangadex_covers.py [--apply] on Linux)
"""
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import init_db, get_db, release_db
from backend.activity_logger import log_activity


def is_anilist(url):
    return bool(url) and 'anilist.co' in url


def load_rows():
    """Every MangaDex source, with its series' current cover."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.id, s.title, s.cover_url, ss.id, ss.source_url, ss.cover_url, ss.is_primary
        FROM series s
        JOIN series_sources ss ON ss.series_id = s.id
        WHERE ss.source_type = 'mangadex'
        ORDER BY s.title COLLATE NOCASE
    """)
    rows = cursor.fetchall()
    release_db(conn)
    return rows


def main():
    apply = '--apply' in sys.argv
    init_db()

    from backend.trackers.mangadex import extract_manga_id, get_covers_for_manga

    rows = load_rows()
    # Only the MangaDex sources that matter: an AniList per-source cover, or
    # a series whose shown cover is an AniList one.
    rows = [r for r in rows if is_anilist(r[5]) or is_anilist(r[2])]
    manga_ids = {r[3]: extract_manga_id(r[4]) for r in rows}
    print(f"{len(rows)} MangaDex sources to check, fetching their covers...")
    covers = get_covers_for_manga([m for m in manga_ids.values() if m])

    source_updates = []   # (source_id, new_cover)
    series_new = {}       # series_id -> (title, old_cover, new_cover, rank)
    missing = []
    for series_id, title, series_cover, source_id, source_url, source_cover, is_primary in rows:
        new_cover = covers.get(manga_ids[source_id])
        if not new_cover:
            missing.append((title, source_url))
            continue
        if is_anilist(source_cover):
            source_updates.append((source_id, new_cover))
        if is_anilist(series_cover):
            # With more than one MangaDex source, prefer the one whose old
            # cover is what the series shows, then the primary one.
            rank = (series_cover == source_cover, bool(is_primary))
            prev = series_new.get(series_id)
            if prev is None or rank > prev[3]:
                series_new[series_id] = (title, series_cover, new_cover, rank)

    for title, old, new, _ in series_new.values():
        print(f"[cover] {title}\n        {old}\n     -> {new}")
    for title, url in missing:
        print(f"[miss]  {title}: MangaDex returned no cover for {url}")

    print(f"\n{len(series_new)} series covers and {len(source_updates)} MangaDex source covers to replace, "
          f"{len(missing)} not found on MangaDex.")
    if not apply:
        print("Dry run - nothing written. Re-run with --apply to save.")
        return

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.executemany("UPDATE series_sources SET cover_url = ? WHERE id = ?",
                           [(c, sid) for sid, c in source_updates])
        cursor.executemany("UPDATE series SET cover_url = ? WHERE id = ?",
                           [(v[2], sid) for sid, v in series_new.items()])
    except Exception:
        release_db(conn, commit=False)
        raise
    release_db(conn)

    bulk_id = str(uuid.uuid4())
    for series_id, (title, old, new, _) in series_new.items():
        log_activity('edited', series_id=series_id, series_title=title,
                     old_value={'cover_url': old}, new_value={'cover_url': new},
                     is_bulk=True, bulk_id=bulk_id)
    print("Saved.")


if __name__ == "__main__":
    main()
