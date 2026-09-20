# backend/gallery_covers.py
#
# Saves an Atsumaru series' full cover gallery, for the Series Settings
# cover picker. Kagane doesn't need this: its gallery comes back from the
# same browser fetch as the series itself (get_series_info(with_gallery=True)),
# and MangaDex's is a single cheap API call the callers make directly.
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
