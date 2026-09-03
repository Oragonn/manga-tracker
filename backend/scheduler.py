import threading
import time
import sqlite3
import os
import concurrent.futures
from datetime import datetime, timezone, timedelta
from .database import get_db, release_db
from .trackers.mangadex import extract_manga_id, get_latest_chapters
from .trackers.kagane import extract_series_id, get_series_info
from .trackers import atsu as atsu_tracker
from .trackers import asura as asura_tracker
from .backup_manager import BackupManager
from .series_backup_manager import SeriesBackupManager

class MangaScheduler:
    def __init__(self):
        self.active = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.cleanup_thread = threading.Thread(target=self._cleanup_logs, daemon=True)

        # ✅ FIX: Use absolute paths
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(project_root, "data", "tracker.db")
        backups_root = os.path.join(project_root, "backups")

        self.backup_manager = BackupManager(
            db_path=db_path,
            backup_dir=os.path.join(backups_root, "database"),
            backup_interval_hours=1,  # Backup every hour
            retention_days=7,         # Keep for 7 days
            max_size_mb=2048          # ...but never let total size pass 2 GB either
        )

        self.series_backup_manager = SeriesBackupManager(
            backup_dir=os.path.join(backups_root, "series_csv"),
            backup_interval_hours=24,  # Kenmei-style CSV snapshot once a day
            retention_days=30
        )

        # Per-status fetch state for the /scheduler page - tracked in memory
        # since none of this (last activity, live scan progress) needs to
        # survive a restart, unlike everything else the scheduler touches.
        #
        # "How many are left to fetch" is deliberately NOT tracked here at
        # all - get_status_summary() derives it live from the DB every call
        # (last_check vs. each status's interval), so it can never drift out
        # of sync with reality the way an in-memory counter can.
        #
        # What IS tracked here is just scan progress, for the progress bar,
        # in two independent tracks merged for display in
        # get_status_summary():
        #  - manual_* : a "Scan Now" run - a single fixed batch of series ids
        #    captured up front, so current/total is exact for its whole life.
        #  - background_* : one background tick's batch for this status.
        #    Deliberately scoped to a single tick (not a multi-tick "wave"):
        #    due series are snapshotted once at tick start, so this is always
        #    an exact, bounded count/total with nothing to reconcile against
        #    new arrivals - anything that becomes due mid-tick simply isn't
        #    part of this tick's batch and waits for the next one, which
        #    starts its own fresh background_progress_current/total at 0.
        self._status_lock = threading.Lock()
        self._status_state = {
            status: {
                'last_scanned_at': None,
                'manual_scanning': False,
                'manual_progress_current': 0,
                'manual_progress_total': 0,
                'background_scanning': False,
                'background_progress_current': 0,
                'background_progress_total': 0,
                # Whole-status sweep tracking for the "Left to Fetch" stat -
                # separate from due_count ("Due Now") and from the per-tick
                # background_* progress bar above. sweep_active latches True
                # the moment something is due or a scan starts, and stays
                # True across the quiet gaps between due sub-batches (not
                # every series in a status crosses its threshold at once) -
                # it only goes False once swept_ids covers every current id
                # in the status, i.e. a full lap is actually done. While
                # inactive, "Left to Fetch" reports 0 instead of jumping to
                # total_series with nothing happening.
                'sweep_active': False,
                'swept_ids': set(),
                # Caps the displayed "Due Now" number while a scan is in
                # flight, so it can only fall (as fetches complete) rather
                # than also rising (as new series cross their own interval
                # mid-scan) - the two effects landing on the same number at
                # once is what reads as "not really decreasing". Captured
                # fresh the moment a scan starts, cleared the moment it
                # ends so the display re-syncs to the true live count
                # (which may jump up) while nothing is actively fetching.
                'due_count_ceiling': None,
                # Set by cancel_status(). While True, get_status_summary()
                # forces Due Now/Left to Fetch/scanning/progress to 0 for
                # this status regardless of live state. Purely a DISPLAY
                # override - cleared the moment this status next actually
                # starts scanning (manual or background), same as before.
                # It is NOT what protects against stale completions (see
                # scan_generation below) - a bool can't tell "this old run"
                # apart from "a newer run that already started", which is
                # exactly the gap that let a straggling completion from an
                # already-cancelled run get miscounted into whatever scan
                # was active by the time it finally finished.
                'cancelled': False,
                # Bumped every time a fresh scan starts for this status
                # (background tick or manual Scan Now) AND on every
                # cancel_status() call. Each tick/run captures its own
                # generation number locally and compares it against the
                # live value before applying any completion to shared
                # state - a completion whose captured generation no longer
                # matches is from a superseded run (cancelled, or simply an
                # old tick that's still draining an unkillable in-flight
                # fetch) and is ignored, no matter what's running now. This
                # is what actually prevents cross-run bleed; 'cancelled'
                # alone can't, since it gets cleared as soon as anything
                # new starts.
                'scan_generation': 0,
            }
            for status in ('reading', 'plan_to_read', 'on_hold', 'dropped', 'completed')
        }
        # Futures currently submitted for each status, so cancel_status()
        # can cancel the ones that haven't started running yet (a queued
        # Future can be cancelled outright; one already mid-fetch can't be
        # force-killed - no cross-thread interrupt in Python - so it just
        # finishes naturally in the background, its result ignored because
        # 'cancelled' is checked before that status's state gets updated).
        self._active_futures = {
            status: set()
            for status in ('reading', 'plan_to_read', 'on_hold', 'dropped', 'completed')
        }

    def _cleanup_logs(self):
        """Background thread to cleanup old activity logs every 24 hours."""
        while self.active:
            try:
                from .activity_logger import cleanup_old_logs
                cleanup_old_logs()
                # Sleep for 24 hours
                time.sleep(86400)
            except Exception as e:
                print(f"[Cleanup] Error: {e}")
                time.sleep(3600)  # Retry in 1 hour on error

    def _get_kagane_series_ids(self):
        """Series ids with at least one Kagane source. The Kagane browser
        client (camoufox_kagane.py) is a singleton with its own internal
        lock serializing every fetch (each can take up to ~40s waiting out
        a Cloudflare challenge) - submitting several to the general 8-way
        pool just wastes worker slots blocked on that lock instead of doing
        real parallel work for the other trackers, so these get routed to
        their own single-worker lane instead."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT series_id FROM series_sources WHERE source_type = 'kagane'")
        ids = {r[0] for r in cursor.fetchall()}
        release_db(conn)
        return ids

    def get_check_interval(self, status):
        intervals = {
            'reading': 1800,
            'plan_to_read': 10800,
            'on_hold': 86400,
            'dropped': 604800,
            'completed': 604800
        }
        return intervals.get(status, 604800)

    # Lower number = scanned first. A freshly bulk-imported library (or a
    # restart after being offline a while) can put hundreds/thousands of
    # series in "due" state simultaneously (every series starts with
    # last_check=NULL, which is always due); without this, the time-sensitive
    # "reading" tier would just wait behind whatever order the DB happens to
    # return, potentially for a long time.
    STATUS_SCAN_PRIORITY = {
        'reading': 0,
        'on_hold': 1,
        'plan_to_read': 2,
        'dropped': 3,
        'completed': 3,
    }

    # Series are scanned this many at a time per tick (each series itself
    # already fans out to its own sources concurrently inside scan_series).
    # At ~1.5-2s per series scanned one at a time, a status whose series all
    # share a last_check timestamp (e.g. right after a restart) could take
    # longer to clear than its own check interval, permanently falling
    # behind - 8-way concurrency here keeps a few hundred series comfortably
    # inside a single 60s tick instead of dribbling out over many ticks.
    CONCURRENT_SCAN_WORKERS = 8

    def _run(self):
        from .database import get_db, release_db  # Ensure consistent import
        while self.active:
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("SELECT id, status, last_check FROM series")
                series_list = cursor.fetchall()
                release_db(conn)

                now = datetime.now(timezone.utc)

                due = []
                for (sid, status, last_check) in series_list:
                    if last_check:
                        last = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                        next_check = last + timedelta(seconds=self.get_check_interval(status))
                        if now < next_check:
                            continue
                    due.append((sid, status, last_check))

                # Reading first, then most-overdue within the same status
                # (NULL last_check -- never checked -- sorts as oldest).
                due.sort(key=lambda row: (
                    self.STATUS_SCAN_PRIORITY.get(row[1], 4),
                    row[2] or ''
                ))

                # Snapshot this tick's batch size per status for the
                # /scheduler page's progress bar. Scoped to just this tick -
                # see the __init__ comment on background_* for why. Skip any
                # status a manual Scan Now already owns, so this doesn't
                # corrupt or prematurely end that run.
                due_counts_by_status = {}
                for _, status, _ in due:
                    due_counts_by_status[status] = due_counts_by_status.get(status, 0) + 1

                # Each status due this tick gets its own generation number,
                # captured locally - see __init__'s comment on
                # scan_generation for why a boolean 'cancelled' flag alone
                # isn't enough to protect against a completion that's still
                # trickling in from an already-superseded run.
                tick_generation = {}
                with self._status_lock:
                    for status, state in self._status_state.items():
                        if state['manual_scanning']:
                            continue
                        count = due_counts_by_status.get(status, 0)
                        state['background_scanning'] = count > 0
                        state['background_progress_current'] = 0
                        state['background_progress_total'] = count
                        if count > 0:
                            # A fresh tick's worth of work for this status is
                            # about to start - a prior cancel_status() call
                            # only meant "stop what was happening then", not
                            # "never scan this status again".
                            state['cancelled'] = False
                            state['scan_generation'] += 1
                            self._active_futures[status] = set()
                        tick_generation[status] = state['scan_generation']

                kagane_ids = self._get_kagane_series_ids()

                tick_start = time.time()
                submitted = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.CONCURRENT_SCAN_WORKERS) as executor, \
                     concurrent.futures.ThreadPoolExecutor(max_workers=1) as kagane_executor:
                    future_to_item = {}
                    for sid, status, last_check in due:
                        if not self.active:
                            break
                        pool = kagane_executor if sid in kagane_ids else executor
                        future = pool.submit(self.scan_series, sid)
                        future_to_item[future] = (sid, status)
                        if status in self._active_futures:
                            with self._status_lock:
                                self._active_futures[status].add(future)
                        submitted += 1

                    for future in concurrent.futures.as_completed(future_to_item):
                        sid, status = future_to_item[future]
                        with self._status_lock:
                            current_gen = self._status_state[status]['scan_generation'] \
                                if status in self._status_state else None
                        if current_gen != tick_generation.get(status):
                            # This tick's generation for this status has
                            # moved on (cancelled, or superseded by a manual
                            # scan) since this future was submitted - it's
                            # stale, ignore it regardless of what's running
                            # now.
                            continue
                        try:
                            future.result()
                        except Exception as e:
                            # scan_series() catches its own errors, so this is
                            # only for something unexpected escaping it (a
                            # cancelled-before-it-started future also raises
                            # here, harmlessly, as CancelledError).
                            print(f"[Scheduler] Unhandled error during concurrent scan: {e}")
                        if status in self._status_state:
                            with self._status_lock:
                                state = self._status_state[status]
                                if state['scan_generation'] != tick_generation.get(status):
                                    continue
                                state['last_scanned_at'] = \
                                    datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                                state['swept_ids'].add(sid)
                                if state['background_scanning']:
                                    state['background_progress_current'] += 1

                # This tick's batch is fully processed (the as_completed loop
                # above only returns once every submitted future is done) -
                # clear background_scanning so the progress bar disappears
                # between ticks instead of sitting at "N / N" until the next
                # one starts. Leaves manual Scan Now runs alone.
                with self._status_lock:
                    for status, state in self._status_state.items():
                        if state['manual_scanning']:
                            continue
                        state['background_scanning'] = False
                        state['background_progress_current'] = 0
                        state['background_progress_total'] = 0

                if submitted:
                    print(f"[Scheduler] Tick scanned {submitted} due series in "
                          f"{time.time() - tick_start:.1f}s ({self.CONCURRENT_SCAN_WORKERS} workers)")

                time.sleep(60)
            except Exception as e:
                print(f"[Scheduler] Background error: {e}")
                time.sleep(60)

    def _update_last_check(self, series_id, conn):
        timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        cursor = conn.cursor()
        cursor.execute("UPDATE series SET last_check = ? WHERE id = ?", (timestamp, series_id))

    def _process_chapters(self, series_id, chapters, conn):
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chapters WHERE series_id = ?", (series_id,))
        for ch in chapters:
            cursor.execute("""
                INSERT INTO chapters (
                    series_id, volume, raw_chapter, chapter_number,
                    release_date, chapter_url, is_oneshot, source_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                series_id,
                ch.get('volume'),
                ch.get('raw_chapter', str(ch['chapter_number'])),
                ch['chapter_number'],
                ch['release_date'],
                ch['chapter_url'],
                int(ch.get('is_oneshot', False)),
                ch.get('source_type')
            ))
        if chapters:
            latest_ch = max(ch['chapter_number'] for ch in chapters)
            latest_release = max(
                (ch['release_date'] for ch in chapters
                 if ch['chapter_number'] == latest_ch and ch['release_date']),
                default=''
            )
            cursor.execute("""
                UPDATE series
                SET latest_chapter = ?, latest_release = ?, total_chapters = ?, last_check = ?
                WHERE id = ?
            """, (
                latest_ch,
                latest_release,
                len(chapters),
                datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                series_id
            ))
        else:
            self._update_last_check(series_id, conn)
        conn.commit()

    def _fetch_source_chapters(self, source):
        """Fetch the chapter list for a single source. Runs on a worker
        thread from scan_series's ThreadPoolExecutor -- raises on failure
        so the caller's future.result() surfaces the error per-source."""
        source_url = source['source_url']
        source_type = source['source_type']

        print(f"[Scheduler] Fetching from {source_type}: {source_url}")

        if source_type == 'mangadex':
            manga_id = extract_manga_id(source_url)
            return get_latest_chapters(manga_id, limit=100) if manga_id else None
        elif source_type == 'kagane':
            kagane_id = extract_series_id(source_url)
            if not kagane_id:
                return None
            kagane_info = get_series_info(kagane_id)
            return kagane_info['chapters'] if kagane_info else None
        elif source_type == 'atsu':
            atsu_id = atsu_tracker.extract_series_id(source_url)
            if not atsu_id:
                return None
            atsu_info = atsu_tracker.get_series_info(atsu_id)
            return atsu_info['chapters'] if atsu_info else None
        elif source_type == 'asura':
            asura_id = asura_tracker.extract_series_id(source_url)
            if not asura_id:
                return None
            asura_info = asura_tracker.get_series_info(asura_id)
            return asura_info['chapters'] if asura_info else None
        return None

    def scan_series(self, series_id):
        """
        Scan all sources for a series and merge chapters.
        Fix for Bug #2: Now properly merges chapters from multiple sources.
        """
        try:
            from .database import get_series_sources, get_db, release_db
            
            # Get all sources for this series
            sources = get_series_sources(series_id)
            
            if not sources:
                print(f"[Scheduler] No sources found for series {series_id}")
                # *** FIX: Try to fetch from legacy source_url column ***
                conn_legacy = get_db()
                cursor_legacy = conn_legacy.cursor()
                cursor_legacy.execute("SELECT source_url FROM series WHERE id = ?", (series_id,))
                row = cursor_legacy.fetchone()
                release_db(conn_legacy)
                
                if row and row[0]:
                    print(f"[Scheduler] Found legacy source_url, creating source entry...")
                    # Create missing source entry
                    from .database import add_source_to_series
                    source_url = row[0]
                    source_type = 'mangadex' if 'mangadex.org' in source_url else 'kagane' if ('kagane.org' in source_url or 'kagane.to' in source_url) else 'atsu' if 'atsu.moe' in source_url else 'asura' if 'asurascans.com' in source_url else 'unknown'
                    add_source_to_series(series_id, source_url, source_type, is_primary=True)
                    # Retry getting sources
                    sources = get_series_sources(series_id)
                
                if not sources:
                    return
            
            all_chapters = []
            successful_sources = 0
            sources_reached = 0  # fetch didn't raise, whether or not it returned chapters

            # Fetch chapters from all sources concurrently instead of one at a
            # time -- each source is an independent network call, and the
            # merge step below is already order-independent (it compares
            # release dates rather than relying on iteration order).
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(sources))) as executor:
                future_to_source = {
                    executor.submit(self._fetch_source_chapters, source): source
                    for source in sources
                }
                from .database import record_source_success, record_source_failure

                for future in concurrent.futures.as_completed(future_to_source):
                    source = future_to_source[future]
                    source_type = source['source_type']
                    try:
                        chapters = future.result()
                    except Exception as source_error:
                        print(f"[Scheduler] Error fetching from {source_type}: {source_error}")
                        try:
                            record_source_failure(source['id'], str(source_error))
                        except Exception:
                            pass
                        # Kagane already logs its own scan failures (with
                        # more specific context) inside camoufox_kagane.py's
                        # browser client, several layers below this - only
                        # MangaDex/Atsu/Asura had no /errors visibility at
                        # all for an ongoing source failure, just the
                        # Source Alerts bell above.
                        if source_type != 'kagane':
                            try:
                                from .error_logger import log_error
                                conn_title = get_db()
                                cursor_title = conn_title.cursor()
                                cursor_title.execute("SELECT title FROM series WHERE id = ?", (series_id,))
                                row_title = cursor_title.fetchone()
                                release_db(conn_title)
                                title = row_title[0] if row_title else f"Series #{series_id}"
                                log_error(source['source_url'], str(source_error), series_title=title)
                            except Exception:
                                pass
                        continue

                    try:
                        record_source_success(source['id'])
                    except Exception:
                        pass
                    sources_reached += 1

                    if chapters:
                        # Tag chapters with source info
                        for ch in chapters:
                            ch['source_id'] = source['id']
                            ch['source_type'] = source_type
                            ch['source_url'] = source['source_url']
                        all_chapters.extend(chapters)
                        successful_sources += 1
                        print(f"[Scheduler] Got {len(chapters)} chapters from {source_type}")
                    else:
                        print(f"[Scheduler] No chapters from {source_type}")

            print(f"[Scheduler] Total raw chapters: {len(all_chapters)} from {successful_sources} sources")

            # Apply user corrections (ban a specific bad link, or
            # replace/inject a chapter) before merging. Runs regardless of
            # fetch success so a manual-only chapter still shows up even
            # during a total source outage. Bans target one exact
            # chapter_url rather than "this source's copy of this chapter
            # number" - a source like Atsumaru can have more than one
            # scanlator group's link for the same chapter number, and
            # banning one must not take the other down with it.
            from .database import get_chapter_overrides
            overrides = get_chapter_overrides(series_id)
            if overrides:
                banned_urls = {o['chapter_url'] for o in overrides if o['is_banned'] and o['chapter_url']}
                if banned_urls:
                    all_chapters = [
                        ch for ch in all_chapters
                        if ch['chapter_url'] not in banned_urls
                    ]
                for o in overrides:
                    if o['is_banned']:
                        continue
                    key = (o['source_type'], o['chapter_number'])
                    live_entries = [
                        ch for ch in all_chapters
                        if (ch['source_type'], ch['chapter_number']) == key
                    ]
                    all_chapters = [
                        ch for ch in all_chapters
                        if (ch['source_type'], ch['chapter_number']) != key
                    ]
                    # An override fixes the *link*; it shouldn't permanently
                    # freeze whatever release_date happened to be known at
                    # the moment it was saved. A source can report a
                    # chapter before it's finished timestamping it (e.g.
                    # AsuraScans briefly returning no date for a just-posted
                    # chapter) - prefer whatever this scan's live fetch says
                    # now, falling back to the override's stored date only
                    # if the source doesn't currently report this chapter
                    # at all, so the date self-heals on a later scan instead
                    # of staying blank forever.
                    live_release_date = next(
                        (e['release_date'] for e in live_entries if e.get('release_date')), None
                    )
                    all_chapters.append({
                        'chapter_number': o['chapter_number'],
                        'volume': o['volume'],
                        'raw_chapter': o['raw_chapter'] or str(o['chapter_number']),
                        'release_date': live_release_date or o['release_date'],
                        'chapter_url': o['chapter_url'],
                        'is_oneshot': o['is_oneshot'],
                        'source_id': None,
                        'source_type': o['source_type'],
                        'source_url': None,
                    })

            # *** FIX: Improved chapter merging logic ***
            if not all_chapters:
                conn = get_db()
                try:
                    if sources_reached == 0:
                        # Every source failed to fetch (outage, Cloudflare
                        # block, etc.) - this says nothing about whether
                        # chapters still exist, so don't touch them.
                        self._update_last_check(series_id, conn)
                    else:
                        # At least one source was actually reached and
                        # confirmed zero chapters - a legitimate empty
                        # result (e.g. a source with chapters was just
                        # removed and the remaining ones have none), not a
                        # failure. Clear stale chapters instead of leaving
                        # orphaned rows behind forever.
                        self._process_chapters(series_id, [], conn)
                finally:
                    release_db(conn)
                return
            
            # Merge chapters (deduplicate by chapter_number). Across
            # *different* sources, the most recent candidate wins outright -
            # both its link and its own release_date - so a different
            # source picking up the latest chapter correctly updates
            # last-release to that source's date. The "use the earliest
            # date" smoothing only applies *within* a single source's own
            # repost duplicates (e.g. a second Atsumaru scanlator group
            # reposting a chapter that's been out for a while under the
            # same chapter number) so that repost can't make an
            # already-released chapter look freshly dropped - it must not
            # reach across sources, or a source with an earlier (or just
            # differently-timestamped) copy can hijack last-release even
            # when a different source's copy is the one actually chosen.
            merged_chapters = {}
            earliest_dates = {}
            for ch in all_chapters:
                ch_num = ch['chapter_number']
                ch_date = ch.get('release_date') or ''
                dkey = (ch['source_type'], ch_num)
                if ch_date and (dkey not in earliest_dates or ch_date < earliest_dates[dkey]):
                    earliest_dates[dkey] = ch_date

                if ch_num not in merged_chapters:
                    merged_chapters[ch_num] = ch
                else:
                    # Keep the one with the most recent release date
                    existing = merged_chapters[ch_num]
                    existing_date = existing.get('release_date') or ''

                    # Safe comparison: treat None/empty as oldest
                    if ch_date and existing_date:
                        if ch_date > existing_date:
                            print(f"[Scheduler] Ch.{ch_num}: Using {ch['source_type']} (newer: {ch_date} vs {existing_date})")
                            merged_chapters[ch_num] = ch
                    elif ch_date and not existing_date:
                        # New chapter has date, existing doesn't -> prefer new
                        merged_chapters[ch_num] = ch
                    # else: keep existing (either both have no date, or existing has date and new doesn't)

            for ch_num, ch in merged_chapters.items():
                dkey = (ch['source_type'], ch_num)
                if dkey in earliest_dates:
                    ch['release_date'] = earliest_dates[dkey]

            # Convert back to list and sort
            final_chapters = list(merged_chapters.values())
            final_chapters.sort(key=lambda x: x['chapter_number'])
            
            print(f"[Scheduler] Final merged chapters: {len(final_chapters)}")
            
            # Save to database
            conn = get_db()
            try:
                self._process_chapters(series_id, final_chapters, conn)
                print(f"[Scheduler] Successfully saved {len(final_chapters)} chapters for series {series_id}")
            finally:
                release_db(conn)
                        
        except Exception as e:
            from .error_logger import log_error
            try:
                conn_err = get_db()
                cursor_err = conn_err.cursor()
                cursor_err.execute("SELECT title FROM series WHERE id = ?", (series_id,))
                row_err = cursor_err.fetchone()
                title = row_err[0] if row_err else f"Series #{series_id}"
                release_db(conn_err)
            except:
                title = f"Series #{series_id}"
            log_error(
                source_url=f"series:{series_id}",
                error_message=str(e),
                series_title=title
            )
            print(f"[Scheduler] Scan error for {series_id}: {e}")

            # Still bump last_check even though this scan failed unexpectedly -
            # otherwise a series that keeps throwing (bad data, a bug in a
            # tracker module, etc.) stays permanently "due", gets retried
            # every single tick forever, and pins the /scheduler page's
            # "next fetch" countdown at "Due now" indefinitely even though
            # every other series in its status is scanning normally.
            try:
                conn_fail = get_db()
                try:
                    self._update_last_check(series_id, conn_fail)
                finally:
                    release_db(conn_fail)
            except Exception as update_err:
                print(f"[Scheduler] Failed to update last_check after scan error for {series_id}: {update_err}")


    def get_status_summary(self):
        """Per-reading-status fetch info for the /scheduler page: how many
        series are due right now ("Due Now"), how many still haven't been
        fetched this sweep of the whole status ("Left to Fetch"), when the
        next one comes due, and any live scan-now progress."""
        from .database import get_db, release_db

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, status, last_check FROM series")
        rows = cursor.fetchall()
        release_db(conn)

        now = datetime.now(timezone.utc)
        rows_by_status = {status: [] for status in self._status_state}
        for sid, status, last_check in rows:
            if status in rows_by_status:
                rows_by_status[status].append((sid, last_check))

        result = {}
        with self._status_lock:
            for status, entries in rows_by_status.items():
                interval = self.get_check_interval(status)
                due_count = 0
                soonest_due_at = None
                for _, last_check in entries:
                    if last_check:
                        last = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                        due_at = last + timedelta(seconds=interval)
                    else:
                        due_at = now  # never checked -- due immediately
                    if due_at <= now:
                        due_count += 1
                    if soonest_due_at is None or due_at < soonest_due_at:
                        soonest_due_at = due_at

                state = self._status_state[status]

                if state['cancelled']:
                    # Forced to 0/idle by cancel_status() until this status
                    # next actually starts scanning - skip the normal
                    # due/sweep computation entirely so a live due_count
                    # doesn't immediately re-activate the sweep on the very
                    # next poll.
                    result[status] = {
                        'total_series': len(entries),
                        'due_count': 0,
                        'left_to_fetch': 0,
                        'last_scanned_at': state['last_scanned_at'],
                        'next_due_at': soonest_due_at.isoformat().replace('+00:00', 'Z') if soonest_due_at else None,
                        'scanning': False,
                        'progress_current': 0,
                        'progress_total': 0,
                    }
                    continue

                if state['manual_scanning']:
                    scanning = True
                    progress_current = state['manual_progress_current']
                    progress_total = state['manual_progress_total']
                elif state['background_scanning']:
                    scanning = True
                    progress_current = state['background_progress_current']
                    progress_total = state['background_progress_total']
                else:
                    scanning = False
                    progress_current = 0
                    progress_total = 0

                # "Left to Fetch": every series in this status minus the
                # ones already swept (fetched) this sweep, whole-status and
                # persisted across ticks/batches. Latches active the moment
                # there's something due or scanning, and stays active
                # through the quiet gaps between due sub-batches - only
                # deactivates once every current id has actually been
                # swept, reporting 0 (not total_series) while idle in
                # between sweeps.
                if due_count > 0 or scanning:
                    state['sweep_active'] = True

                current_ids = {sid for sid, _ in entries}
                remaining_ids = current_ids - state['swept_ids']
                if state['sweep_active'] and not remaining_ids and current_ids:
                    state['sweep_active'] = False
                    state['swept_ids'] = set()
                    remaining_ids = set()
                left_to_fetch = len(remaining_ids) if state['sweep_active'] else 0

                # "Due Now" display: a running minimum of the live due_count
                # while a scan is in flight, so it can only fall as fetches
                # complete - never rise mid-scan just because other series
                # cross their own interval in the meantime (that's the "two
                # effects landing on the same number at once" thrashing).
                # Cleared the instant nothing is scanning, so it still
                # catches back up to reality (up or down) between scans.
                if scanning:
                    if state['due_count_ceiling'] is None:
                        state['due_count_ceiling'] = due_count
                    else:
                        state['due_count_ceiling'] = min(state['due_count_ceiling'], due_count)
                    due_count_display = state['due_count_ceiling']
                else:
                    state['due_count_ceiling'] = None
                    due_count_display = due_count

                result[status] = {
                    'total_series': len(entries),
                    'due_count': due_count_display,
                    'left_to_fetch': left_to_fetch,
                    'last_scanned_at': state['last_scanned_at'],
                    'next_due_at': soonest_due_at.isoformat().replace('+00:00', 'Z') if soonest_due_at else None,
                    'scanning': scanning,
                    'progress_current': progress_current,
                    'progress_total': progress_total,
                }
        return result

    def scan_status_now(self, status):
        """Force-scan every series with this reading status right now,
        ignoring each one's normal check interval. Runs on a background
        thread so the triggering request returns immediately; progress is
        polled via get_status_summary(). Returns False if this status is
        already mid-scan (caller should refuse to start another)."""
        if status not in self._status_state:
            return False

        with self._status_lock:
            state = self._status_state[status]
            # Refuse if either a manual scan or the current background tick
            # already owns this status - avoids two scans hammering the same
            # sources at once.
            if state['manual_scanning'] or state['background_scanning']:
                return False
            state['manual_scanning'] = True
            state['manual_progress_current'] = 0
            state['manual_progress_total'] = 0
            # A fresh manual run is starting - a prior cancel_status() call
            # only meant "stop what was happening then". Bump the
            # generation so any straggling completion from an old run
            # (cancelled or otherwise superseded) can never be mistaken
            # for one of THIS run's - see __init__'s scan_generation note.
            state['cancelled'] = False
            state['scan_generation'] += 1
            my_generation = state['scan_generation']
            self._active_futures[status] = set()

        def _worker():
            try:
                from .database import get_db, release_db
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM series WHERE status = ?", (status,))
                series_ids = [r[0] for r in cursor.fetchall()]
                release_db(conn)

                with self._status_lock:
                    self._status_state[status]['manual_progress_total'] = len(series_ids)

                kagane_ids = self._get_kagane_series_ids()

                with concurrent.futures.ThreadPoolExecutor(max_workers=self.CONCURRENT_SCAN_WORKERS) as executor, \
                     concurrent.futures.ThreadPoolExecutor(max_workers=1) as kagane_executor:
                    future_to_sid = {}
                    for sid in series_ids:
                        if not self.active:
                            break
                        pool = kagane_executor if sid in kagane_ids else executor
                        future = pool.submit(self.scan_series, sid)
                        future_to_sid[future] = sid
                        with self._status_lock:
                            self._active_futures[status].add(future)

                    for future in concurrent.futures.as_completed(future_to_sid):
                        sid = future_to_sid[future]
                        with self._status_lock:
                            if self._status_state[status]['scan_generation'] != my_generation:
                                continue  # superseded (cancelled, or overtaken) - ignore
                        try:
                            future.result()
                        except Exception as e:
                            print(f"[Scheduler] Unhandled error during scan_status_now('{status}'): {e}")
                        with self._status_lock:
                            if self._status_state[status]['scan_generation'] != my_generation:
                                continue
                            self._status_state[status]['manual_progress_current'] += 1
                            self._status_state[status]['last_scanned_at'] = \
                                datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                            self._status_state[status]['swept_ids'].add(sid)
            except Exception as e:
                print(f"[Scheduler] scan_status_now('{status}') error: {e}")
            finally:
                with self._status_lock:
                    self._status_state[status]['manual_scanning'] = False

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def cancel_status(self, status):
        """Cancel this status's fetching only - no other status is
        affected. Stops the whole in-progress scan (manual or background),
        not just whatever's in the currently-running batch: every series
        for this status still queued (not yet started) is cancelled
        outright, and completions arriving after this point - including
        ones already mid-fetch when cancel was clicked, which Python can't
        force-kill mid-network-call - are simply ignored instead of being
        applied to this status's state. Due Now and Left to Fetch both
        drop to 0 immediately and stay there until this status next starts
        a scan on its own (manual or the next background tick due)."""
        if status not in self._status_state:
            return False

        with self._status_lock:
            state = self._status_state[status]
            state['cancelled'] = True
            # Invalidates every future already submitted for this status,
            # including ones already mid-fetch that we can't force-kill -
            # whenever they finally complete, their captured generation
            # won't match this new value, so _run()/scan_status_now() will
            # ignore them even if a completely different scan has started
            # for this status by then. This is what actually prevents a
            # stale completion from bleeding into a later run's counters
            # (see __init__'s scan_generation note) - 'cancelled' alone
            # can't, since it gets cleared as soon as anything new starts.
            state['scan_generation'] += 1
            state['manual_scanning'] = False
            state['background_scanning'] = False
            state['manual_progress_current'] = 0
            state['manual_progress_total'] = 0
            state['background_progress_current'] = 0
            state['background_progress_total'] = 0
            state['sweep_active'] = False
            state['swept_ids'] = set()
            state['due_count_ceiling'] = None
            futures = list(self._active_futures.get(status, ()))
            self._active_futures[status] = set()

        for future in futures:
            future.cancel()  # no-op (returns False) if already running/done
        return True

    def start_scanning(self):
        self.thread.start()
        self.cleanup_thread.start()
        self.backup_manager.start()
        self.series_backup_manager.start()

    def stop(self):
        self.active = False