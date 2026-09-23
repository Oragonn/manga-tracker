"""
Audits every series source against a live re-fetch of that source right now,
to check whether the tracker's stored chapters still match what the source
currently reports.

Read-only: only makes outbound HTTP requests to the sources and reads from
the given sqlite database. Never writes to it and never touches production.

Mirrors backend/scheduler.py's scan_series pipeline (fetch each source ->
apply chapter_overrides bans/manual entries -> union of chapter numbers)
closely enough that a mismatch here means either:
  - the tracker is stale (source has new/removed chapters since the last
    real scan), or
  - the source link is dead/broken (extraction or fetch failure), or
  - a genuine parsing/merge bug that a fresh scan would reproduce too.

Usage:
    python scripts/audit_source_accuracy.py --db path/to/tracker.db --out report.jsonl
    python scripts/audit_source_accuracy.py --db ... --limit 15               # smoke test
    python scripts/audit_source_accuracy.py --db ... --series-ids 12,45,901   # specific series
    python scripts/audit_source_accuracy.py --db ... --after "2026-09-15"     # only series added after a date
"""
import argparse
import concurrent.futures
import json
import sqlite3
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.trackers import mangadex, kagane, atsu, asura, hivetoons, flamecomics
from backend.trackers.kagane import _extract_season_and_chapter

# Above this, a jump between two consecutive chapters in a single source's
# own (already sorted) final numbering is flagged for manual review -
# legitimate releases essentially never skip this many numbers at once, so a
# jump this size is more likely a numbering bug (e.g. an undetected/
# miscalculated season-reset offset) than a real gap in the source.
LARGE_JUMP_THRESHOLD = 5


def numbering_diagnostics(source_type, chapters):
    """Checks that don't rely on trusting the fetcher's own output: do two
    different chapters in this source's own list collide on the same final
    number (one would silently vanish on merge - the Tower of God bug's
    shape), and are there suspiciously large jumps in the final sequence
    (the season-reset offset bug's shape). Independent of whether the
    tracker.db copy matches this fetch - these look for a bug baked into
    the parsing itself, which a fresh re-fetch alone can't reveal."""
    diag = {}

    by_number = {}
    for ch in chapters:
        by_number.setdefault(round(ch['chapter_number'], 2), []).append(ch)
    collisions = {num: entries for num, entries in by_number.items() if len(entries) > 1}
    if collisions:
        diag['collisions'] = [
            {'chapter_number': num,
             'entries': [{'title': e.get('title'), 'chapter_url': e.get('chapter_url')} for e in entries]}
            for num, entries in sorted(collisions.items())
        ]

    # Only meaningful for Kagane: the other sources' chapter_number is a
    # direct passthrough of what the site itself reports, so a gap there
    # just reflects the source (a scanlator skipping numbers, an official
    # release renumbering), not something our code computed. For Kagane the
    # number is reconstructed by us, so a gap in the final sequence can mean
    # our reconstruction went wrong.
    if source_type == 'kagane':
        sorted_chapters = sorted(chapters, key=lambda c: c['chapter_number'])
        jumps = []
        for prev, cur in zip(sorted_chapters, sorted_chapters[1:]):
            delta = cur['chapter_number'] - prev['chapter_number']
            if delta > LARGE_JUMP_THRESHOLD:
                jumps.append({
                    'from': prev['chapter_number'], 'to': cur['chapter_number'],
                    'from_title': prev.get('title'), 'to_title': cur.get('title'),
                    'to_url': cur.get('chapter_url'),
                })
        if jumps:
            diag['large_jumps'] = jumps

        resets = []
        prev_raw = None
        for ch in chapters:
            title = ch.get('title') or ''
            _, raw = _extract_season_and_chapter(title)
            if raw is None:
                continue
            if prev_raw is not None and raw < prev_raw:
                resets.append({'title': title, 'raw': raw, 'prev_raw': prev_raw,
                               'final_assigned': ch['chapter_number']})
            prev_raw = raw
        if resets:
            # A season boundary is a one-off ("[Season 2] Ep. 0" after
            # "Ep. 78") - the real shape of the two past bugs. An anthology
            # instead re-titles *every* story "Chapter 1" (Berserk's "The
            # Golden Age, Chapter 1", "Casca, Chapter 1", ...), so nearly
            # every "reset" restarts at raw<=1 - the algorithm already
            # handles that via chapter_no, not a bug, and not worth a human
            # re-checking dozens of near-identical entries for.
            at_one = sum(1 for r in resets if r['raw'] <= 1.0)
            anthology_like = len(resets) >= 3 and (at_one / len(resets)) >= 0.7
            diag['season_resets_detected'] = resets
            diag['likely_anthology_structure'] = anthology_like

    return diag


