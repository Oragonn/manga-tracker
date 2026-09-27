"""
Read-only check after restoring a database backup: compares the live
database with the safety copy the Restore button saved just before it
replaced it (backups/database/safety_before_restore_<time>.db.gz), and
reports:

  - whether the restored database is healthy (SQLite integrity check)
  - how far back it went (newest activity in each)
  - what the restore lost: series added since the backup, reading progress,
    status, title and source changes made since
  - covers: which ones differ, split into "the cover-script damage undone"
    (the safety copy showed the default MangaDex cover, the restored one a
    cover you chose) and everything else, how many AniList covers are left,
    and cover files missing on disk

Writes nothing.

Usage: venv/bin/python3 scripts/audit_after_restore.py [safety_backup.db.gz]
(defaults to the newest safety_before_restore_* in backups/database/)
"""
import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / 'data' / 'tracker.db'


def open_ro(path):
    path = Path(path)
    if path.suffix == '.gz':
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        with gzip.open(path, 'rb') as src:
            shutil.copyfileobj(src, tmp)
        tmp.close()
        path = Path(tmp.name)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def find_safety_backup():
    candidates = []
    for folder in (PROJECT_ROOT / 'backups' / 'database', PROJECT_ROOT / 'backups'):
        if folder.is_dir():
            candidates += folder.glob('safety_before_restore_*.db.gz')
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def is_anilist(url):
    return bool(url) and 'anilist.co' in url


def short(url, n=70):
    url = url or '(none)'
    return url if len(url) <= n else '...' + url[-(n - 3):]


