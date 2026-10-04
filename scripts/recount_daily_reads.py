"""
One-time fix: recounts the Stats page's days the activity log still covers
(it keeps 30 days) - chapters read and series added - with the current rules.

  * A day used to be the plain sum of every progress change, so putting a
    series back to a lower chapter (correcting one set on another day) was
    taken off everything else read that day - 2026-10-03 had 10 chapters
    read and was stored as -4, which showed as a day without reading and
    broke the streak. Now each series counts its net change for the day,
    never below 0 (database.progress_chapters_by_day).
  * Days used to be cut in UTC; they're now the user's own (database.stats_tz,
    Paris unless TIMEZONE in .env says otherwise), so a chapter read after
    midnight counts for that day.

New days are counted that way already; this puts the days saved before it
right. Only days after the log's first day are recounted (the first one may
have been partly cleaned out of the log already). Each day's difference is
also applied to its week/month/year rows.

A day with reading of a series that was deleted and then brought back (undo)
is left alone: the stats got that reading back, but its progress entries
stay closed out in the log, so a recount would lose it.

Usage: venv\\Scripts\\python.exe scripts\\recount_daily_reads.py [--apply]
(or venv/bin/python3 scripts/recount_daily_reads.py --apply on Linux)

Without --apply it only lists what would change. Safe to re-run.
Run it from the project root, same as the server, so it opens the same
data/tracker.db (and reads the same .env).
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')

from backend.database import (get_db, release_db, chapters_read_between, _apply_stats_delta,
                              stats_day, stats_day_start, stats_now, stats_tz)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--apply', action='store_true', help='write the recounted days')
    args = parser.parse_args()

    conn = get_db()
    ok = False
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(timestamp) FROM activity_log WHERE action_type = 'progress'")
        first = cursor.fetchone()[0]
        if not first:
            print("No progress in the activity log - nothing to recount.")
            ok = True
            return
        print(f"Days in {stats_tz().key}")
        # Days a deleted-then-restored series was read on (see above)
        cursor.execute("SELECT series_title, old_value FROM activity_log WHERE action_type = 'deleted' AND can_undo = 0")
        restored_days = {}
        for title, old_value in cursor.fetchall():
            try:
                history = (json.loads(old_value) if old_value else {}).get('_progress_history') or []
            except ValueError:
                continue
            for entry in history:
                if entry.get('timestamp'):
                    restored_days.setdefault(stats_day(entry['timestamp']), set()).add(title)
        day = date.fromisoformat(stats_day(first)) + timedelta(days=1)
        today = stats_now().date()
        changed = 0
        while day <= today:
            d = day.isoformat()
            start = stats_day_start(d)
            end = stats_day_start((day + timedelta(days=1)).isoformat())
            cursor.execute("SELECT chapters_read, series_added FROM stats_history WHERE period_type = 'day' AND period_start = ?", (d,))
            row = cursor.fetchone()
            stored_read, stored_added = (row[0] or 0, row[1] or 0) if row else (0, 0)
            read = chapters_read_between(cursor, start, end)
            # DATETIME() turns the local times into UTC, which created_at is in
            cursor.execute("""
                SELECT COUNT(*) FROM series
                WHERE DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) < DATETIME(?)
            """, (start.isoformat(), end.isoformat()))
            added = cursor.fetchone()[0] or 0
            if d in restored_days and (round(stored_read, 1) != read or stored_added != added):
                print(f"  {d}: left as it is ({stored_read} read) - "
                      f"has reading of restored series: {', '.join(sorted(restored_days[d]))}")
            elif round(stored_read, 1) != read or stored_added != added:
                changed += 1
                print(f"  {d}: read {stored_read} -> {read}, added {stored_added} -> {added}")
                if args.apply:
                    # the week/month/year rows were summed from the same raw
                    # values, negatives and all (the day row is set below)
                    _apply_stats_delta(cursor, start, chapters_delta=read - stored_read,
                                       series_delta=added - stored_added)
                    cursor.execute("""
                        INSERT OR REPLACE INTO stats_history
                        (period_type, period_start, period_end, series_added, chapters_read)
                        VALUES ('day', ?, ?, ?, ?)
                    """, (d, d, added, read))
            day += timedelta(days=1)
        print(f"{changed} day(s) {'recounted' if args.apply else 'would change - run again with --apply to write them'}"
              if changed else "Every day already matches.")
        ok = True
    finally:
        release_db(conn, commit=ok and args.apply)


if __name__ == '__main__':
    main()