def fetch_source_chapters(source_type, source_url):
    """Same dispatch as scheduler._fetch_source_chapters, but standalone
    (no DB writes, no status tracking) - just the live chapter list."""
    if source_type == 'mangadex':
        manga_id = mangadex.extract_manga_id(source_url)
        if not manga_id:
            raise ValueError("could not extract MangaDex manga id from url")
        return mangadex.get_latest_chapters(manga_id, limit=100)
    elif source_type == 'kagane':
        kagane_id = kagane.extract_series_id(source_url)
        if not kagane_id:
            raise ValueError("could not extract Kagane series id from url")
        info = kagane.get_series_info(kagane_id)
        return info['chapters'] if info else []
    elif source_type == 'atsu':
        atsu_id = atsu.extract_series_id(source_url)
        if not atsu_id:
            raise ValueError("could not extract Atsu manga id from url")
        info = atsu.get_series_info(atsu_id)
        return info['chapters'] if info else []
    elif source_type == 'asura':
        slug = asura.extract_series_id(source_url)
        if not slug:
            raise ValueError("could not extract Asura slug from url")
        info = asura.get_series_info(slug)
        return info['chapters'] if info else []
    elif source_type == 'hive':
        slug = hivetoons.extract_series_id(source_url)
        if not slug:
            raise ValueError("could not extract Hive slug from url")
        info = hivetoons.get_series_info(slug)
        return info['chapters'] if info else []
    elif source_type == 'flame':
        flame_id = flamecomics.extract_series_id(source_url)
        if not flame_id:
            raise ValueError("could not extract Flame Comics series id from url")
        info = flamecomics.get_series_info(flame_id)
        return info['chapters'] if info else []
    else:
        raise ValueError(f"unknown source_type '{source_type}'")


