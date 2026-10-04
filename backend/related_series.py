# backend/related_series.py
#
# Related series for the whole library: the sequels, prequels, spin-offs,
# side stories... MangaDex and Atsumaru list for every tracked series, kept
# in related_series / related_series_from (database.py). The other sites
# have no relations to read (AsuraScans, HiveToons, Flame Comics,
# Thunderscans and Comix only recommend, Kagane has nothing).
#
# They're read while the scheduler fetches a series' chapters (record()),
# from requests it makes anyway: Atsumaru's series page carries them, and
# MangaDex's status request - made for a primary source - returns them with
# includes[]=manga. A MangaDex source that isn't primary only fetches the
# chapter feed, so its relations cost one extra request, made at most once
# a day (needs_check()). "Check now" (scan_now) reads the whole library at
# once instead of waiting for each series' turn.
#
# A related series is remembered from the first check that finds it
# (first_seen_at), so one that turns up later - a newly announced sequel -
# is listed above everything else and counted as new until the list is next
# opened. What a series' first check finds is its starting point, not "new"
# (is_initial) - so adding a series doesn't flood the list.

import json
import threading
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from .database import get_db, release_db
from .source_links import parse_source_link
from .search_utils import normalize_search_text, comparable_titles

SOURCE_LABELS = {'mangadex': 'MangaDex', 'atsu': 'Atsumaru'}
# How often a non-primary MangaDex source's relations are looked up
EXTRA_CHECK_AGE = timedelta(days=1)
# "Check now"'s Atsumaru results are written every this many series, so the
# DB lock is never held long and progress isn't all lost to a restart
_ATSU_SAVE_EVERY = 50

_scan_lock = threading.Lock()
_state_lock = threading.Lock()
_state = {'running': False, 'source': None, 'done': 0, 'total': 0, 'error': None}


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _get_meta(cursor, key):
    cursor.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else None


def _set_meta(key, value):
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    finally:
        release_db(conn)


def _set_state(**changes):
    with _state_lock:
        _state.update(changes)


def scan_state():
    """The "Check now" run's progress, and how much of the library has had
    its relations read so far: {running, source, done, total, error,
    checked_series, total_series, last_checked}."""
    with _state_lock:
        state = dict(_state)
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT series_id) FROM series_sources WHERE source_type IN ('mangadex', 'atsu')")
        state['total_series'] = cursor.fetchone()[0]
        cursor.execute("""
            SELECT COUNT(DISTINCT rc.series_id), MAX(rc.checked_at) FROM related_checked rc
            JOIN series_sources ss ON ss.series_id = rc.series_id AND ss.source_type = rc.source_type
        """)
        state['checked_series'], state['last_checked'] = cursor.fetchone()
    finally:
        release_db(conn)
    return state


# ── Recording ────────────────────────────────────────────────────

def _prepare(source_type, related):
    """Links for a site's related list, and Atsumaru's covers: its relations
    carry the cover's path, kept as a link to our /atsu-cover route, which
    downloads it the first time it's shown. (MangaDex's relations come
    without covers - fill_covers() looks them up, only when they're shown.)"""
    for item in related:
        if source_type == 'mangadex':
            item['url'] = f"https://mangadex.org/title/{item['id']}"
        else:
            item['url'] = f"https://atsu.moe/manga/{item['id']}"
            if item.get('poster'):
                item['cover_url'] = f"/api/related-series/atsu-cover?path={quote(item['poster'])}"
    return related


