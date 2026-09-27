"""
Repairs the covers the first version of reapply_mangadex_covers.py
overwrote: it replaced the shown cover of EVERY series with a MangaDex
source by the default MangaDex cover, including the ones you had picked
yourself (Atsumaru covers, gallery covers, uploads...).

Give it a database backup taken before that script ran. For each series it
works out the cover you had chosen - the backup's cover, updated by any
cover change you made after the backup (from the activity log), or the
cover it was added with for series added after the backup - and puts it
back, but only when all of these hold, so nothing else is touched:
  - the series currently shows a MangaDex cover that is exactly its MangaDex
    source's cover (what the script wrote)
  - the cover you had chosen is different and isn't an AniList one (those
    were meant to be replaced - reapply_mangadex_covers.py handles them)

Dry run by default. Add --apply to write. Restores are logged as one bulk
'edited' activity, so they can be undone from the Logs page.

Usage: venv\\Scripts\\python.exe scripts\\restore_covers_from_backup.py <backup.db.gz|backup.db> [--apply]
(or venv/bin/python3 scripts/restore_covers_from_backup.py backups/database/<file> [--apply] on Linux)
"""
import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import init_db, get_db, release_db
from backend.activity_logger import log_activity


def is_anilist(url):
    return bool(url) and 'anilist.co' in url


def open_backup(path):
    """Read-only connection to the backup; a .gz is unpacked to a temp file."""
    path = Path(path)
    if path.suffix == '.gz':
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        with gzip.open(path, 'rb') as src:
            shutil.copyfileobj(src, tmp)
        tmp.close()
        path = Path(tmp.name)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def local_file_exists(url):
    """False only for a /static/... cover whose file is gone from disk."""
    if not url or not url.startswith('/static/'):
        return True
    return (PROJECT_ROOT / 'web' / url.lstrip('/')).exists()


def main():
    args = [a for a in sys.argv[1:] if a != '--apply']
    apply = '--apply' in sys.argv
    if len(args) != 1:
        print(__doc__)
        sys.exit(1)
    init_db()

    backup = open_backup(args[0])
    backup_covers = dict(backup.execute("SELECT id, cover_url FROM series"))
    backup_time = backup.execute("SELECT MAX(timestamp) FROM activity_log").fetchone()[0] or ''
    backup.close()
    print(f"Backup has {len(backup_covers)} series, newest activity at {backup_time}")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, cover_url FROM series")
    current = cursor.fetchall()
    cursor.execute("SELECT series_id, cover_url FROM series_sources WHERE source_type = 'mangadex'")
    md_source_covers = {}
    for series_id, cover in cursor.fetchall():
        md_source_covers.setdefault(series_id, set()).add(cover)
    # What you chose after the backup: the cover a series was added with,
    # then every cover edit (and its undo), oldest first.
    cursor.execute("""
        SELECT series_id, action_type, old_value, new_value, can_undo = 0
        FROM activity_log
        WHERE timestamp > ? AND series_id IS NOT NULL AND action_type IN ('added', 'edited')
        ORDER BY timestamp, id
    """, (backup_time,))
    later = cursor.fetchall()
    release_db(conn)

    chosen = dict(backup_covers)
    for series_id, action, old_json, new_json, undone in later:
        new = json.loads(new_json) if new_json else {}
        old = json.loads(old_json) if old_json else {}
        if 'cover_url' not in new:
            continue
        # An undone edit (mark_log_undone sets can_undo = 0) leaves the
        # series on its old cover.
        chosen[series_id] = old.get('cover_url') if (undone and action == 'edited') else new['cover_url']

    restores = []
    for series_id, title, cover in current:
        want = chosen.get(series_id)
        if not want or want == cover or is_anilist(want):
            continue
        if not (cover and 'uploads.mangadex.org' in cover and cover in md_source_covers.get(series_id, ())):
            continue
        restores.append((series_id, title, cover, want))

    for series_id, title, cover, want in restores:
        note = '' if local_file_exists(want) else '   (WARNING: file missing on disk)'
        print(f"[restore] {title}\n          {cover}\n       -> {want}{note}")
    print(f"\n{len(restores)} covers to restore.")
    if not apply:
        print("Dry run - nothing written. Re-run with --apply to save.")
        return

    conn = get_db()
    try:
        conn.cursor().executemany("UPDATE series SET cover_url = ? WHERE id = ?",
                                  [(want, sid) for sid, _, _, want in restores])
    except Exception:
        release_db(conn, commit=False)
        raise
    release_db(conn)

    bulk_id = str(uuid.uuid4())
    for series_id, title, cover, want in restores:
        log_activity('edited', series_id=series_id, series_title=title,
                     old_value={'cover_url': cover}, new_value={'cover_url': want},
                     is_bulk=True, bulk_id=bulk_id)
    print("Saved.")


if __name__ == "__main__":
    main()