def load_work_items(db_path, series_ids=None, source_types=None, after=None, limit=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    q = "SELECT id, title, latest_chapter, created_at FROM series"
    clauses, params = [], []
    if series_ids:
        clauses.append(f"id IN ({','.join('?' * len(series_ids))})")
        params.extend(series_ids)
    if after:
        clauses.append("created_at >= ?")
        params.append(after)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    series_rows = cur.execute(q, params).fetchall()

    items = []
    for s in series_rows:
        sid = s['id']
        src_q = "SELECT id, source_url, source_type, is_primary FROM series_sources WHERE series_id = ?"
        src_params = [sid]
        if source_types:
            src_q += f" AND source_type IN ({','.join('?' * len(source_types))})"
            src_params.extend(source_types)
        sources = [dict(r) for r in cur.execute(src_q, src_params).fetchall()]
        if not sources:
            continue

        stored = cur.execute(
            "SELECT chapter_number, source_type, chapter_url FROM chapters WHERE series_id = ?",
            (sid,)
        ).fetchall()
        stored_numbers = {round(r[0], 2) for r in stored}

        overrides = [dict(r) for r in cur.execute(
            "SELECT source_type, chapter_number, is_banned, chapter_url FROM chapter_overrides WHERE series_id = ?",
            (sid,)
        ).fetchall()]

        items.append({
            'series_id': sid,
            'title': s['title'],
            'db_latest_chapter': s['latest_chapter'],
            'sources': sources,
            'stored_numbers': stored_numbers,
            'overrides': overrides,
        })

    conn.close()
    return items


def audit_one_series(item):
    banned_urls = {o['chapter_url'] for o in item['overrides'] if o['is_banned'] and o['chapter_url']}
    manual_numbers = {round(o['chapter_number'], 2) for o in item['overrides'] if not o['is_banned']}

    per_source = []
    fresh_numbers = set()
    any_success = False

    for src in item['sources']:
        entry = {'source_type': src['source_type'], 'source_url': src['source_url'],
                  'is_primary': bool(src['is_primary']), 'ok': False}
        try:
            t0 = time.time()
            chapters = fetch_source_chapters(src['source_type'], src['source_url'])
            elapsed = round(time.time() - t0, 1)
            chapters = [c for c in chapters if c.get('chapter_url') not in banned_urls]
            nums = {round(c['chapter_number'], 2) for c in chapters}
            fresh_numbers |= nums
            entry.update(ok=True, live_count=len(nums),
                         live_latest=max(nums) if nums else None, elapsed_s=elapsed)
            diag = numbering_diagnostics(src['source_type'], chapters)
            if diag:
                entry['diagnostics'] = diag
            any_success = True
        except Exception as e:
            entry['error'] = f"{type(e).__name__}: {e}"[:400]
        per_source.append(entry)

    result = {
        'series_id': item['series_id'],
        'title': item['title'],
        'db_latest_chapter': item['db_latest_chapter'],
        'sources': per_source,
        'any_source_ok': any_success,
    }

    if any_success:
        fresh_numbers |= manual_numbers
        stored_numbers = item['stored_numbers']
        missing_in_tracker = sorted(fresh_numbers - stored_numbers)
        extra_in_tracker = sorted(stored_numbers - fresh_numbers)
        result.update(
            stored_count=len(stored_numbers),
            fresh_count=len(fresh_numbers),
            stored_latest=max(stored_numbers) if stored_numbers else None,
            fresh_latest=max(fresh_numbers) if fresh_numbers else None,
            missing_in_tracker=missing_in_tracker,
            extra_in_tracker=extra_in_tracker,
        )

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--series-ids', help="comma-separated series ids")
    ap.add_argument('--source-types', help="comma-separated source types to restrict to")
    ap.add_argument('--after', help="only series with created_at >= this (ISO date)")
    ap.add_argument('--limit', type=int)
    ap.add_argument('--workers', type=int, default=12)
    args = ap.parse_args()

    series_ids = [int(x) for x in args.series_ids.split(',')] if args.series_ids else None
    source_types = args.source_types.split(',') if args.source_types else None

    items = load_work_items(args.db, series_ids=series_ids, source_types=source_types,
                             after=args.after, limit=args.limit)
    print(f"[Audit] {len(items)} series to check, workers={args.workers}", flush=True)

    out_path = Path(args.out)
    done = 0
    t_start = time.time()
    with out_path.open('w', encoding='utf-8') as f, \
         concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(audit_one_series, item): item for item in items}
        for fut in concurrent.futures.as_completed(futures):
            item = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = {'series_id': item['series_id'], 'title': item['title'],
                          'fatal_error': f"{type(e).__name__}: {e}",
                          'trace': traceback.format_exc()[-1000:]}
            f.write(json.dumps(result) + "\n")
            f.flush()
            done += 1
            if done % 25 == 0 or done == len(items):
                elapsed = time.time() - t_start
                rate = done / elapsed if elapsed else 0
                eta = (len(items) - done) / rate if rate else 0
                print(f"[Audit] {done}/{len(items)} done "
                      f"({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)", flush=True)

    print(f"[Audit] Finished. Wrote {done} results to {out_path}", flush=True)


if __name__ == '__main__':
    main()
