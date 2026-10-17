# backend/source_links.py
#
# Recognises a pasted source link (a series page, or a chapter-reader URL, on
# any of the eight tracked sites) and finds the tracked series that has it
# attached, so the dashboard search box can take a link instead of a title.
#
# Deliberately doesn't import backend.trackers.*: kagane.py pulls in
# camoufox_kagane, which starts a headless browser the moment it's imported,
# and a plain search request must never trigger that. The patterns below
# mirror the extract_*_id() functions in those modules.

import json
import os
import re
import threading

_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
# The scheme is optional so a link pasted without "https://" still counts.
_PREFIX = r'(?:https?://)?(?:www\.)?'

# The address Kagane is reached on. The same site (same ids, same API) moves
# between domains: kagane.to stopped resolving in 2026-10, then kagane.org -
# the address Kagane tells people to bookmark - turned into a "Mirrors" page
# listing the working ones (its API still answered, but every series, reader
# and search page showed the list instead).
#
# So the address in use isn't fixed: it's kept in data/kagane_domain.json and
# moved by the Kagane site check (kagane_mirror.py) when the current one stops
# working or kagane.org serves the site again. Every request, every link built
# (chapter-reader links, search links, the site check) goes through
# kagane_domain() / kagane_base(), and stored links are rewritten to it on a
# move and at startup (database.move_kagane_links_to_current_domain).
KAGANE_HOME_DOMAIN = 'kagane.org'
KAGANE_DEFAULT_DOMAIN = 'kagane.si'
# Tried when the current address fails, after the ones kagane.org lists
KAGANE_KNOWN_DOMAINS = ('kagane.si', 'kagane.org', 'kagane.cx', 'kagane.moe', 'kagane.net', 'kagane.to')
# Any Kagane host - a mirror that appears later is recognised without a code change
KAGANE_HOST_PATTERN = r'kagane\.[a-z]{2,10}'
_KAGANE_DOMAIN = re.compile(r'^' + KAGANE_HOST_PATTERN + r'$')
_KAGANE_HOST = re.compile(r'^(?:https?://)?(?:www\.)?' + KAGANE_HOST_PATTERN + r'(?=[/?#:]|$)', re.I)

_kagane_lock = threading.Lock()
_kagane_state = None  # data/kagane_domain.json, loaded on first use


def _kagane_state_path():
    # Beside the database, like paused_sites.json: restoring a backup
    # doesn't send Kagane back to an address that may be dead
    from .database import DB_PATH
    return os.path.join(os.path.dirname(DB_PATH) or '.', 'kagane_domain.json')


def _load_kagane_state():
    global _kagane_state
    if _kagane_state is None:
        try:
            with open(_kagane_state_path(), encoding='utf-8') as f:
                data = json.load(f)
            _kagane_state = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            _kagane_state = {}
        except Exception as e:
            print(f"[Kagane] Couldn't read {_kagane_state_path()}: {e}")
            _kagane_state = {}
    return _kagane_state


def _save_kagane_state(state):
    global _kagane_state
    path = _kagane_state_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)
    _kagane_state = state


def is_kagane_domain(domain):
    return bool(_KAGANE_DOMAIN.match(domain or ''))


def kagane_state():
    """{'domain', 'previous', 'moved_at', 'reason', 'mirrors'} - whatever of
    it has been saved."""
    with _kagane_lock:
        return dict(_load_kagane_state())


def kagane_domain():
    """The Kagane domain in use right now, e.g. 'kagane.si'."""
    with _kagane_lock:
        domain = _load_kagane_state().get('domain')
    return domain if is_kagane_domain(domain) else KAGANE_DEFAULT_DOMAIN


def kagane_base():
    return f'https://{kagane_domain()}'


