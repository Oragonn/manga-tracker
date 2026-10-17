# backend/kagane_mirror.py
#
# Keeps Kagane on an address that works. Kagane moves between domains
# (kagane.to died in 2026-10, then kagane.org became a page listing its
# mirrors: kagane.si, kagane.cx, ...), so after every Kagane site check
# (site_health.check_site, every 30 minutes) this decides whether to move:
#
#   1. kagane.org - the address Kagane tells people to bookmark - is used
#      whenever it serves the site itself again.
#   2. Otherwise the current address is left as soon as its home page
#      turns into the mirror list (its API may still answer, but every
#      link opened there shows the list), or once it has failed two checks
#      in a row - one failed check (a blip, a short outage) isn't enough.
#   3. It moves to the first address that works, in the order kagane.org
#      lists them (the recommended one first), then any other known one.
#
# "Works" means the home page loads, isn't the mirror list, and the API
# there returns chapters for one of your series (camoufox_kagane.probe_domain).
# If none does, nothing changes. A move switches every request and new link
# at once, rewrites the stored links, and is written to the Activity Log.

from .source_links import (
    KAGANE_HOME_DOMAIN, KAGANE_KNOWN_DOMAINS, kagane_domain, kagane_state,
    parse_source_link, remember_kagane_mirrors, set_kagane_domain,
)

FAILED_CHECKS_BEFORE_MOVING = 2

_failed_checks = 0  # Kagane site checks failed in a row on the current address


def after_check(status, reference_url):
    """Called by the Kagane site check with its result ('up' / 'down') and
    the series link it checked. Returns the new domain if Kagane was moved,
    else None. Never raises."""
    global _failed_checks
    try:
        _failed_checks = 0 if status == 'up' else _failed_checks + 1
        parsed = parse_source_link(reference_url or '')
        series_id = parsed[1] if parsed and parsed[0] == 'kagane' else None
        moved = _decide(series_id)
    except Exception as e:
        print(f"[Kagane] Mirror check failed: {e}")
        moved = None
    if not moved:
        # A scan that was already running during a move stores its links on
        # the old address - catch those up (a few ms when there are none)
        try:
            from .database import move_kagane_links_to_current_domain
            move_kagane_links_to_current_domain()
        except Exception as e:
            print(f"[Kagane] Couldn't move stored links to {kagane_domain()}: {e}")
    return moved


def _decide(series_id):
    from .camoufox_kagane import kagane_browser
    current = kagane_domain()
    mirrors = []
    tried = {current}

    if current != KAGANE_HOME_DOMAIN:
        home = kagane_browser.probe_domain(KAGANE_HOME_DOMAIN, series_id)
        tried.add(KAGANE_HOME_DOMAIN)
        if home['ok']:
            return move_to(KAGANE_HOME_DOMAIN, f'{KAGANE_HOME_DOMAIN} serves the site again')
        mirrors = home['mirrors']

    here = kagane_browser.probe_domain(current)  # home page only: the check just tried its API
    mirrors = mirrors or here['mirrors']
    if mirrors:
        remember_kagane_mirrors(mirrors)
    if here['mirrors']:
        reason = f'{current} only shows the list of mirrors'
    elif _failed_checks >= FAILED_CHECKS_BEFORE_MOVING:
        reason = f'{current} failed {_failed_checks} checks in a row'
    else:
        return None

    candidates = list(dict.fromkeys(
        mirrors + (kagane_state().get('mirrors') or []) + list(KAGANE_KNOWN_DOMAINS)))
    for domain in candidates:
        if domain in tried:
            continue
        tried.add(domain)
        result = kagane_browser.probe_domain(domain, series_id)
        if result['ok']:
            return move_to(domain, reason)
        print(f"[Kagane] {domain} can't be used: {result['why']}")
    print(f"[Kagane] {reason}, but no other address works - staying on {current}")
    return None


def move_to(domain, reason):
    """Switch Kagane to `domain`: every request and new link from now on,
    the stored links, and an Activity Log entry."""
    global _failed_checks
    previous = set_kagane_domain(domain, reason)
    _failed_checks = 0
    print(f"[Kagane] Moved from {previous} to {domain}: {reason}")
    try:
        from .database import move_kagane_links_to_current_domain
        move_kagane_links_to_current_domain()
    except Exception as e:
        print(f"[Kagane] Couldn't move stored links to {domain} (retried at next start): {e}")
    try:
        from .activity_logger import log_activity
        log_activity('site_moved', series_title='Kagane',
                     old_value={'source_type': 'kagane', 'domain': previous},
                     new_value={'source_type': 'kagane', 'domain': domain, 'reason': reason},
                     can_undo=False)
    except Exception as e:
        print(f"[Kagane] Couldn't log the move: {e}")
    return domain
