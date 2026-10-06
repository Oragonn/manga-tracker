# backend/camoufox_kagane.py
#
# Replaces selenium_kagane.py. kagane.to's Cloudflare protection is now an
# interactive Turnstile challenge (not the old passive JS delay check), which
# plain Selenium can no longer pass. camoufox (a patched, stealth-hardened
# Firefox build) reliably gets through it on real page navigation.
#
# The old /api/v2/books/{id} endpoint this client used to call for chapters
# no longer exists / is far more strictly gated than /api/v2/series/{id} --
# the current site embeds the full chapter list (series_books) directly in
# the series metadata response, so a single fetch now covers both meta and
# chapters.

import asyncio
import base64
import concurrent.futures
import json
import os
import threading
import time

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

_CHALLENGE_TITLE_MARKERS = (
    'un instant', 'just a moment', 'un momento', 'momento',
    'nur einen moment', 'un attimo', 'loading',
)

# Cover images are behind the same Cloudflare Turnstile challenge as the API,
# and <img> tags can't run that interactive challenge (it needs a full page
# navigation), so hotlinking straight to kagane.to would just show a broken
# image for anyone without an existing kagane.to clearance cookie. Instead,
# download the cover once (via the already-cleared browser page) and cache
# it locally, keyed by Kagane's own image_id so repeat scans of the same
# series reuse the cached file instead of re-downloading.
_COVER_DIR = os.path.join(os.path.dirname(__file__), '..', 'web', 'static', 'uploads', 'kagane_covers')
_COVER_CONTENT_TYPE_EXT = {
    'image/webp': '.webp',
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
}

# Seconds get_series_info(with_gallery=True) may spend downloading gallery
# covers, on top of the normal fetch (see KaganeBrowserClient._download_gallery).
_GALLERY_TIME_BUDGET = 45

_FETCH_IMAGE_AS_DATA_URL_JS = """async (url) => {
    try {
        const res = await fetch(url, { credentials: 'include' });
        if (!res.ok) return { status: res.status };
        const blob = await res.blob();
        const dataUrl = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onloadend = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(blob);
        });
        return { status: res.status, contentType: res.headers.get('content-type'), dataUrl };
    } catch (e) {
        return { error: String(e) };
    }
}"""


def _lookup_series_title(series_id):
    """Best-effort: this client only knows the Kagane UUID, not which of our
    tracked series it belongs to - look it up by matching the UUID against
    stored source_urls (either kagane.to or kagane.org, whichever the source
    was originally added with) so failure logs show a real title instead of
    the generic "Kagane Browser Fetch" placeholder."""
    try:
        from .database import get_db, release_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT s.title FROM series_sources ss JOIN series s ON s.id = ss.series_id "
            "WHERE ss.source_url LIKE ? LIMIT 1",
            (f"%{series_id}%",)
        )
        row = cursor.fetchone()
        release_db(conn)
        return row[0] if row else None
    except Exception:
        return None


class KaganeHTTPError(RuntimeError):
    """Kagane answered with an HTTP error page instead of JSON, or didn't
    answer at all - its side, so a fresh browser won't help."""