def _save(source_type, results):
    """Store what one site said for the tracked series in `results`
    ({series_id: [related]}, each related {id, url, relation, title, status,
    medium, created_at}): their relations from this site are
    replaced; a related series already known keeps its first_seen_at. For a
    series checked on this site for the first time, what's new to the list
    is its starting point (is_initial), not "new"."""
    if not results:
        return
    now = _now()
    conn = get_db()
    ok = False
    try:
        cursor = conn.cursor()
        series_ids = list(results)
        checked_before = set()
        for start in range(0, len(series_ids), 500):
            chunk = series_ids[start:start + 500]
            marks = ','.join('?' * len(chunk))
            cursor.execute(f"SELECT series_id FROM related_checked WHERE source_type = ? AND series_id IN ({marks})",
                           [source_type, *chunk])
            checked_before.update(row[0] for row in cursor.fetchall())
            cursor.execute(f"DELETE FROM related_series_from WHERE source_type = ? AND series_id IN ({marks})",
                           [source_type, *chunk])
        for series_id, related in results.items():
            initial = series_id not in checked_before
            for item in related:
                cursor.execute("""
                    INSERT INTO related_series (source_type, source_id, url, title, cover_url, status, medium,
                                                source_created_at, first_seen_at, is_initial)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_type, source_id) DO UPDATE SET
                        url = excluded.url,
                        title = excluded.title,
                        cover_url = COALESCE(excluded.cover_url, cover_url),
                        status = COALESCE(excluded.status, status),
                        medium = COALESCE(excluded.medium, medium),
                        source_created_at = COALESCE(excluded.source_created_at, source_created_at)
                """, (source_type, item['id'], item['url'], item['title'], item.get('cover_url'), item.get('status'),
                      item.get('medium'), item.get('created_at'), now, 1 if initial else 0))
                cursor.execute("SELECT id FROM related_series WHERE source_type = ? AND source_id = ?",
                               (source_type, item['id']))
                related_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT OR IGNORE INTO related_series_from (related_id, series_id, relation, source_type) VALUES (?, ?, ?, ?)",
                    (related_id, series_id, item['relation'], source_type))
            cursor.execute("INSERT OR REPLACE INTO related_checked (series_id, source_type, checked_at) VALUES (?, ?, ?)",
                           (series_id, source_type, now))
        ok = True
    finally:
        release_db(conn, commit=ok)
    schedule_tag_sync()


def record(series_id, source_type, related):
    """The relations a site reported for a tracked series while its
    chapters were fetched. Never raises - the chapter scan mustn't fail
    over its Related list."""
    try:
        _save(source_type, {series_id: _prepare(source_type, related or [])})
    except Exception as e:
        print(f"[Related] Couldn't record {source_type} relations for series {series_id}: {e}")


_reading_soon = set()
_reading_soon_lock = threading.Lock()


def read_relations_soon(series_id, delay=10):
    """Read a just-added series' relations from its MangaDex / Atsumaru
    sources in the background, instead of waiting for its first scheduled
    scan (hours away for Plan to Read). The delay lets the extra links
    pasted with it be attached first, so one read covers them all."""
    with _reading_soon_lock:
        if series_id in _reading_soon:
            return
        _reading_soon.add(series_id)

    def read():
        with _reading_soon_lock:
            _reading_soon.discard(series_id)
        from .trackers import mangadex, atsu
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT source_url, source_type FROM series_sources WHERE series_id = ? AND source_type IN ('mangadex', 'atsu')",
                           (series_id,))
            sources = cursor.fetchall()
        finally:
            release_db(conn)
        for url, source_type in sources:
            key = parse_source_link(url)
            if not key:
                continue
            try:
                related = (mangadex.get_status_and_related(key[1])[1] if source_type == 'mangadex'
                           else atsu.get_related(key[1]))
            except Exception as e:
                print(f"[Related] Couldn't read {SOURCE_LABELS[source_type]} relations for new series {series_id}: {e}")
                continue
            record(series_id, source_type, related)

    timer = threading.Timer(delay, read)
    timer.daemon = True
    timer.start()


