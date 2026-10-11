# backend/site_pause.py
#
# Source sites paused by hand from the Scheduler page. A paused site is left
# completely alone until it's unpaused: no scan fetches it, no site check,
# no Add preview, no metadata refresh, no cover download. Everything that
# reaches a site goes through a guard here - each tracker's requests session
# (guard_session) and the Kagane/Comix browser clients (ensure_not_paused) -
# so no caller can miss it. Made for a site that has blocked this IP
# (Comix, 2026-10-11), where any request only keeps the block going.
#
# Kept in a small JSON file beside the database rather than in it: the
# guard runs inside requests, and get_db()'s global lock isn't re-entrant
# (a thread already holding it would deadlock). It also means restoring a
# backup doesn't unpause a site.

import json
import os
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_paused = None  # {source_type: paused_at ISO}, loaded on first use


class SitePaused(RuntimeError):
    """The site is paused on the Scheduler page - not contacted at all."""


def _path():
    from .database import DB_PATH
    return os.path.join(os.path.dirname(DB_PATH) or '.', 'paused_sites.json')


def _load():
    global _paused
    if _paused is None:
        try:
            with open(_path(), encoding='utf-8') as f:
                data = json.load(f)
            _paused = {k: v for k, v in data.items() if isinstance(k, str)} if isinstance(data, dict) else {}
        except FileNotFoundError:
            _paused = {}
        except Exception as e:
            print(f"[Site Pause] Couldn't read {_path()}: {e}")
            _paused = {}
    return _paused


def paused_sites():
    """{source_type: paused_at} for every paused site."""
    with _lock:
        return dict(_load())


def is_paused(source_type):
    with _lock:
        return source_type in _load()


def set_paused(source_type, paused):
    """Pause or unpause a site; returns whether anything changed."""
    with _lock:
        current = _load()
        if paused == (source_type in current):
            return False
        updated = dict(current)
        if paused:
            updated[source_type] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        else:
            updated.pop(source_type, None)
        path = _path()
        tmp = f"{path}.part"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(updated, f)
        os.replace(tmp, path)
        globals()['_paused'] = updated
    return True


def ensure_not_paused(source_type):
    if is_paused(source_type):
        from .site_health import SITE_LABELS
        label = SITE_LABELS.get(source_type, source_type)
        raise SitePaused(f"{label} is paused (Scheduler page) - not contacted until it's unpaused")


def guard_session(session, source_type):
    """Make every request of this requests.Session raise SitePaused instead
    of going out while the site is paused."""
    send = session.request

    def request(*args, **kwargs):
        ensure_not_paused(source_type)
        return send(*args, **kwargs)

    session.request = request
    return session
