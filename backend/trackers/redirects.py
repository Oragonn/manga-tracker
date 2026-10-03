# backend/trackers/redirects.py
#
# A source site that's gone or moving often doesn't fail outright - it
# redirects every page somewhere else (Flame Comics, 2026-10: everything
# goes to its Discord invite). The request then "works" and the tracker only
# fails later with a vague "no parseable data" / JSON error. This makes any
# request that lands on a different site fail right away, saying so - the
# reason then shown on the Scheduler page, Source Alerts and Series
# Settings' red dot.

from urllib.parse import urljoin, urlparse


class SiteRedirectError(Exception):
    """The site sent the request off to another site - down or moved."""


def site_of(url):
    """The site part of a URL's host: api.mangadex.org -> mangadex.org."""
    host = (urlparse(url).hostname or '').lower()
    return '.'.join(host.split('.')[-2:])


def redirect_error(requested_url, final_url):
    """A SiteRedirectError if final_url is on another site than
    requested_url, else None."""
    if final_url and site_of(final_url) != site_of(requested_url):
        return SiteRedirectError(
            f"{site_of(requested_url)} redirects to {final_url} instead of the page asked for - site down or moved"
        )
    return None


def _response_hook(resp, *args, **kwargs):
    # Runs on each hop before requests follows it, so it's the redirect
    # itself (a 3xx and where it points) that's looked at
    if resp.is_redirect:
        error = redirect_error(resp.url, urljoin(resp.url, resp.headers.get('location', '')))
        if error:
            raise error


def watch_redirects(session):
    """Make every request of this requests.Session raise SiteRedirectError
    when it's redirected to another site."""
    session.hooks['response'].append(_response_hook)
    return session
