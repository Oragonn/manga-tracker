"""
Cleans up after sources that were removed BEFORE removing a source took its
titles/tags/content rating/gallery covers back out (backend/source_metadata.py).
Those series still carry what the removed source had merged in.

Which sources were removed is found in:
  - the activity log's old 'source_removed' entries (it keeps ~30 days)
  - a database backup: a source in the backup that its series (still
    there) doesn't have any more - even if it was moved to another series. By default the oldest backup in
    backups/database/; pass --backup <file> (repeatable) to use others,
    e.g. an older one downloaded from Discord.
Removals older than all of that can't be traced - nothing recorded which
source it was.

For each one the removed source is fetched from its site, and exactly what
a removal does now is applied: a title/tag goes only if no remaining source
also reports it (two sources with "Horror", one removed: "Horror" stays),
the content rating reverts if that source had set it, and the site's
gallery covers go if no source from that site is left. If a site can't be
reached (e.g. the removed series was deleted from it), that series is
skipped.

Dry run by default - prints what it would change. Add --apply to write.
Every change is logged as a 'source_cleanup' activity (one bulk group), so
it can be undone from the Logs page. Safe to re-run: sources already
cleaned up are skipped.

Usage: venv/bin/python3 scripts/clean_removed_sources.py [--backup FILE ...] [--apply]
(or venv\\Scripts\\python.exe scripts\\clean_removed_sources.py ... on Windows)
"""
import argparse
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
from backend.source_links import parse_source_link, find_series_ids
from backend.source_metadata import strip_removed_source


def open_backup(path):
    path = Path(path)
    if path.suffix == '.gz':
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        with gzip.open(path, 'rb') as src:
            shutil.copyfileobj(src, tmp)
        tmp.close()
        path = Path(tmp.name)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def oldest_backup():
    folder = PROJECT_ROOT / 'backups' / 'database'
    files = list(folder.glob('tracker_backup_*.db.gz')) if folder.is_dir() else []
    return min(files, key=lambda p: p.name) if files else None


def removed_from_log(cursor):
    """(series_id, source_url, source_type) of logged removals from before
    removals cleaned up after themselves, not undone."""
    cursor.execute("""
        SELECT series_id, old_value FROM activity_log
        WHERE action_type = 'source_removed' AND series_id IS NOT NULL AND can_undo = 1
    """)
    found = []
    for series_id, raw in cursor.fetchall():
        old = json.loads(raw) if raw else {}
        # New-style entries (with 'removed') were already cleaned up
        if old.get('source_url') and 'removed' not in old:
            found.append((series_id, old['source_url'], old.get('source_type')))
    return found


def removed_from_backup(path, live_series):
    backup = open_backup(path)
    rows = backup.execute("SELECT series_id, source_url, source_type FROM series_sources").fetchall()
    backup.close()
    return [r for r in rows if r[0] in live_series]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backup', action='append', default=[], help='database backup to compare against')
    parser.add_argument('--apply', action='store_true', help='write the changes (default: dry run)')
    args = parser.parse_args()
    init_db()

    backups = [Path(b) for b in args.backup] or [b for b in [oldest_backup()] if b]

    conn = get_db()
    try:
        cursor = conn.cursor()
        live_series = dict(cursor.execute("SELECT id, title FROM series").fetchall())
        candidates = removed_from_log(cursor)
        # (an undone cleanup has can_undo = 0 and may be redone)
        cursor.execute("SELECT series_id, old_value FROM activity_log WHERE action_type = 'source_cleanup' AND can_undo = 1")
        done = {(sid, json.loads(raw).get('source_url')) for sid, raw in cursor.fetchall() if raw}
    finally:
        release_db(conn)
    print(f"{len(candidates)} old removals in the activity log")
    for b in backups:
        rows = removed_from_backup(b, live_series)
        print(f"{len(rows)} sources in {b.name} (checking which are gone)")
        candidates += rows

    # Keep the ones really gone from that series: it still exists and
    # doesn't have the link again in any form (re-added / URL just
    # rewritten). A link moved to ANOTHER series still counts - what it had
    # merged into this one is still here.
    todo, seen = [], set()
    conn = get_db()
    try:
        cursor = conn.cursor()
        for series_id, url, source_type in candidates:
            link = parse_source_link(url)
            key = (series_id, link or url)
            if key in seen or series_id not in live_series or (series_id, url) in done:
                continue
            seen.add(key)
            if not link or series_id in find_series_ids(cursor, *link):
                continue
            todo.append((series_id, url, source_type or link[0]))
    finally:
        release_db(conn)
    print(f"\n{len(todo)} removed sources to clean up after\n")

    bulk_id = str(uuid.uuid4())
    changed = skipped = 0
    for series_id, url, source_type in sorted(todo, key=lambda t: live_series[t[0]].casefold()):
        title = live_series[series_id]
        try:
            result = strip_removed_source(series_id, {'source_url': url, 'source_type': source_type, 'metadata': None},
                                          apply=args.apply)
        except Exception as e:
            result = {'removed': {}, 'warning': f'failed: {e}'}
        removed = result.get('removed') or {}
        print(f"[{title}]  {url}")
        if removed.get('alt_titles'):
            print(f"    titles:  {', '.join(removed['alt_titles'])}")
        if removed.get('genres'):
            print(f"    tags:    {', '.join(removed['genres'])}")
        if removed.get('content_rating'):
            print(f"    rating:  {removed['content_rating']['old']} -> {removed['content_rating']['new']}")
        if removed.get('gallery_covers'):
            print(f"    gallery: {len(removed['gallery_covers'])} covers")
        if result.get('warning'):
            print(f"    SKIPPED titles/tags/rating: {result['warning']}")
            skipped += 1
        if not removed and not result.get('warning'):
            print("    nothing to remove")
        if removed:
            changed += 1
            if args.apply:
                log_activity('source_cleanup', series_id=series_id, series_title=title,
                             old_value={'source_url': url, 'source_type': source_type, 'removed': removed},
                             is_bulk=True, bulk_id=bulk_id)

    print(f"\n{changed} series {'cleaned up' if args.apply else 'to clean up'}, {skipped} skipped (site unreachable).")
    if not args.apply:
        print("Dry run - nothing written. Re-run with --apply to save.")


if __name__ == "__main__":
    main()
