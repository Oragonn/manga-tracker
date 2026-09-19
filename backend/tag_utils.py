# backend/tag_utils.py
#
# Helpers for the `genres` column, which holds every scraped tag for a series
# (genres, themes, demographics, ...) merged across all of its sources, and
# for the tag rules (merge / ban) the Fixes page's Tag tab layers on top of it.
#
# Rules are applied when tags are READ, never written back into `genres`, so
# the column always keeps exactly what the sources reported and removing a
# rule instantly restores the original tags.

import json


def normalize_tag_list(value):
    """Coerce a stored or fetched genres value (list, dict, str, None) into a
    plain list of non-empty strings."""
    if isinstance(value, dict):
        value = list(value.values())
    elif not isinstance(value, list):
        value = [value] if value else []
    return [str(v).strip() for v in value if v and str(v).strip()]


def merge_tag_lists(*tag_lists):
    """Union several tag lists in first-seen order, dropping case-insensitive
    duplicates ("Slice of Life" vs "Slice Of Life" from two different
    sources) and keeping whichever spelling showed up first."""
    seen = set()
    merged = []
    for tags in tag_lists:
        for tag in tags or []:
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                merged.append(tag)
    return merged


def parse_stored_tags(raw):
    """Decode a `series.genres` column value into a list of tag names ([] if
    it's empty or isn't a JSON list)."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return normalize_tag_list(parsed) if isinstance(parsed, list) else []


# --- Tag rules ---------------------------------------------------------
#
# A rules dict maps a casefolded tag name to its rule:
#   {'id', 'tag', 'action': 'merge' | 'ban', 'target', 'target_key'}
# A 'merge' rule folds `tag` into `target`; a 'ban' rule hides `tag`.
# Names are matched case-insensitively.

def load_tag_rules(cursor):
    """Every tag rule, keyed by casefolded tag name. Takes a cursor from the
    caller's own connection: get_db() holds a non-reentrant global lock, so
    opening a second connection from inside a request that already has one
    open would deadlock."""
    cursor.execute("SELECT id, tag, tag_key, action, target, target_key FROM tag_rules")
    return {
        row[2]: {
            'id': row[0], 'tag': row[1], 'action': row[3],
            'target': row[4], 'target_key': row[5],
        }
        for row in cursor.fetchall()
    }


def resolve_tag(tag, rules):
    """The name `tag` ends up as once the rules are applied, following merges
    as far as they go. None if the tag, or anything it merges into, is
    banned."""
    seen = set()
    current = tag
    while True:
        key = current.casefold()
        rule = rules.get(key)
        if rule is None:
            return current
        if rule['action'] == 'ban':
            return None
        if key in seen:  # can't happen (loops are rejected on create), but never spin
            return current
        seen.add(key)
        current = rule['target']


def apply_tag_rules(tags, rules):
    """Fold merged tags into their target and drop banned ones, de-duplicating
    (two tags merged into the same target collapse to one)."""
    if not rules:
        return list(tags)
    resolved = (resolve_tag(tag, rules) for tag in tags)
    return merge_tag_lists([tag for tag in resolved if tag is not None])


def edit_series_tags(raw_tags, add, remove, rules):
    """Apply an edit made on a series' EFFECTIVE tag list (what the Tags filter
    and the Series Settings chips show, i.e. after merges and bans) back onto
    its stored tags, and return the new stored list.

    Removing a tag drops every stored tag that shows up as it, so removing "b"
    also drops the "a" and "aa" merged into it. Stored tags that are banned
    never show in the effective list, so an edit can't touch them: they stay on
    the series, hidden, exactly as before. Adding a tag stores its effective
    name (an alias is stored as the tag it merges into) and skips one the
    series already shows, however it's capitalised.

    Raises ValueError, changing nothing, if asked to add a banned tag."""
    remove_keys = {name.casefold() for name in remove}
    kept = []
    for tag in raw_tags:
        effective = resolve_tag(tag, rules)
        if effective is not None and effective.casefold() in remove_keys:
            continue
        kept.append(tag)

    shown = set()
    for tag in kept:
        effective = resolve_tag(tag, rules)
        if effective is not None:
            shown.add(effective.casefold())

    for name in add:
        effective = resolve_tag(name, rules)
        if effective is None:
            raise ValueError(f"'{name}' is a banned tag - unban it on the Fixes page first")
        if effective.casefold() not in shown:
            shown.add(effective.casefold())
            kept.append(effective)
    return kept


def tags_matching(name, rules):
    """Every stored tag name that shows up as `name` once the rules are
    applied: the resolved name itself plus each tag merged into it. Empty if
    `name` is banned. This is what a Tags-filter selection has to match in the
    raw `genres` column."""
    canonical = resolve_tag(name, rules)
    if canonical is None:
        return []
    names = [canonical]
    for rule in rules.values():
        if rule['action'] != 'merge':
            continue
        final = resolve_tag(rule['tag'], rules)
        if final is not None and final.casefold() == canonical.casefold():
            names.append(rule['tag'])
    return merge_tag_lists(names)


def count_tags(genre_rows, rules=None):
    """One {'tag', 'count'} entry per case-insensitive tag across the given
    raw `series.genres` values, where count is the number of series that have
    it and 'tag' is its most common spelling (sources disagree on casing).
    With `rules`, they're applied first: merged tags are counted under their
    target and banned ones are left out."""
    spellings = {}      # casefolded tag -> {spelling: occurrences}
    series_counts = {}  # casefolded tag -> number of series
    for raw in genre_rows:
        tags = parse_stored_tags(raw)
        if rules:
            tags = apply_tag_rules(tags, rules)
        for key in {tag.casefold() for tag in tags}:
            series_counts[key] = series_counts.get(key, 0) + 1
        for tag in tags:
            counts = spellings.setdefault(tag.casefold(), {})
            counts[tag] = counts.get(tag, 0) + 1
    return [
        {'tag': min(counts, key=lambda name: (-counts[name], name)), 'count': series_counts[key]}
        for key, counts in spellings.items()
    ]