def set_kagane_domain(domain, reason):
    """Switch every Kagane request and newly built link to `domain`. Stored
    links are the caller's to move (kagane_mirror.move_to)."""
    if not is_kagane_domain(domain):
        raise ValueError(f"Not a Kagane domain: {domain!r}")
    from datetime import datetime, timezone
    with _kagane_lock:
        state = dict(_load_kagane_state())
        previous = state.get('domain') if is_kagane_domain(state.get('domain')) else KAGANE_DEFAULT_DOMAIN
        state.update(domain=domain, previous=previous, reason=reason,
                     moved_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
        _save_kagane_state(state)
    return previous


def remember_kagane_mirrors(mirrors):
    """Keep the mirror list kagane.org last showed, to fall back on if
    kagane.org itself can't be reached when the current address fails."""
    mirrors = [m for m in dict.fromkeys(mirrors) if is_kagane_domain(m)]
    if not mirrors:
        return
    with _kagane_lock:
        state = dict(_load_kagane_state())
        if state.get('mirrors') != mirrors:
            state['mirrors'] = mirrors
            _save_kagane_state(state)


def is_kagane_url(url):
    """Whether `url` points at Kagane, on any of its domains."""
    return bool(_KAGANE_HOST.match((url or '').strip()))


# (source_type, pattern capturing that site's own series id)
_PATTERNS = [
    ('mangadex', re.compile(_PREFIX + r'mangadex\.org/(?:title|manga)/(' + _UUID + ')', re.I)),
    ('kagane', re.compile(_PREFIX + KAGANE_HOST_PATTERN + r'/series/(' + _UUID + ')', re.I)),
    # atsu.moe redirects a series link straight to the latest chapter, so a
    # /read/<series>/<chapter> link identifies the same series too.
    ('atsu', re.compile(_PREFIX + r'atsu\.moe/(?:manga|read)/([A-Za-z0-9_-]+)', re.I)),
    ('asura', re.compile(_PREFIX + r'asurascans\.com/comics/([A-Za-z0-9-]+)', re.I)),
    ('hive', re.compile(_PREFIX + r'hivetoons\.org/series/([A-Za-z0-9-]+)', re.I)),
    ('flame', re.compile(_PREFIX + r'flamecomics\.xyz/series/(\d+)', re.I)),
    # Series pages only: chapter links (/<slug>-chapter-N/) sit outside
    # /comics/ and don't reliably carry the series slug.
    ('thunder', re.compile(_PREFIX + r'en-thunderscans\.com/comics/([A-Za-z0-9_-]+)', re.I)),
    # /title/<hid>-<slug>, and its chapters' /title/<hid>-<slug>/<id>-chapter-N
    ('comix', re.compile(_PREFIX + r'comix\.to/title/([A-Za-z0-9]+)', re.I)),
]

# Ids that are UUIDs compare case-insensitively, and so do Thunderscans'
# WordPress slugs; Atsumaru's short ids are case-sensitive, so those are
# left exactly as written.
_CASE_INSENSITIVE_IDS = {'mangadex', 'kagane', 'thunder'}

# AsuraScans ends every series slug with an 8-character hash
# (.../comics/chronicles-of-the-lazy-sovereign-b57aa235) that it rotates
# site-wide now and then - the same series is linked as -b57aa235 in an
# older source and -05c7df14 today. It isn't part of which series it is
# (Asura's API takes the slug without it), so it's dropped from the id.
_ASURA_HASH = re.compile(r'-[0-9a-f]{8}$', re.I)

_TRACKED_HOST = re.compile(
    _PREFIX + r'(?:mangadex\.org|' + KAGANE_HOST_PATTERN + r'|atsu\.moe|asurascans\.com|hivetoons\.org|flamecomics\.xyz|en-thunderscans\.com|comix\.to)(?:[/?#]|$)',
    re.I
)


def clean_source_url(url):
    """The link to store for a source: surrounding whitespace and any
    "?query" / "#fragment" dropped when it points at one of the eight tracked
    sites. A link copied from a browser tab carries UI state along with it
    (MangaDex's ?tab=art / ?tab=chapters, for one) that has no bearing on
    which series it is, and it made the same series look like a different
    link. A Kagane link on another of its domains is moved to the one in
    use. Anything else is only trimmed."""
    if not isinstance(url, str):
        return url
    url = url.strip()
    if _TRACKED_HOST.match(url):
        url = re.split(r'[?#]', url, maxsplit=1)[0]
        url = _KAGANE_HOST.sub(kagane_base(), url)
    return url


def parse_source_link(text):
    """(source_type, series_id) if `text` is a link to a series - or one of
    its chapters - on a tracked site, otherwise None."""
    text = (text or '').strip()
    for source_type, pattern in _PATTERNS:
        match = pattern.match(text)
        if match:
            series_id = match.group(1)
            if source_type in _CASE_INSENSITIVE_IDS:
                series_id = series_id.lower()
            elif source_type == 'asura':
                series_id = _ASURA_HASH.sub('', series_id)
            return source_type, series_id
    return None


def find_series_ids(cursor, source_type, source_series_id):
    """Ids of the tracked series that have a source pointing at this
    (source_type, source_series_id). Compares parsed ids rather than URL
    text, so a different slug, a /manga/ vs /title/ path, kagane.si vs
    kagane.org, or a chapter link all still land on the same series."""
    # LIKE is only a cheap pre-filter (it's case-insensitive and treats "_"
    # as a wildcard) - the parsed comparison below is the actual match.
    cursor.execute(
        "SELECT series_id, source_url FROM series_sources "
        "WHERE source_type = ? AND source_url LIKE ?",
        (source_type, f"%{source_series_id}%")
    )
    wanted = (source_type, source_series_id)
    return sorted({
        row[0] for row in cursor.fetchall()
        if parse_source_link(row[1]) == wanted
    })