class KaganeBrowserClient:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_call = 0
        self.min_delay = 0.9  # seconds between requests

        self._loop = None
        self._camoufox_cm = None
        self._browser = None
        self._page = None

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        if not self._ready.wait(timeout=60):
            raise RuntimeError("Timed out starting Kagane browser client")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init_browser())
        self._ready.set()
        self._loop.run_forever()

    async def _init_browser(self):
        self._camoufox_cm = AsyncCamoufox(headless=True, humanize=True, geoip=True)
        self._browser = await self._camoufox_cm.__aenter__()
        self._page = await self._browser.new_page()

    async def _reinit_browser(self):
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._camoufox_cm:
                await self._camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass
        await self._init_browser()

    async def _fetch_json_async(self, url, timeout=45):
        # The page's own document responses: the challenge, then (once it
        # clears) the API's real answer
        navigations = []

        def on_response(resp):
            try:
                if resp.request.is_navigation_request() and resp.frame == self._page.main_frame:
                    navigations.append(resp)
            except Exception:
                pass

        self._page.on('response', on_response)
        try:
            return await self._wait_for_json(url, timeout, navigations)
        finally:
            self._page.remove_listener('response', on_response)

    async def _error_page(self, url, navigations):
        """A KaganeHTTPError if the page is an HTTP error (Kagane's API
        down, the series gone) rather than the API's answer or a Cloudflare
        challenge still being solved, else None."""
        if not navigations or navigations[-1].status < 400:
            return None
        resp = navigations[-1]
        if (resp.headers.get('cf-mitigated') or '').lower() == 'challenge':
            return None
        try:
            title = (await self._page.title() or '').strip()
        except Exception:
            return None  # mid-navigation
        if any(marker in title.lower() for marker in _CHALLENGE_TITLE_MARKERS):
            return None
        return KaganeHTTPError(f"Kagane answered HTTP {resp.status}"
                               f"{f' ({title})' if title else ''} at {url}")

    async def _wait_for_json(self, url, timeout, navigations):
        try:
            await self._page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            # Not even the challenge page came back: Kagane isn't answering
            raise KaganeHTTPError(f"Kagane didn't answer within {timeout}s at {url}")
        # Sent off to another site (down or moved) - no <pre> to wait for
        from .trackers.redirects import redirect_error
        moved = redirect_error(url, self._page.url)
        if moved:
            raise moved

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                pre = await self._page.query_selector("pre")
                if pre:
                    text = await pre.inner_text()
                    if text.strip():
                        return json.loads(text)
            except Exception:
                pass  # transient (mid-navigation) -- keep polling
            # An error page (a 502 while Kagane's API is down) will never
            # turn into JSON: fail now instead of waiting out the timeout
            error = await self._error_page(url, navigations)
            if error:
                raise error
            await asyncio.sleep(0.5)

        try:
            title = (await self._page.title() or "").lower()
        except Exception:
            title = ""
        if any(marker in title for marker in _CHALLENGE_TITLE_MARKERS):
            raise RuntimeError(
                f"Stuck on Cloudflare challenge page after {timeout}s at {url}"
            )
        try:
            html = await self._page.content()
        except Exception:
            html = ""
        raise RuntimeError(f"No <pre> tag found at {url}. Page snippet: {html[:500]}")

    def _run_coro(self, coro, timeout=40):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # Stop it for real: left running, it keeps driving the page while
            # the caller's retry reinitialises the browser under it.
            future.cancel()
            raise

    def get_series_info(self, series_id, with_gallery=False):
        """
        Fetch series metadata and chapters via Kagane's API using a
        stealth-hardened browser to clear Cloudflare's Turnstile challenge.
        Returns (meta_dict, books_list), shaped exactly like the old
        Selenium-based client's output so kagane.py needs no changes beyond
        the import line.

        with_gallery also downloads every cover in the series' gallery and
        returns them as meta['gallery_covers']. Off by default: the
        scheduler calls this on every scan, and a gallery is only worth
        fetching when a source is first added.
        """
        if not series_id:
            raise ValueError("series_id is required")

        with self.lock:
            last_error = None
            # kagane.to's Turnstile occasionally just doesn't clear within the
            # navigation window on the first try (not specific to any one
            # series -- it happens across the whole Kagane pool, though
            # larger/heavier series pages are more likely to brush up against
            # the timeout since there's simply more to load). A fresh browser
            # session often gets a clean challenge on the next attempt, so
            # retry once with a reinit before logging a failure, instead of
            # treating every transient stall as a missed scan.
            for attempt in range(2):
                now = time.time()
                elapsed = now - self.last_call
                if elapsed < self.min_delay:
                    time.sleep(self.min_delay - elapsed + 0.05)
                self.last_call = time.time()

                try:
                    return self._run_coro(
                        self._fetch_all_async(series_id, with_gallery=with_gallery),
                        # The gallery's own time budget rides on top of the base allowance
                        timeout=90 + (_GALLERY_TIME_BUDGET + 15 if with_gallery else 0)
                    )
                except Exception as e:
                    last_error = e
                    # Kagane itself answered with an error: a fresh browser
                    # won't change that, so the retry just asks again
                    if isinstance(e, KaganeHTTPError):
                        continue
                    try:
                        self._run_coro(self._reinit_browser())
                    except Exception:
                        pass

            try:
                from .error_logger import log_error
                log_error(
                    source_url=f"https://kagane.to/series/{series_id}",
                    error_message=str(last_error),
                    series_title=_lookup_series_title(series_id) or "Kagane Browser Fetch"
                )
            except Exception:
                pass
            raise RuntimeError(f"Kagane fetch failed after recovery: {last_error}")

    def website_loads(self, url):
        """Whether a kagane.to page (the home page) loads - the site check's
        "is it only the API that's down?" probe."""
        from .site_health import browser_page_loads
        with self.lock:
            try:
                return self._run_coro(browser_page_loads(self._page, url), timeout=100)
            except Exception:
                return False

    async def _fetch_all_async(self, series_id, with_gallery=False):
        meta_url = f"https://kagane.to/api/v2/series/{series_id}"
        raw = await self._fetch_json_async(meta_url)
        cover_url = await self._get_cached_or_download_cover(raw)
        meta, books = self._transform(raw, cover_url)
        if with_gallery:
            meta['gallery_covers'] = await self._download_gallery(raw)
        return meta, books

    async def _get_cached_or_download_cover(self, raw):
        covers = raw.get('series_covers') or []
        image_id = covers[0].get('image_id') if covers else None
        if not image_id:
            return None
        return await self._cache_image(image_id)

    async def _cache_image(self, image_id):
        """Local /static URL for one Kagane image, downloading it through
        the cleared browser page first if it isn't cached yet. Returns None
        on any failure - a missing cover shouldn't fail the whole fetch."""
        os.makedirs(_COVER_DIR, exist_ok=True)

        for ext in _COVER_CONTENT_TYPE_EXT.values():
            if os.path.exists(os.path.join(_COVER_DIR, f"{image_id}{ext}")):
                return f"/static/uploads/kagane_covers/{image_id}{ext}"

        try:
            img_url = f"https://kagane.to/api/v2/image/{image_id}/compressed"
            result = await self._page.evaluate(_FETCH_IMAGE_AS_DATA_URL_JS, img_url)
            data_url = result.get('dataUrl')
            if not data_url:
                return None

            _, b64data = data_url.split(',', 1)
            image_bytes = base64.b64decode(b64data)
            content_type = (result.get('contentType') or '').split(';')[0].strip()
            ext = _COVER_CONTENT_TYPE_EXT.get(content_type, '.jpg')

            filename = f"{image_id}{ext}"
            # Write-then-rename so a half-written file (killed mid-download)
            # can never be mistaken for a cached cover by the exists() check.
            final_path = os.path.join(_COVER_DIR, filename)
            tmp_path = f"{final_path}.part"
            with open(tmp_path, 'wb') as f:
                f.write(image_bytes)
            os.replace(tmp_path, final_path)
            return f"/static/uploads/kagane_covers/{filename}"
        except Exception:
            return None

    async def _download_gallery(self, raw):
        """Cache every entry of the series' `series_covers` list (one per
        volume/language, same set as the series page's cover gallery) and
        return them as [{cover_url, volume, locale, note}].

        Best-effort and time-boxed: this runs inside get_series_info's own
        timeout, and a long-running series can have well over 100 covers, so
        past the budget it stops and returns what it has rather than
        letting the gallery fail the whole fetch. Covers already cached are
        skipped instantly on a re-run, so a later re-run (the backfill
        script) picks up where this one left off."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _GALLERY_TIME_BUDGET
        gallery = []
        for cover in raw.get('series_covers') or []:
            image_id = cover.get('image_id')
            if not image_id:
                continue
            if loop.time() > deadline:
                print(f"[Kagane] Gallery download hit its {_GALLERY_TIME_BUDGET}s budget "
                      f"after {len(gallery)} of {len(raw.get('series_covers') or [])} covers")
                break
            local_url = await self._cache_image(image_id)
            if not local_url:
                continue
            gallery.append({
                'cover_url': local_url,
                'volume': cover.get('volume_number'),
                'locale': cover.get('language'),
                'note': cover.get('note')
            })
        return gallery

    def _transform(self, raw, cover_url):
        """Adapt kagane.to's current API shape to the (meta, books) shape
        kagane.py's parsing logic expects."""
        genre_names = [g.get('genre_name') for g in raw.get('genres', []) if g.get('genre_name')]
        fmt = raw.get('format')
        if fmt and fmt not in genre_names:
            genre_names.append(fmt)

        meta = {
            'name': raw.get('title', 'Unknown Title'),
            'status': raw.get('publication_status', ''),
            'genres': genre_names,
            # Kagane keeps its themes/tropes in a separate 'tags' list next
            # to 'genres' -- pass them through so kagane.py can merge both.
            'tags': [t.get('tag_name') for t in raw.get('tags', []) if t.get('tag_name')],
            'content_rating': raw.get('content_rating'),
            'cover_url': cover_url,
            'alternate_titles': [
                {'title': t.get('title')}
                for t in raw.get('series_alternate_titles', [])
                if t.get('title')
            ],
        }

        books = []
        for b in raw.get('series_books', []):
            books.append({
                'id': b.get('book_id'),
                'title': b.get('title') or 'Untitled',
                'number_sort': b.get('sort_no', 0),
                # sort_no is just the book's position in the list; chapter_no is
                # the chapter number Kagane itself shows ("3.5", "36") and is
                # what an untitled book has to be numbered from.
                'chapter_no': b.get('chapter_no'),
                'release_date': b.get('published_on'),
                # When the book went up on Kagane. published_on is missing on a
                # lot of books, and kagane.py falls back to this (as the site does).
                'uploaded_at': b.get('became_visible_at') or b.get('created_at'),
            })

        return meta, books


# Singleton instance -- used by kagane.py
kagane_browser = KaganeBrowserClient()
