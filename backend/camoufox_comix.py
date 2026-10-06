# backend/camoufox_comix.py
#
# comix.to sits behind a Cloudflare challenge that plain requests can't pass,
# and its JSON API (/api/v1) is locked down on top of that: every request
# carries a `_` signature computed in the browser from the URL and its
# parameters, and the chapter list comes back encrypted ({"e": "..."}).
# Reproducing the signing outside the browser means copying Comix's
# obfuscated secure-*.js, which they rebuild regularly (the third-party
# github.com/yurtzy/comix-api did exactly that and was already broken).
#
# Instead this lets Comix's own code do the work. A camoufox page loads
# comix.to once, and two of the site's script files are patched as they
# arrive:
#   - env-*.js holds the site's API client, `var k={get:async(e,t)=>...}`,
#     whose interceptors sign each request and decrypt each answer. A line
#     appended to it exposes that client as window.__cxGet.
#   - main-*.js gets a small "bridge": camoufox runs our page.evaluate()
#     calls in an isolated world that can't see the page's window, so
#     requests and answers pass through attributes on <html> (the DOM is
#     shared between the two worlds).
# After that, any series' details or chapter pages are one signed API call
# each (~0.3s) from the same page, with no further page loads.
#
# Cover images are on static.comix.to, behind its own challenge for direct
# navigation, but the site's pages load them fine as <img>. So a cover is
# fetched by adding an <img> to the cleared page and reading the bytes of
# that network response, then cached locally (like Kagane's).

import asyncio
import concurrent.futures
import itertools
import json
import os
import re
import threading
import time

_SITE = "https://comix.to"

_COVER_DIR = os.path.join(os.path.dirname(__file__), '..', 'web', 'static', 'uploads', 'comix_covers')
_COVER_CONTENT_TYPE_EXT = {
    'image/webp': '.webp',
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
}

# The site's API client in env-*.js: `var k={get:async(e,t)=>(await ro.get(e,t)).data,...`
_CLIENT_RE = re.compile(r'var (\w+)=\{get:async\((\w+),(\w+)\)=>\(await \w+\.get\(\2,\3\)\)\.data')
# Fallback: the chapters call inside it, `chapters:(e,t={})=>k.get(F.manga.chapters(e),{params:t})`
_CHAPTERS_CALL_RE = re.compile(r'chapters:\((\w+),(\w+)=\{\}\)=>(\w+)\.get\(\w+\.manga\.chapters\(\1\),\{params:\2\}\)')

_BRIDGE_JS = r"""
(() => {
  if (window.__cxBridge) return; window.__cxBridge = true;
  const root = document.documentElement;
  const parse = JSON.parse, stringify = JSON.stringify;
  // Say when the API client is reachable
  const ready = setInterval(() => {
    if (window.__cxGet) { root.setAttribute('data-cx-ready', '1'); clearInterval(ready); }
  }, 100);
  new MutationObserver(() => {
    const raw = root.getAttribute('data-cx-req');
    if (!raw) return;
    root.removeAttribute('data-cx-req');
    const q = parse(raw);
    (async () => {
      let out;
      try {
        if (!window.__cxGet) throw new Error('Comix API client not available');
        out = { ok: true, data: await window.__cxGet(q.path, q.params) };
      } catch (e) {
        out = { ok: false, error: String((e && e.message) || e), status: (e && e.response && e.response.status) || null };
      }
      root.setAttribute('data-cx-res-' + q.rid, stringify(out));
    })();
  }).observe(root, { attributes: true, attributeFilter: ['data-cx-req'] });
})();
"""


class ComixNotFound(Exception):
    """The series doesn't exist on Comix (any more)."""


class ComixHTTPError(RuntimeError):
    """Comix answered with a server error (5xx) or didn't answer at all -
    it's down, not us."""