def needs_check(series_id, source_type, max_age=EXTRA_CHECK_AGE):
    """Whether a series' relations on a site are older than `max_age` (or
    were never read) - for the checks that cost an extra request."""
    try:
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT checked_at FROM related_checked WHERE series_id = ? AND source_type = ?",
                           (series_id, source_type))
            row = cursor.fetchone()
        finally:
            release_db(conn)
        if not row:
            return True
        at = datetime.strptime(row[0], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - at >= max_age
    except Exception:
        return False


# ── "Has Related" custom tag ─────────────────────────────────────
# Series related to another series you track get this custom tag - both
# ends of the link (Solo Leveling and Solo Leveling: Ragnarok), whatever
# the relation - so the dashboard's Custom Tags filter finds them. It's
# kept in step both ways (removed once no tracked series is related any
# more), shortly after relations change - one sync at a time, never more
# often than every RELATED_TAG_DELAY seconds. The tag is known by id (meta
# related_tag_id), so it can be renamed; deleting it turns the
# auto-tagging off for good (it isn't recreated).

RELATED_TAG_NAME = 'Has Related'
RELATED_TAG_DELAY = 20
_tag_sync_lock = threading.Lock()
_tag_sync_timer = None


def _related_tag_id():
    """The tag's id - created the first time; None once it's been deleted."""
    from .database import create_custom_tag
    conn = get_db()
    try:
        cursor = conn.cursor()
        stored = _get_meta(cursor, 'related_tag_id')
        if stored:
            cursor.execute("SELECT id FROM custom_tags WHERE id = ?", (int(stored),))
            row = cursor.fetchone()
            return row[0] if row else None
    finally:
        release_db(conn)
    tag_id = create_custom_tag(RELATED_TAG_NAME)
    if tag_id:
        _set_meta('related_tag_id', str(tag_id))
    return tag_id


def sync_related_tag():
    """Put the tag on exactly the series that should have it."""
    tag_id = _related_tag_id()
    if not tag_id:
        return
    want = set()
    for group in get_related_list()['series']:
        for item in group['related']:
            if item['tracked']:
                want.update((group['series_id'], item['tracked']['id']))
    conn = get_db()
    ok = False
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT series_id FROM series_custom_tags WHERE tag_id = ?", (tag_id,))
        have = {row[0] for row in cursor.fetchall()}
        cursor.executemany("INSERT OR IGNORE INTO series_custom_tags (series_id, tag_id) VALUES (?, ?)",
                           [(series_id, tag_id) for series_id in want - have])
        cursor.executemany("DELETE FROM series_custom_tags WHERE series_id = ? AND tag_id = ?",
                           [(series_id, tag_id) for series_id in have - want])
        ok = True
    finally:
        release_db(conn, commit=ok)
    if want != have:
        print(f"[Related] '{RELATED_TAG_NAME}' tag: +{len(want - have)} -{len(have - want)} series")


def _run_tag_sync():
    global _tag_sync_timer
    with _tag_sync_lock:
        _tag_sync_timer = None
    try:
        sync_related_tag()
    except Exception as e:
        print(f"[Related] '{RELATED_TAG_NAME}' tag sync failed: {e}")


def schedule_tag_sync(delay=RELATED_TAG_DELAY):
    """Sync the tag in `delay` seconds - unless one is already waiting to,
    so a scan recording series after series syncs once, not per series."""
    global _tag_sync_timer
    with _tag_sync_lock:
        if _tag_sync_timer is not None:
            return
        _tag_sync_timer = threading.Timer(delay, _run_tag_sync)
        _tag_sync_timer.daemon = True
        _tag_sync_timer.start()


def was_checked(series_id):
    """Whether a series' relations were ever read (from any site)."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM related_checked WHERE series_id = ? LIMIT 1", (series_id,))
        return cursor.fetchone() is not None
    finally:
        release_db(conn)


# ── "Check now": the whole library at once ───────────────────────

def _library_sources(source_type):
    """{site's own series id: [tracked series ids]} for one site."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT series_id, source_url FROM series_sources WHERE source_type = ?", (source_type,))
        rows = cursor.fetchall()
    finally:
        release_db(conn)
    by_site_id = {}
    for series_id, url in rows:
        key = parse_source_link(url)
        if key and key[0] == source_type:
            by_site_id.setdefault(key[1], []).append(series_id)
    return by_site_id


def _scan_mangadex():
    from .trackers import mangadex
    library = _library_sources('mangadex')
    ids = list(library)
    _set_state(source='mangadex', done=0, total=len(ids))
    results = {}
    for start in range(0, len(ids), 100):
        for manga_id, related in mangadex.get_related_batch(ids[start:start + 100]).items():
            _prepare('mangadex', related)
            for series_id in library.get(manga_id, []):
                results.setdefault(series_id, []).extend(related)
        _set_state(done=min(start + 100, len(ids)))
    _save('mangadex', results)
    print(f"[Related] MangaDex: {len(ids)} series checked, {sum(len(r) for r in results.values())} relations")


def _scan_atsu():
    from .trackers import atsu
    library = _library_sources('atsu')
    ids = list(library)
    _set_state(source='atsu', done=0, total=len(ids))
    results, failures = {}, 0
    for i, manga_id in enumerate(ids, 1):
        try:
            related = _prepare('atsu', atsu.get_related(manga_id))
            for series_id in library[manga_id]:
                results.setdefault(series_id, []).extend(related)
        except Exception as e:
            failures += 1
            print(f"[Related] Atsumaru {manga_id} failed: {e}")
        if i % _ATSU_SAVE_EVERY == 0 or i == len(ids):
            _save('atsu', results)
            results = {}
        _set_state(done=i)
    print(f"[Related] Atsumaru: {len(ids)} series checked, {failures} failed")


_SCANNERS = {'mangadex': _scan_mangadex, 'atsu': _scan_atsu}


def run_scan():
    """Read every tracked series' relations from both sites now. Returns
    False, doing nothing, when a run is already going."""
    if not _scan_lock.acquire(blocking=False):
        return False
    try:
        _set_state(running=True, error=None)
        for source, scanner in _SCANNERS.items():
            try:
                scanner()
            except Exception as e:
                print(f"[Related] {SOURCE_LABELS[source]} check failed: {e}")
                _set_state(error=f"{SOURCE_LABELS[source]}: {e}")
        return True
    finally:
        _set_state(running=False, source=None)
        _scan_lock.release()


def scan_now():
    """Start run_scan() in the background; False if one is running."""
    if _scan_lock.locked():
        return False
    threading.Thread(target=run_scan, daemon=True).start()
    return True


# ── Reading ──────────────────────────────────────────────────────

# (title, title_en, title_romaji, alt_titles) -> its normalised titles.
# Normalising the whole library's ~24k titles was nearly all of the time
# Series Settings' Related button waited on; a series' row only needs it
# again once one of its titles changes.
_normalised_titles = {}


def _library_titles(cursor):
    """{normalised title: series id} for every tracked series' titles - so
    a related series tracked from another site (not the link it's listed
    with) is still found in the library."""
    global _normalised_titles
    cursor.execute("SELECT id, title, title_en, title_romaji, alt_titles FROM series ORDER BY id")
    titles = {}
    cache = {}
    for series_id, title, title_en, title_romaji, alt_titles in cursor.fetchall():
        key = (title, title_en, title_romaji, alt_titles)
        normalised = _normalised_titles.get(key)
        if normalised is None:
            try:
                alts = json.loads(alt_titles) if alt_titles else []
            except (ValueError, TypeError):
                alts = []
            normalised = comparable_titles([title, title_en, title_romaji, *(alts if isinstance(alts, list) else [])])
        cache[key] = normalised
        for title_key in normalised:
            titles.setdefault(title_key, series_id)
    # rebuilt each time, so titles no longer in the library drop out
    _normalised_titles = cache
    return titles


def get_related_list(series_id=None):
    """The tracked series that have related series, each with its related
    ones (the same one from both sites merged, with both links) - those you
    track too are included, with `tracked` set:
    {series: [{series_id, title, cover_url, status (your reading status),
    added_at (when you added it),
    sources: [{url, source_type}] (primary first), hidden_ids, is_new, related:
    [{ids, title, relations, status, medium, links: [{url, source,
    source_label}], cover_url (None: not looked up yet - see fill_covers),
    first_seen_at, source_created_at, is_initial, is_new, tracked: None or
    {id, title, status}}]}], seen_at}
    hidden_ids is None, or the related_series ids the series had when it was
    hidden - it's hidden only while it has nothing else. Newest first: a
    related series that turned up after its series' first check (latest
    first), then the rest by when the site added them - within a series and
    for the series themselves (by their newest related); the tracked ones
    after the rest, and never new."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        seen_at = _get_meta(cursor, 'related_seen_at')
        cursor.execute("SELECT series_id, source_url FROM series_sources ORDER BY series_id")
        tracked_links = {}
        for tracked_id, source_url in cursor.fetchall():
            tracked_links.setdefault(parse_source_link(source_url), tracked_id)
        library_titles = _library_titles(cursor)
        cursor.execute("SELECT id, title, status FROM series")
        library = {row[0]: {'id': row[0], 'title': row[1], 'status': row[2]} for row in cursor.fetchall()}
        cursor.execute("SELECT series_id, related_ids FROM related_hidden")
        hidden = {}
        # not `series_id` - that's the filter the query below uses
        for hidden_id, related_ids in cursor.fetchall():
            try:
                hidden[hidden_id] = json.loads(related_ids)
            except (ValueError, TypeError):
                pass
        cursor.execute("""
            SELECT r.id, r.source_type, r.source_id, r.url, r.title, r.cover_url, r.status, r.medium,
                   r.source_created_at, r.first_seen_at, r.is_initial,
                   f.series_id, f.relation, s.title, s.cover_url, s.status, s.created_at
            FROM related_series r
            JOIN related_series_from f ON f.related_id = r.id
            JOIN series s ON s.id = f.series_id
            -- only from a site the series still has a source on (a removed
            -- source isn't read again, so its relations would linger)
            WHERE EXISTS (SELECT 1 FROM series_sources ss
                          WHERE ss.series_id = f.series_id AND ss.source_type = f.source_type)
              AND (? IS NULL OR f.series_id = ?)
            ORDER BY r.id
        """, (series_id, series_id))
        rows = cursor.fetchall()
        cursor.execute("SELECT series_id, source_url, source_type FROM series_sources ORDER BY is_primary DESC, id")
        series_sources = {}
        for source_series_id, url, source_type in cursor.fetchall():
            series_sources.setdefault(source_series_id, []).append({'url': url, 'source_type': source_type})
    finally:
        release_db(conn)

    groups = {}  # series_id -> group
    for (rid, source_type, source_id, url, title, cover_url, status, medium, created_at, first_seen, is_initial,
         series_id, relation, series_title, series_cover, series_status, series_added) in rows:
        title_key = normalize_search_text(title) or f'{source_type}:{source_id}'
        tracked_id = tracked_links.get((source_type, source_id)) or library_titles.get(title_key)
        # a relation to itself (its own colored edition under the same title...)
        if tracked_id == series_id:
            continue
        group = groups.get(series_id)
        if group is None:
            group = groups[series_id] = {
                'series_id': series_id, 'title': series_title, 'cover_url': series_cover,
                'status': series_status, 'added_at': str(series_added) if series_added else None,
                'sources': series_sources.get(series_id, []),
                'hidden_ids': hidden.get(series_id), 'related': [], '_by_id': {}, '_by_key': {},
            }
        item = group['_by_id'].get(rid)
        if item is None:
            # The same title from the other site is the same series - but two
            # entries of one site aren't, whatever their titles (MangaDex has
            # a dozen doujinshi of one series all called "Untitled")
            item = next((other for other in group['_by_key'].get(title_key, [])
                         if all(link['source'] != source_type for link in other['links'])), None)
            if item is None:
                item = {
                    'ids': [], 'title': title, 'cover_url': None, 'relations': [], 'status': None, 'medium': None,
                    'links': [], 'first_seen_at': first_seen, 'source_created_at': None, 'is_initial': True,
                    'tracked': None,
                }
                group['_by_key'].setdefault(title_key, []).append(item)
                group['related'].append(item)
            group['_by_id'][rid] = item
        if relation not in item['relations']:
            item['relations'].append(relation)
        if rid in item['ids']:
            continue
        item['ids'].append(rid)
        # MangaDex's title and cover win (it prefers an English title; its
        # covers load straight from its CDN). None: not looked up yet, '': none
        if source_type == 'mangadex':
            item['title'] = title
            if cover_url or item['cover_url'] is None:
                item['cover_url'] = cover_url
        elif not item['cover_url']:
            item['cover_url'] = cover_url or item['cover_url']
        item['status'] = item['status'] or status
        item['medium'] = item['medium'] or medium
        item['links'].append({'url': url, 'source': source_type, 'source_label': SOURCE_LABELS[source_type]})
        item['first_seen_at'] = min(item['first_seen_at'], first_seen)
        if created_at and (not item['source_created_at'] or created_at > item['source_created_at']):
            item['source_created_at'] = created_at
        item['is_initial'] = item['is_initial'] and bool(is_initial)
        if tracked_id and not item['tracked']:
            item['tracked'] = library.get(tracked_id)

    def newest_first(entries, found, created):
        # site's date, newest first (undated last) ...
        entries.sort(key=lambda e: created(e) or '', reverse=True)
        # ... under everything found since the first check, latest first
        entries.sort(key=lambda e: found(e) or '', reverse=True)

    series = list(groups.values())
    for group in series:
        del group['_by_id'], group['_by_key']
        for item in group['related']:
            item['is_new'] = (not item['tracked'] and not item['is_initial']
                              and (not seen_at or item['first_seen_at'] > seen_at))
        newest_first(group['related'],
                     lambda item: None if item['is_initial'] else item['first_seen_at'],
                     lambda item: item['source_created_at'])
        # the ones you track after the rest
        group['related'].sort(key=lambda item: bool(item['tracked']))
        group['is_new'] = any(item['is_new'] for item in group['related'])
    newest_first(series,
                 lambda g: max((i['first_seen_at'] for i in g['related'] if not i['is_initial'] and not i['tracked']), default=None),
                 lambda g: max((i['source_created_at'] or '' for i in g['related'] if not i['tracked']), default=None))
    return {'series': series, 'seen_at': seen_at}


def fill_covers(related_ids):
    """Covers for the related series (related_series ids) about to be
    shown: MangaDex's are looked up now - up to 100 per request, only those
    never looked up - and stored ('' when it has none), so it's once per
    series. Returns {id: cover_url} for every id asked about."""
    from .trackers import mangadex
    ids = [int(i) for i in related_ids][:300]
    if not ids:
        return {}
    conn = get_db()
    try:
        cursor = conn.cursor()
        marks = ','.join('?' * len(ids))
        cursor.execute(f"SELECT id, source_type, source_id, cover_url FROM related_series WHERE id IN ({marks})", ids)
        rows = cursor.fetchall()
    finally:
        release_db(conn)
    covers = {rid: cover for rid, _, _, cover in rows if cover is not None}
    need = {source_id: rid for rid, source_type, source_id, cover in rows
            if cover is None and source_type == 'mangadex'}
    found, looked_up = {}, set()
    manga_ids = list(need)
    for start in range(0, len(manga_ids), 100):
        batch = manga_ids[start:start + 100]
        try:
            found.update(mangadex.get_covers_for_manga(batch))
            looked_up.update(batch)
        except Exception as e:
            print(f"[Related] MangaDex covers for {len(batch)} related series failed: {e}")
    updates = []
    for manga_id in looked_up:
        # the 256px thumbnail - the rows show them small
        cover = f"{found[manga_id]}.256.jpg" if manga_id in found else ''
        covers[need[manga_id]] = cover
        updates.append((cover, need[manga_id]))
    if updates:
        conn = get_db()
        ok = False
        try:
            conn.executemany("UPDATE related_series SET cover_url = ? WHERE id = ?", updates)
            ok = True
        finally:
            release_db(conn, commit=ok)
    return covers


def mark_seen():
    _set_meta('related_seen_at', _now())


def hide_series(series_id, related_ids):
    """Hide a tracked series from the Related list, remembering the related
    series (ids) it has now - another one turning up brings it back."""
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO related_hidden (series_id, related_ids, hidden_at) VALUES (?, ?, ?)",
                     (series_id, json.dumps(sorted(set(related_ids))), _now()))
    finally:
        release_db(conn)


def unhide_series(series_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM related_hidden WHERE series_id = ?", (series_id,))
    finally:
        release_db(conn)
