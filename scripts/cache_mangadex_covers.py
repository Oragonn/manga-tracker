"""
Saves every MangaDex cover the tracker shows into
web/static/uploads/mangadex_covers/ now, instead of each one the first time
it's shown. The server already does this by itself (at startup, every hour,
and after a series or source is added - see backend/mangadex_covers.py);
this is for doing it by hand, or to see how many are still missing.

Usage: venv\\Scripts\\python.exe scripts\\cache_mangadex_covers.py [--dry-run]
(or venv/bin/python3 scripts/cache_mangadex_covers.py [--dry-run] on Linux)

--dry-run only counts what's saved and what's missing. Safe to re-run -
covers already saved are skipped, and one being fetched by the server at
the same time is simply fetched twice.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.mangadex_covers import COVER_DIR, cover_path, wanted_covers, warm_covers


def folder_size_mb():
    total = 0
    for root, _, files in os.walk(COVER_DIR):
        total += sum(os.path.getsize(os.path.join(root, name)) for name in files)
    return total / 1024 / 1024


def main():
    if '--dry-run' in sys.argv:
        wanted = wanted_covers()
        missing = [name for name in wanted if not os.path.exists(cover_path(*name))]
        print(f"{len(wanted)} covers shown, {len(wanted) - len(missing)} saved, {len(missing)} missing")
    else:
        warm_covers(pause=0.1)
    print(f"mangadex_covers/ is {folder_size_mb():.0f} MB")


if __name__ == '__main__':
    main()