class ComixBrowserClient:
    def __init__(self):
        self.lock = threading.Lock()
        self._loop = None
        self._camoufox_cm = None
        self._browser = None
        self._page = None
        self._ready = False
        self._rid = itertools.count(1)
        self._patched = False

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._started = threading.Event()
        self._thread.start()
        if not self._started.wait(timeout=60):
            raise RuntimeError("Timed out starting Comix browser client")

    # --- browser lifecycle (all on the client's own event loop thread) ---

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init_browser())
        self._started.set()
        self._loop.run_forever()

    async def _init_browser(self):
        from camoufox.async_api import AsyncCamoufox
        # Not Kagane's humanize/geoip options: with them Comix's Cloudflare
        # answers the site's own script files with a 403 challenge page
        self._camoufox_cm = AsyncCamoufox(headless=True)
        self._browser = await self._camoufox_cm.__aenter__()
        self._page = await self._browser.new_page()
        await self._page.route('**/dist/main-*.js', self._patch_main)
        await self._page.route('**/dist/env-*.js', self._patch_env)
        self._ready = False

    async def _reinit_browser(self):
        self._ready = False
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

    @staticmethod
    async def _fetch_script(route):
        """The script's response and its text, or None for the text when it
        isn't a script we can read (the response is then passed through as
        is). The request goes out exactly as the browser made it: changing
        any header (even Accept-Encoding) gets Cloudflare's 403 page instead.
        Playwright decodes gzip itself but not zstd, which a 403 page has
        come back in - decoded here in case a real script ever does too."""
        resp = await route.fetch()
        if resp.status != 200:
            return resp, None
        body = await resp.body()
        if (resp.headers.get('content-encoding') or '').lower() == 'zstd':
            try:
                import zstandard
                body = zstandard.ZstdDecompressor().decompressobj().decompress(body)
            except Exception:
                pass
        try:
            return resp, body.decode('utf-8')
        except UnicodeDecodeError:
            print(f"[Comix] Couldn't read {route.request.url.rsplit('/', 1)[-1]} "
                  f"(content-encoding {resp.headers.get('content-encoding')!r})")
            return resp, None

    @staticmethod
    async def _fulfill(route, resp, js):
        headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in ('content-encoding', 'content-length')}
        await route.fulfill(status=resp.status, headers=headers, body=js)

    async def _patch_main(self, route):
        resp, js = await self._fetch_script(route)
        if js is None:
            return await route.fulfill(response=resp)
        await self._fulfill(route, resp, _BRIDGE_JS + '\n' + js)

    async def _patch_env(self, route):
        resp, js = await self._fetch_script(route)
        if js is None:
            self._patched = False
            return await route.fulfill(response=resp)
        m = _CLIENT_RE.search(js)
        if m:
            js += f'\n;window.__cxGet=(u,p)=>{m.group(1)}.get(u,{{params:p}});'
            self._patched = True
        else:
            m = _CHAPTERS_CALL_RE.search(js)
            if m:
                e, t, k = m.groups()
                # Exposed the first time the page itself asks for chapters
                patched_call = (f'chapters:({e},{t}={{}})=>(window.__cxGet=(u,p)=>{k}.get(u,{{params:p}}),'
                                + m.group(0).split('=>', 1)[1] + ')')
                js = js[:m.start()] + patched_call + js[m.end():]
                self._patched = True
            else:
                self._patched = False
        await self._fulfill(route, resp, js)

    async def _ensure_ready(self, hid, timeout=60):
        """Load a Comix page (the series' own, which also makes the site ask
        for its chapters - the fallback patch needs that) and wait for the
        API client to be exposed."""
        if self._ready:
            return
        self._patched = False
        # The page's own document responses: the challenge, then (once it
        # clears) the series page itself
        navigations = []

        def on_response(resp):
            try:
                if resp.request.is_navigation_request() and resp.frame == self._page.main_frame:
                    navigations.append(resp)
            except Exception:
                pass

        self._page.on('response', on_response)
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            try:
                await self._page.goto(f"{_SITE}/title/{hid}", timeout=timeout * 1000, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                # Not even the challenge page came back: Comix isn't answering
                raise ComixHTTPError(f"Comix didn't answer within {timeout}s")
            from .trackers.redirects import redirect_error
            moved = redirect_error(_SITE + "/", self._page.url)
            if moved:
                raise moved
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    if await self._page.get_attribute('html', 'data-cx-ready'):
                        self._ready = True
                        return
                except Exception:
                    pass  # mid-navigation (the Cloudflare challenge reloads the page)
                # A server error page (a 502 while Comix is down) will never
                # get the API client: fail now instead of waiting out the timeout
                error = await self._error_page(navigations)
                if error:
                    raise error
                await asyncio.sleep(0.25)
        finally:
            self._page.remove_listener('response', on_response)
        try:
            title = (await self._page.title() or '').lower()
        except Exception:
            title = ''
        if 'moment' in title or 'cloudflare' in title:
            raise RuntimeError(f"Stuck on Comix's Cloudflare challenge after {timeout}s")
        if not self._patched:
            raise RuntimeError("Comix's site code changed - its API client wasn't found (tracker needs updating)")
        raise RuntimeError(f"Comix page didn't finish loading after {timeout}s")

    async def _error_page(self, navigations):
        """A ComixHTTPError if the page is a server error (5xx) rather than
        Comix or a Cloudflare challenge still being solved, else None. Not
        4xx: a missing series is told apart by the API's own 404."""
        if not navigations or navigations[-1].status < 500:
            return None
        resp = navigations[-1]
        if (resp.headers.get('cf-mitigated') or '').lower() == 'challenge':
            return None
        try:
            title = (await self._page.title() or '').strip()
        except Exception:
            return None  # mid-navigation
        if 'moment' in title.lower():
            return None  # "Just a moment..." - the challenge
        return ComixHTTPError(f"Comix answered HTTP {resp.status}{f' ({title})' if title else ''}")

    async def _call(self, path, params, timeout=30):
        rid = next(self._rid)
        await self._page.evaluate(
            "q => document.documentElement.setAttribute('data-cx-req', JSON.stringify(q))",
            {'rid': rid, 'path': path, 'params': params or {}}
        )
        attr = f'data-cx-res-{rid}'
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            # A new document (the site reloads itself when its Cloudflare
            # clearance runs out) has lost this request and the bridge's
            # ready flag with it - give up now rather than at the timeout
            try:
                raw, ready = await self._page.evaluate(
                    "a => [document.documentElement.getAttribute(a),"
                    " document.documentElement.getAttribute('data-cx-ready')]", attr)
            except Exception:
                raw, ready = None, None
            if not raw and not ready:
                raise RuntimeError("Comix page reloaded during the request")
            if raw:
                await self._page.evaluate(f"() => document.documentElement.removeAttribute('{attr}')")
                out = json.loads(raw)
                if out.get('ok'):
                    return out.get('data')
                if out.get('status') == 404:
                    raise ComixNotFound(f"Comix has no {path}")
                if (out.get('status') or 0) >= 500:
                    raise ComixHTTPError(f"Comix API {path} answered HTTP {out['status']}")
                raise RuntimeError(f"Comix API {path} failed: {out.get('error')}")
            await asyncio.sleep(0.05)
        raise RuntimeError(f"Comix API {path} timed out after {timeout}s")

    async def _api_async(self, hid, path, params):
        await self._ensure_ready(hid)
        return await self._call(path, params)

    async def _cover_async(self, hid, url, timeout=20):
        await self._ensure_ready(hid)
        async with self._page.expect_response(lambda r: r.url == url, timeout=timeout * 1000) as info:
            await self._page.evaluate(
                "u => { const i = new Image(); i.src = u; i.style.display = 'none';"
                " i.onload = i.onerror = () => i.remove(); document.body.appendChild(i); }", url)
        resp = await info.value
        if resp.status != 200:
            raise RuntimeError(f"cover answered HTTP {resp.status}")
        return await resp.body(), (resp.headers.get('content-type') or '').split(';')[0].strip()

    def _run(self, coro, timeout):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    # --- public, thread-safe ---

    def api(self, hid, path, params=None):
        """One signed+decrypted Comix API call (e.g. '/manga/<hid>',
        '/manga/<hid>/chapters'). `hid` is the series the call is about,
        used to load a page if the browser isn't on comix.to yet. Retries
        once on a reloaded page, then once on a fresh browser - or, when
        Comix itself answered with a server error, just once more."""
        with self.lock:
            last_error = None
            for attempt in range(3):
                try:
                    return self._run(self._api_async(hid, path, params), timeout=100)
                except ComixNotFound:
                    raise
                except Exception as e:
                    from .trackers.redirects import SiteRedirectError
                    if isinstance(e, SiteRedirectError):
                        raise
                    # A fresh page or browser won't change Comix's own answer
                    if isinstance(e, ComixHTTPError):
                        if isinstance(last_error, ComixHTTPError):
                            break
                        last_error = e
                        continue
                    last_error = e
                    # A cleared session or the page's signing key can expire:
                    # reload the page first, then start over with a new browser
                    self._ready = False
                    if attempt == 1:
                        try:
                            self._run(self._reinit_browser(), timeout=90)
                        except Exception:
                            pass
            raise RuntimeError(f"Comix fetch failed: {last_error}")

    def website_loads(self, url):
        """Whether a comix.to page (the home page) loads - the site check's
        "is it only the API that's down?" probe. Leaves the series page the
        API calls run from, so the next call loads one again."""
        from .site_health import browser_page_loads
        with self.lock:
            self._ready = False
            try:
                return self._run(browser_page_loads(self._page, url), timeout=100)
            except Exception:
                return False

    def download_cover(self, hid, url):
        """Local /static URL of a Comix cover, downloading it through the
        cleared page if it isn't cached yet. None on any failure - a missing
        cover shouldn't fail the fetch."""
        if not url:
            return None
        name = re.sub(r'[^A-Za-z0-9@_-]', '_', url.rsplit('/', 1)[-1].rsplit('.', 1)[0])
        os.makedirs(_COVER_DIR, exist_ok=True)
        for ext in set(_COVER_CONTENT_TYPE_EXT.values()):
            if os.path.exists(os.path.join(_COVER_DIR, f"{name}{ext}")):
                return f"/static/uploads/comix_covers/{name}{ext}"
        with self.lock:
            try:
                body, content_type = self._run(self._cover_async(hid, url), timeout=40)
            except Exception as e:
                print(f"[Comix] Cover download failed for {url}: {e}")
                return None
        if not body:
            return None
        ext = _COVER_CONTENT_TYPE_EXT.get(content_type, '.jpg')
        final_path = os.path.join(_COVER_DIR, f"{name}{ext}")
        # Write-then-rename so a half-written file is never taken for a cached cover
        tmp_path = f"{final_path}.part"
        with open(tmp_path, 'wb') as f:
            f.write(body)
        os.replace(tmp_path, final_path)
        return f"/static/uploads/comix_covers/{name}{ext}"


_client = None
_client_lock = threading.Lock()
_START_RETRY_AFTER = 300  # seconds
_start_failed_at = 0
_start_error = None


def get_client():
    """The shared client, started on first use (not at import: importing the
    tracker must never launch a browser). If the browser won't start, every
    scan in the next few minutes fails straight away instead of each
    spending a minute trying again."""
    global _client, _start_failed_at, _start_error
    with _client_lock:
        if _client is None:
            if time.time() - _start_failed_at < _START_RETRY_AFTER:
                raise RuntimeError(f"Comix browser failed to start: {_start_error}")
            try:
                _client = ComixBrowserClient()
            except Exception as e:
                _start_failed_at, _start_error = time.time(), e
                raise
        return _client