def main():
    safety_path = Path(sys.argv[1]) if len(sys.argv) > 1 else find_safety_backup()
    if not safety_path or not safety_path.exists():
        print("No safety_before_restore_* backup found - pass its path as an argument.")
        sys.exit(1)

    now = open_ro(DB_PATH)
    before = open_ro(safety_path)
    print(f"Live database : {DB_PATH}")
    print(f"Before restore: {safety_path}\n")

    # --- Health -----------------------------------------------------------
    print("== Health ==")
    print("  integrity_check:", now.execute("PRAGMA integrity_check").fetchone()[0])
    for label, db in (('live', now), ('before restore', before)):
        newest = db.execute("SELECT MAX(timestamp) FROM activity_log").fetchone()[0]
        counts = [db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ('series', 'series_sources', 'chapters')]
        print(f"  {label:15}: newest activity {newest}, {counts[0]} series, {counts[1]} sources, {counts[2]} chapters")
    orphans = now.execute("""
        SELECT COUNT(*) FROM series_sources ss WHERE NOT EXISTS (SELECT 1 FROM series s WHERE s.id = ss.series_id)
    """).fetchone()[0]
    no_source = now.execute("""
        SELECT COUNT(*) FROM series s WHERE NOT EXISTS (SELECT 1 FROM series_sources ss WHERE ss.series_id = s.id)
    """).fetchone()[0]
    print(f"  sources without a series: {orphans}, series without a source: {no_source}")

    cols = 'id, title, cover_url, status, current_chapter, current_volume'
    live = {r[0]: r for r in now.execute(f"SELECT {cols} FROM series")}
    old = {r[0]: r for r in before.execute(f"SELECT {cols} FROM series")}

    # --- Lost by the restore -------------------------------------------------
    print("\n== Changes since the backup that the restore undid ==")
    gone = [old[i] for i in old if i not in live]
    back = [live[i] for i in live if i not in old]
    print(f"  {len(gone)} series existed before the restore but not now (added after the backup):")
    for r in sorted(gone, key=lambda r: r[1].casefold()):
        print(f"    - {r[1]}  (ch {r[4]}, {r[3]})")
    if back:
        print(f"  {len(back)} series exist now but didn't before the restore (deleted after the backup):")
        for r in sorted(back, key=lambda r: r[1].casefold()):
            print(f"    - {r[1]}")
    progress = [(old[i], live[i]) for i in live if i in old and (old[i][4], old[i][5]) != (live[i][4], live[i][5])]
    print(f"  {len(progress)} series with different reading progress:")
    for o, l in sorted(progress, key=lambda p: p[1][1].casefold()):
        print(f"    - {l[1]}: was ch {o[4]} (vol {o[5]}), now ch {l[4]} (vol {l[5]})")
    status = [(old[i], live[i]) for i in live if i in old and old[i][3] != live[i][3]]
    print(f"  {len(status)} series with a different status:")
    for o, l in sorted(status, key=lambda p: p[1][1].casefold()):
        print(f"    - {l[1]}: was {o[3]}, now {l[3]}")
    titles = [(old[i], live[i]) for i in live if i in old and old[i][1] != live[i][1]]
    if titles:
        print(f"  {len(titles)} renamed series:")
        for o, l in titles:
            print(f"    - '{o[1]}' is now '{l[1]}' again")
    src_old = set(before.execute("SELECT series_id, source_url FROM series_sources"))
    src_now = set(now.execute("SELECT series_id, source_url FROM series_sources"))
    src_added = [(s, u) for s, u in src_old - src_now if s in live]
    src_removed = [(s, u) for s, u in src_now - src_old if s in old]
    print(f"  {len(src_added)} sources added after the backup are gone, {len(src_removed)} removed after it are back:")
    for s, u in sorted(src_added):
        print(f"    - gone: {live[s][1]}: {u}")
    for s, u in sorted(src_removed):
        print(f"    - back: {live[s][1]}: {u}")

    # --- Covers ----------------------------------------------------------------
    print("\n== Covers ==")
    md_before = {}
    for sid, cover in before.execute("SELECT series_id, cover_url FROM series_sources WHERE source_type = 'mangadex'"):
        md_before.setdefault(sid, set()).add(cover)
    fixed, to_anilist, other = [], [], []
    for i in live:
        if i not in old or old[i][2] == live[i][2]:
            continue
        o, l = old[i][2], live[i][2]
        # The cover script's signature: it showed the MangaDex source's cover
        was_script = bool(o) and 'uploads.mangadex.org' in o and o in md_before.get(i, ())
        if was_script and is_anilist(l):
            to_anilist.append(live[i][1])
        else:
            (fixed if was_script else other).append((live[i][1], o, l))
    print(f"  {len(fixed)} covers the cover script had replaced by the MangaDex default are back to your pick")
    for t, o, l in sorted(fixed, key=lambda x: x[0].casefold())[:15]:
        print(f"    - {t}: {short(l)}")
    if len(fixed) > 15:
        print(f"    ... and {len(fixed) - 15} more")
    print(f"  {len(to_anilist)} covers the cover script had changed from AniList to MangaDex are AniList again"
          " (fine - reapply_mangadex_covers.py redoes those)")
    print(f"  {len(other)} other cover differences (a cover changed after the backup, now reverted):")
    for t, o, l in sorted(other, key=lambda x: x[0].casefold()):
        print(f"    - {t}\n        before restore: {short(o)}\n        now:            {short(l)}")

    anilist_series = sum(1 for r in live.values() if is_anilist(r[2]))
    anilist_sources = now.execute(
        "SELECT COUNT(*) FROM series_sources WHERE source_type = 'mangadex' AND cover_url LIKE '%anilist.co%'"
    ).fetchone()[0]
    print(f"  AniList covers left: {anilist_series} series covers, {anilist_sources} MangaDex source covers"
          + ("  -> run scripts/reapply_mangadex_covers.py" if anilist_series or anilist_sources else ""))
    missing = [(r[1], r[2]) for r in live.values()
               if r[2] and r[2].startswith('/static/') and not (PROJECT_ROOT / 'web' / r[2].lstrip('/')).exists()]
    print(f"  {len(missing)} series point at a local cover file that doesn't exist:")
    for t, u in sorted(missing)[:20]:
        print(f"    - {t}: {u}")


if __name__ == "__main__":
    main()
