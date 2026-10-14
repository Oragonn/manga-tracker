# backend/browser_env.py
#
# Environment for the camoufox browsers (Kagane, Comix). Cloudflare's check
# compares the browser's timezone with where its IP address is: the server
# runs on UTC while the IP is in France, and a browser saying "UTC" from a
# French IP fails the check. Comix, 2026-10-14: after an IP block,
# Cloudflare kept the server on "Just a moment..." forever until the
# browser ran on Paris time (verified on the server and locally - the same
# machine fails with TZ=UTC and passes with TZ=Europe/Paris). camoufox's own
# geoip option changes the language but not the timezone the page sees.

import os

BROWSER_TIMEZONE = 'Europe/Paris'


def browser_env():
    """The environment to start a camoufox browser with: this process's own,
    with the timezone set to where the IP is."""
    return {**os.environ, 'TZ': BROWSER_TIMEZONE}
