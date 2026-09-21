# backend/source_links.py
#
# Recognises a pasted source link (a series page, or a chapter-reader URL, on
# any of the six tracked sites) and finds the tracked series that has it
# attached, so the dashboard search box can take a link instead of a title.
#
# Deliberately doesn't import backend.trackers.*: kagane.py pulls in
# camoufox_kagane, which starts a headless browser the moment it's imported,
# and a plain search request must never trigger that. The patterns below
# mirror the extract_*_id() functions in those modules.

import re

_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
# The scheme is optional so a link pasted without "https://" still counts.
_PREFIX = r'(?:https?://)?(?:www\.)?'

# (source_type, pattern capturing that site's own series id)
_PATTERNS = [
    ('mangadex', re.compile(_PREFIX + r'mangadex\.org/(?:title|manga)/(' + _UUID + ')', re.I)),
    ('kagane', re.compile(_PREFIX + r'kagane\.(?:to|org)/series/(' + _UUID + ')', re.I)),
    # atsu.moe redirects a series link straight to the latest chapter, so a
    # /read/<series>/<chapter> link identifies the same series too.
    ('atsu', re.compile(_PREFIX + r'atsu\.moe/(?:manga|read)/([A-Za-z0-9_-]+)', re.I)),
    ('asura', re.compile(_PREFIX + r'asurascans\.com/comics/([A-Za-z0-9-]+)', re.I)),
    ('hive', re.compile(_PREFIX + r'hivetoons\.org/series/([A-Za-z0-9-]+)', re.I)),
    ('flame', re.compile(_PREFIX + r'flamecomics\.xyz/series/(\d+)', re.I)),
]

# Ids that are UUIDs compare case-insensitively; Atsumaru's short ids are
# case-sensitive, so those are left exactly as written.
_CASE_INSENSITIVE_IDS = {'mangadex', 'kagane'}

_TRACKED_HOST = re.compile(
    _PREFIX + r'(?:mangadex\.org|kagane\.(?:to|org)|atsu\.moe|asurascans\.com|hivetoons\.org|flamecomics\.xyz)(?:[/?#]|$)',
    re.I
)


def clean_source_url(url):
    """The link to store for a source: surrounding whitespace and any
    "?query" / "#fragment" dropped when it points at one of the six tracked
    sites. A link copied from a browser tab carries UI state along with it
    (MangaDex's ?tab=art / ?tab=chapters, for one) that has no bearing on
    which series it is, and it made the same series look like a different
    link. Anything else is only trimmed."""
    if not isinstance(url, str):
        return url
    url = url.strip()
    if _TRACKED_HOST.match(url):
        url = re.split(r'[?#]', url, maxsplit=1)[0]
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
            return source_type, series_id
    return None


def find_series_ids(cursor, source_type, source_series_id):
    """Ids of the tracked series that have a source pointing at this
    (source_type, source_series_id). Compares parsed ids rather than URL
    text, so a different slug, a /manga/ vs /title/ path, kagane.org vs
    kagane.to, or a chapter link all still land on the same series."""
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
