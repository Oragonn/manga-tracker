"""
One-time backfill: re-fetches every MangaDex, Kagane and Atsumaru source
already in the library and merges in the tags those trackers used to skip
(MangaDex themes/content/format + demographic, Kagane's tags, Atsumaru's
tags). Existing series only ever got their genres at the moment a source was
added, and nothing refreshes them afterwards, so this catches them up.

Additive only -- a series' existing genres are never removed, and a source
that fails to fetch is skipped without touching its series.

Usage: venv\\Scripts\\python.exe scripts\\backfill_tags.py [--dry-run] [--limit N]
(or venv/bin/python3 scripts/backfill_tags.py on Linux)

  --dry-run  fetch and report what would change, without writing anything
  --limit N  only process the first N series (handy for a quick test run)

Safe to re-run: once a series has its tags the merge is a no-op. Run it from
the project root, same as the server, so it opens the same data/tracker.db.
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import init_db, get_db, release_db
from backend.tag_utils import normalize_tag_list, merge_tag_lists

BACKFILL_SOURCE_TYPES = ('mangadex', 'kagane', 'atsu')


def fetch_tags(source_type, source_url):
    """Return the tags the tracker now extracts for one source, or None if
    the URL isn't parseable. Raises on a genuine fetch failure."""
    if source_type == 'mangadex':
        from backend.trackers.mangadex import extract_manga_id, get_manga_info
        manga_id = extract_manga_id(source_url)
        return get_manga_info(manga_id)['genres'] if manga_id else None
    if source_type == 'atsu':
        from backend.trackers.atsu import extract_series_id, get_series_info
        series_id = extract_series_id(source_url)
        return get_series_info(series_id)['genres'] if series_id else None
    if source_type == 'kagane':
        # Imported lazily: kagane.py starts a headless browser at import time.
        from backend.trackers.kagane import extract_series_id, get_series_info
        series_id = extract_series_id(source_url)
        return get_series_info(series_id)['genres'] if series_id else None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, default=None)
    args = parser.parse_args()

    init_db()

    conn = get_db()
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(BACKFILL_SOURCE_TYPES))
    cursor.execute(f"""
        SELECT s.id, s.title, s.genres, ss.source_type, ss.source_url
        FROM series s
        JOIN series_sources ss ON ss.series_id = s.id
        WHERE ss.source_type IN ({placeholders})
        ORDER BY s.id
    """, BACKFILL_SOURCE_TYPES)
    rows = cursor.fetchall()
    release_db(conn)

    series = {}  # series_id -> {'title', 'genres', 'sources': [(type, url)]}
    for series_id, title, genres_json, source_type, source_url in rows:
        entry = series.setdefault(series_id, {
            'title': title,
            'genres': genres_json,
            'sources': [],
        })
        entry['sources'].append((source_type, source_url))

    series_ids = list(series)
    if args.limit:
        series_ids = series_ids[:args.limit]

    print(f"Found {len(series)} series with a MangaDex/Kagane/Atsumaru source"
          f"{f' (processing the first {len(series_ids)})' if args.limit else ''}."
          f"{' DRY RUN - nothing will be written.' if args.dry_run else ''}\n")

    updated = 0
    tags_added = 0
    unchanged = 0
    failed_sources = 0

    for i, series_id in enumerate(series_ids, 1):
        entry = series[series_id]
        try:
            existing = normalize_tag_list(json.loads(entry['genres'])) if entry['genres'] else []
        except (ValueError, TypeError):
            existing = []

        fetched = []
        for source_type, source_url in entry['sources']:
            try:
                tags = fetch_tags(source_type, source_url)
            except Exception as e:
                print(f"[fail] {entry['title']} ({source_type}): {e}")
                failed_sources += 1
                continue
            if tags is None:
                print(f"[skip] {entry['title']} ({source_type}): couldn't parse an id from {source_url}")
                failed_sources += 1
                continue
            fetched.append(normalize_tag_list(tags))

        merged = merge_tag_lists(existing, *fetched)
        added = len(merged) - len(existing)

        if merged == existing:
            unchanged += 1
            print(f"[{i}/{len(series_ids)}] [same] {entry['title']}")
            continue

        if not args.dry_run:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE series SET genres = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), series_id)
            )
            release_db(conn)

        updated += 1
        tags_added += added
        print(f"[{i}/{len(series_ids)}] [ok]   {entry['title']}: {len(existing)} -> {len(merged)} tags")

    print(f"\nDone: {updated} series {'would be ' if args.dry_run else ''}updated "
          f"(+{tags_added} tags), {unchanged} already complete, "
          f"{failed_sources} source fetch(es) failed or skipped.")


if __name__ == "__main__":
    main()
