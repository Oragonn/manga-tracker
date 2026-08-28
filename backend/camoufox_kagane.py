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
import json
import os
import threading
import time

from camoufox.async_api import AsyncCamoufox

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

    async def _fetch_json_async(self, url, timeout=25):
        await self._page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")

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
        return future.result(timeout=timeout)

    def get_series_info(self, series_id):
        """
        Fetch series metadata and chapters via Kagane's API using a
        stealth-hardened browser to clear Cloudflare's Turnstile challenge.
        Returns (meta_dict, books_list), shaped exactly like the old
        Selenium-based client's output so kagane.py needs no changes beyond
        the import line.
        """
        if not series_id:
            raise ValueError("series_id is required")

        with self.lock:
            now = time.time()
            elapsed = now - self.last_call
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed + 0.05)
            self.last_call = time.time()

            try:
                return self._run_coro(self._fetch_all_async(series_id))
            except Exception as e:
                try:
                    from .error_logger import log_error
                    log_error(
                        source_url=f"https://kagane.to/series/{series_id}",
                        error_message=str(e),
                        series_title="Kagane Browser Fetch"
                    )
                except Exception:
                    pass

                try:
                    self._run_coro(self._reinit_browser())
                except Exception:
                    pass
                raise RuntimeError(f"Kagane fetch failed after recovery: {e}")

    async def _fetch_all_async(self, series_id):
        meta_url = f"https://kagane.to/api/v2/series/{series_id}"
        raw = await self._fetch_json_async(meta_url)
        cover_url = await self._get_cached_or_download_cover(raw)
        return self._transform(raw, cover_url)

    async def _get_cached_or_download_cover(self, raw):
        covers = raw.get('series_covers') or []
        image_id = covers[0].get('image_id') if covers else None
        if not image_id:
            return None

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
            with open(os.path.join(_COVER_DIR, filename), 'wb') as f:
                f.write(image_bytes)
            return f"/static/uploads/kagane_covers/{filename}"
        except Exception:
            return None  # a missing cover shouldn't fail the whole fetch

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
                'release_date': b.get('published_on'),
            })

        return meta, books


# Singleton instance -- used by kagane.py
kagane_browser = KaganeBrowserClient()
