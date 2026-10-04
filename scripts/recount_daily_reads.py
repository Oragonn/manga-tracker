"""
One-time fix: recounts the Stats page's "chapters read" for the days the
activity log still covers (it keeps 30 days), with the current rule.

A day used to be the plain sum of every progress change, so putting a
series back to a lower chapter (correcting one set on another day) was taken
off everything else read that day - 2026-10-03 had 10 chapters read and was
stored as -4, which showed as a day without reading and broke the streak.
Now each series counts its net change for the day, never below 0
(database.progress_chapters_by_day). New days are counted that way already;
this puts the days saved before it right.

Only days after the log's first day are recounted (the first one may have
been partly cleaned out of the log already). Each day's difference is also
applied to its week/month/year rows.

Usage: venv\\Scripts\\python.exe scripts\\recount_daily_reads.py [--apply]
(or venv/bin/python3 scripts/recount_daily_reads.py --apply on Linux)

Without --apply it only lists what would change. Safe to re-run.
Run it from the project root, same as the server, so it opens the same
data/tracker.db.
"""
import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import get_db, release_db, chapters_read_between, _apply_stats_delta


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--apply', action='store_true', help='write the recounted days')
    args = parser.parse_args()

    conn = get_db()
    ok = False
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(substr(timestamp, 1, 10)) FROM activity_log WHERE action_type = 'progress'")
        first = cursor.fetchone()[0]
        if not first:
            print("No progress in the activity log - nothing to recount.")
            ok = True
            return
        day = date.fromisoformat(first) + timedelta(days=1)
        today = datetime.now(timezone.utc).date()
        changed = 0
        while day <= today:
            d = day.isoformat()
            cursor.execute("SELECT chapters_read FROM stats_history WHERE period_type = 'day' AND period_start = ?", (d,))
            row = cursor.fetchone()
            stored = row[0] if row else None
            recounted = chapters_read_between(cursor, f'{d}T00:00:00', f'{d}T23:59:59.999999Z')
            if row and round(stored or 0, 1) != recounted:
                changed += 1
                print(f"  {d}: {stored} -> {recounted}")
                if args.apply:
                    # the week/month/year rows were summed from the same raw
                    # changes, negatives and all (the day row is set below)
                    _apply_stats_delta(cursor, datetime.fromisoformat(d).replace(tzinfo=timezone.utc),
                                       chapters_delta=recounted - (stored or 0))
                    cursor.execute("UPDATE stats_history SET chapters_read = ? WHERE period_type = 'day' AND period_start = ?",
                                   (recounted, d))
            day += timedelta(days=1)
        print(f"{changed} day(s) {'recounted' if args.apply else 'would change - run again with --apply to write them'}"
              if changed else "Every day already matches.")
        ok = True
    finally:
        release_db(conn, commit=ok and args.apply)


if __name__ == '__main__':
    main()
