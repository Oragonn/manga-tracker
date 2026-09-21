"""
Lists the pairs of series in the library that share a title (main or
alternate) - the same check the Add Series dialog now runs before adding a new
series. Read-only: nothing is changed.

Pairs are grouped by how alike they look:
  * same main title        - almost certainly one series added twice
  * main title is an alias - one series' title is an alternate title of the other
  * alternate titles only  - they have an alternate title in common; often the
                             same series under two names, but also sequels and
                             spin-offs that list each other's names

Very short titles and placeholders ("Untitled", "Unknown Manga") are ignored.
There is no merge tool: to combine a pair, add the second series' links to the
first one in Series Settings, then delete the second series.

Usage: venv\\Scripts\\python.exe scripts\\find_duplicate_series.py [--db PATH]
(or venv/bin/python3 scripts/find_duplicate_series.py on Linux)

Run it from the project root, same as the server, so it opens the same
data/tracker.db (or pass --db).
"""
import argparse
import itertools
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import series_search_titles
from backend.search_utils import comparable_titles, normalize_search_text, same_title_rank

RANK_HEADINGS = {
    0: "Same main title",
    1: "Main title is an alias of the other",
    2: "Alternate titles only",
}


def main():
    parser = argparse.ArgumentParser(description="List series that share a title.")
    parser.add_argument("--db", default="data/tracker.db", help="database to read (default: data/tracker.db)")
    args = parser.parse_args()

    # Windows consoles default to a legacy code page that can't print CJK titles
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT id, title, title_en, title_romaji, title_native, alt_titles, status FROM series"
    ).fetchall()
    conn.close()

    info = {}
    by_title = defaultdict(set)
    for series_id, title, title_en, title_romaji, title_native, alt_titles, status in rows:
        titles = comparable_titles(series_search_titles(title, title_en, title_romaji, title_native, alt_titles))
        info[series_id] = (title, status, normalize_search_text(title), titles)
        for t in titles:
            by_title[t].add(series_id)

    pairs = set()
    for ids in by_title.values():
        pairs.update(itertools.combinations(sorted(ids), 2))

    grouped = defaultdict(list)
    for a, b in pairs:
        title_a, status_a, main_a, titles_a = info[a]
        title_b, status_b, main_b, titles_b = info[b]
        rank = same_title_rank(main_a, titles_a, main_b, titles_b)
        grouped[rank].append((a, b, sorted(titles_a & titles_b)))

    print(f"{len(rows)} series, {len(pairs)} pairs that share a title")
    for rank in sorted(grouped):
        print(f"\n== {RANK_HEADINGS[rank]} ({len(grouped[rank])})")
        for a, b, shared in sorted(grouped[rank]):
            for series_id in (a, b):
                title, status, _, _ = info[series_id]
                print(f"  #{series_id:<5} {title}  [{status}]")
            print(f"         shared: {', '.join(shared[:4])}{' ...' if len(shared) > 4 else ''}\n")


if __name__ == "__main__":
    main()
