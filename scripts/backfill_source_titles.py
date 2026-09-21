"""
One-time backfill: gives series the titles their sources go by. Two things
kept those out of the library:

  * MangaDex - the tracker read every alternate title out of the wrong fields
    and so kept none of them (only the main title), for every MangaDex source.
  * Second sources - adding one merged only that source's *alternate* titles
    into the series, and Kagane, Atsumaru, AsuraScans and HiveToons don't list
    their main title among those, so a series that had (say) MangaDex plus
    Kagane never learned what Kagane calls it.

Either way a series couldn't be found by a title its source uses. This
re-fetches every MangaDex source (in batches of 100, so it's a few dozen
requests) and every non-primary Atsumaru, Kagane, AsuraScans and HiveToons
source (a primary source's title is the series' own title), and merges each
title into the series' alt titles and search text.

Additive only -- nothing is removed, and a source that fails to fetch is
skipped without touching its series.

Usage: venv\\Scripts\\python.exe scripts\\backfill_source_titles.py [--dry-run] [--limit N]
(or venv/bin/python3 scripts/backfill_source_titles.py on Linux)

  --dry-run  fetch and report what would change, without writing anything
  --limit N  only process the first N series (handy for a quick test run)

Safe to re-run: once a series has a source's titles the merge is a no-op. Run
it from the project root, same as the server, so it opens the same
data/tracker.db.
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import init_db, get_db, release_db, normalize_for_search
from backend.tag_utils import normalize_tag_list
from backend.title_utils import merge_source_titles

# Fetched one at a time; a primary source of these is skipped (see above).
SECONDARY_SOURCE_TYPES = ('atsu', 'kagane', 'asura', 'hive')


def fetch_info(source_type, source_url):
    """The tracker's series info (title + alt_titles among other things) for
    one non-MangaDex source, or None if the URL isn't parseable. Raises on a
    genuine fetch failure."""
    if source_type == 'atsu':
        from backend.trackers.atsu import extract_series_id, get_series_info
    elif source_type == 'kagane':
        # Imported lazily: kagane.py starts a headless browser at import time.
        from backend.trackers.kagane import extract_series_id, get_series_info
    elif source_type == 'asura':
        from backend.trackers.asura import extract_series_id, get_series_info
    elif source_type == 'hive':
        from backend.trackers.hivetoons import extract_series_id, get_series_info
    else:
        return None
    series_id = extract_series_id(source_url)
    return get_series_info(series_id) if series_id else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, default=None)
    args = parser.parse_args()

    init_db()

    conn = get_db()
    cursor = conn.cursor()
    placeholders = ','.join('?' * len(SECONDARY_SOURCE_TYPES))
    cursor.execute(f"""
        SELECT s.id, s.title, s.title_en, s.title_romaji, s.title_native, s.alt_titles,
               ss.source_type, ss.source_url
        FROM series s
        JOIN series_sources ss ON ss.series_id = s.id
        WHERE ss.source_type = 'mangadex'
           OR (ss.is_primary = 0 AND ss.source_type IN ({placeholders}))
        ORDER BY s.id
    """, SECONDARY_SOURCE_TYPES)
    rows = cursor.fetchall()
    release_db(conn)

    series = {}  # series_id -> {'titles': (...), 'alt_titles': json, 'sources': [(type, url)]}
    for series_id, title, title_en, title_romaji, title_native, alt_titles, source_type, source_url in rows:
        entry = series.setdefault(series_id, {
            'titles': (title, title_en, title_romaji, title_native),
            'alt_titles': alt_titles,
            'sources': [],
        })
        entry['sources'].append((source_type, source_url))

    series_ids = list(series)
    if args.limit:
        series_ids = series_ids[:args.limit]

    print(f"Found {len(series)} series with a MangaDex source or a non-primary "
          f"Atsumaru/Kagane/AsuraScans/HiveToons one"
          f"{f' (processing the first {len(series_ids)})' if args.limit else ''}."
          f"{' DRY RUN - nothing will be written.' if args.dry_run else ''}\n")

    # MangaDex can answer for 100 manga in one request, so ask for all of them up front.
    from backend.trackers.mangadex import extract_manga_id, get_titles_for_manga
    mangadex_ids = [
        extract_manga_id(url)
        for series_id in series_ids
        for source_type, url in series[series_id]['sources']
        if source_type == 'mangadex'
    ]
    mangadex_titles = {}
    try:
        mangadex_titles = get_titles_for_manga([i for i in mangadex_ids if i])
        print(f"Fetched titles for {len(mangadex_titles)} MangaDex manga.\n")
    except Exception as e:
        print(f"[fail] MangaDex titles couldn't be fetched, skipping every MangaDex source: {e}\n")

    updated = 0
    titles_added = 0
    unchanged = 0
    failed_sources = 0

    for i, series_id in enumerate(series_ids, 1):
        entry = series[series_id]
        display = entry['titles'][0]
        try:
            stored = json.loads(entry['alt_titles']) if entry['alt_titles'] else []
        except (ValueError, TypeError):
            stored = []
        if isinstance(stored, dict):
            stored = list(stored.values())
        elif not isinstance(stored, list):
            stored = []
        stored = [t for t in stored if isinstance(t, str)]
        existing = normalize_tag_list(stored)

        merged = existing
        for source_type, source_url in entry['sources']:
            if source_type == 'mangadex':
                manga_id = extract_manga_id(source_url)
                if manga_id not in mangadex_titles:
                    print(f"[skip] {display} (mangadex): no titles returned for {source_url}")
                    failed_sources += 1
                    continue
                info = {'alt_titles': mangadex_titles[manga_id]}
            else:
                try:
                    info = fetch_info(source_type, source_url)
                except Exception as e:
                    print(f"[fail] {display} ({source_type}): {e}")
                    failed_sources += 1
                    continue
                if info is None:
                    print(f"[skip] {display} ({source_type}): couldn't parse an id from {source_url}")
                    failed_sources += 1
                    continue
            merged = merge_source_titles(merged, info, entry['titles'])

        # Keep what was stored exactly as it was (`stored`, untrimmed) and
        # only append the new names.
        added = [t for t in merged if t not in existing]
        if not added:
            unchanged += 1
            print(f"[{i}/{len(series_ids)}] [same] {display}")
            continue

        if not args.dry_run:
            new_alt_titles = stored + added
            searchable_text = normalize_for_search(
                " ".join([t for t in entry['titles'] if t] + new_alt_titles)
            )
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE series SET alt_titles = ?, searchable_text = ? WHERE id = ?",
                (json.dumps(new_alt_titles, ensure_ascii=False), searchable_text, series_id)
            )
            release_db(conn)

        updated += 1
        titles_added += len(added)
        print(f"[{i}/{len(series_ids)}] [ok]   {display}: +{len(added)} "
              f"({', '.join(repr(t) for t in added[:4])}{', ...' if len(added) > 4 else ''})")

    print(f"\nDone: {updated} series {'would be ' if args.dry_run else ''}updated "
          f"(+{titles_added} titles), {unchanged} already complete, "
          f"{failed_sources} source fetch(es) failed or skipped.")


if __name__ == "__main__":
    main()
