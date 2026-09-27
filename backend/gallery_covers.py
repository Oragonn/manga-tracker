# backend/gallery_covers.py
#
# Saves an Atsumaru series' full cover gallery, for the Series Settings
# cover picker. Kagane usually doesn't need this: its gallery comes back from
# the same browser fetch as the series itself (get_series_info(with_gallery=
# True)) - except after the Add Series preview, which skips the gallery to
# show the series sooner, so the add fetches it here afterwards. MangaDex's
# is a single cheap API call the callers make directly.
#
# Atsumaru is different because each cover is its own download behind a
# 0.4s throttle - a series with a dozen covers would hold up the add-series
# queue for several seconds, and a bulk import multiplies that - so it runs
# on its own thread and never delays (or fails) the add itself.

import threading


def save_atsu_gallery(series_id, manga_id):
    """Best-effort: fetch and store the gallery, log and swallow any failure."""
    try:
        from .trackers.atsu import get_gallery
        from .database import save_gallery_covers
        save_gallery_covers(series_id, 'atsu', get_gallery(manga_id))
    except Exception as e:
        print(f"[Gallery] Failed to fetch Atsumaru cover gallery for series {series_id}: {e}")


def save_atsu_gallery_in_background(series_id, manga_id):
    """Never raises - the caller has already saved the series by now, and a
    gallery that couldn't be started mustn't turn that into a failed add."""
    try:
        threading.Thread(
            target=save_atsu_gallery,
            args=(series_id, manga_id),
            daemon=True,
            name=f"atsu-gallery-{series_id}"
        ).start()
    except Exception as e:
        print(f"[Gallery] Couldn't start Atsumaru gallery fetch for series {series_id}: {e}")


def save_kagane_gallery(series_id, kagane_id):
    """Best-effort, like save_atsu_gallery."""
    try:
        from .trackers.kagane import get_series_info
        from .database import save_gallery_covers
        info = get_series_info(kagane_id, with_gallery=True)
        save_gallery_covers(series_id, 'kagane', (info or {}).get('gallery_covers'))
    except Exception as e:
        print(f"[Gallery] Failed to fetch Kagane cover gallery for series {series_id}: {e}")


def save_kagane_gallery_in_background(series_id, kagane_id):
    """Never raises, same as save_atsu_gallery_in_background."""
    try:
        threading.Thread(
            target=save_kagane_gallery,
            args=(series_id, kagane_id),
            daemon=True,
            name=f"kagane-gallery-{series_id}"
        ).start()
    except Exception as e:
        print(f"[Gallery] Couldn't start Kagane gallery fetch for series {series_id}: {e}")
