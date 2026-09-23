# Kenmei Import Helper

Companion Chrome/Brave extension for `/import-kenmei`. Speeds up matching
rows from a Kenmei CSV export to MangaDex/Atsumaru/AsuraScans/Kagane/HiveToons.
Also works directly on a `kenmei.co/series/...` page for looking up one
series at a time outside of a CSV - see [Kenmei series lookup](#kenmei-series-lookup)
below.

## Install (unpacked, not published)

1. `chrome://extensions` (or `brave://extensions`)
2. Enable **Developer mode** (top right)
3. **Load unpacked** → select this `extension/` folder

## Use

On `/import-kenmei`, start a row either by clicking its **All** button or
pressing **I**. The extension takes over from there:

| Key | Where              | Does |
|-----|--------------------|------|
| `K` | on a source tab    | jump into the first result on a search-results page |
| `Y` | on a source tab    | capture this tab's URL, append it to the row's URL box live, close the tab |
| `U` | on a source tab    | no match here - just close the tab |
| `I` | anywhere           | start the next pending row (same as clicking its **All** button) |

Starting a row (**I** or **All**) also copies the series name to the
clipboard, ready to paste into a source's own search box.

There's no separate "confirm" key - once all 5 of a row's tabs are closed
(by Y, U, or even a manual Ctrl+W), whatever got captured is submitted
automatically: no clicking Add per source, no 1-by-1 adds. Keys are
ignored while focus is in a text field, so they don't interfere with
actually using the sites' own search boxes. A small status badge
(bottom-right of the import page) shows the current row and how many of
the 5 tabs are still open/captured.

Moving to the next row is always manual (**I**) - nothing auto-advances,
so the loop is "I to start, look/Y/U ×5, [auto-submits], I for the next
row" whenever you're ready.

## Kenmei series lookup

On any `https://kenmei.co/series/...` page, the same source-matching step
works without a CSV import in progress:

| Key | Where              | Does |
|-----|--------------------|------|
| `I` | on the series page | open the same 5 source searches for this page's title, and copy the title to the clipboard |
| `K` | on a source tab    | jump into the first result on a search-results page |
| `Y` | on a source tab    | copy the running `url, url, ...` list to the clipboard, close the tab |
| `U` | on a source tab    | no match here - just close the tab |
| `I` | on a source tab    | re-run the search for the current series (start over) |

`I` also peeks at Kenmei's own **Add to your Dashboard** → source dropdown in
the background (expanding that form and opening its source list just long
enough to read it, then closing the list with Escape) and shows which of the
5 sources Kenmei itself already lists for this series on the badge - e.g.
`Kenmei has: Atsumaru, AsuraScans, HiveToons (not MangaDex, Kagane)`. That
form is left expanded but nothing is ever saved - the dashboard add only
commits on a separate Save click, which this never makes, so your Kenmei
library is untouched. Purely informational: all 5 search tabs still open
either way, so it's still your call whether to bother waiting on a source
Kenmei doesn't list.

Once that check comes back, each of the 5 opened tabs also gets a small dot
just to the left of the first result (the same one `K` would jump into) -
green if Kenmei's list includes that tab's source, red if not. It sits
beside the result, not on it: the dot means "Kenmei says this *site* has the
series", not "this specific result is the confirmed match" - a site's search
can come up empty (or wrong) purely from a title mismatch even when the
series is genuinely there, so it's kept visually separate from whatever the
search happened to find. It's a one-time snapshot taken when the check
finishes - pressing `K` to navigate to a result page loses it (the dot
doesn't follow across a navigation), and a tab closed (`Y`/`U`) before the
check finishes never gets one. Falls back to a fixed top-right corner dot if
no first result is found on the page at all. Each tab both waits for that
info to be pushed to it and asks for it on load, so a tab that reloaded into
a different page shortly after opening (Kagane's Cloudflare Turnstile
challenge does this) still gets its dot once it's back and asking.

The clipboard starts out holding the series title (copied by `I`); the first
`Y` replaces it, and every `Y` after that overwrites it with the full
`url, url, ...` list captured so far - by the last one, the clipboard holds every matched link,
ready to paste straight into the import page's URL box (which wants just
URLs, comma-separated - no title). Unlike the CSV import flow there's
nothing to auto-submit here (no row, no Add button), so nothing happens on
its own after the last tab closes - the clipboard is the end result. A
status badge (bottom-right of the series page, showing the title so you
don't lose track of which series you're matching) shows progress the same
way.

## Dashboard source search

The tracker's own "search all 5 sources" buttons use the same keys:
**Search** in the Add Series modal's *Search title* view (or Enter in that
box), and the search button in Series Settings' source section. With the
extension installed, those open the 5 tabs through the extension instead:

| Key | Where           | Does |
|-----|-----------------|------|
| `K` | on a source tab | jump into the first result on a search-results page |
| `Y` | on a source tab | copy the running `url, url, ...` list to the clipboard, close the tab |
| `U` | on a source tab | no match here - just close the tab |
| `I` | on a source tab | re-run the last search (start over) |

Same clipboard behavior as the kenmei.co lookup: the title is copied when
the search starts, and every `Y` overwrites it with all links captured so
far. Nothing is filled in on the page - paste the list wherever you want
it. A badge (bottom-right of the dashboard) shows progress and hides itself
a few seconds after the last tab closes. Without the extension the buttons
keep their normal open-5-tabs behavior.

## Known limitations (scaffold, not polished)

- Only finds pending rows on the *currently visible page* of the import
  table - moving to the next page of results is still a manual click.
- Matches a finished row by its title text; two rows with an identical
  title in the same CSV would be ambiguous.
- Scoped to any host on port `8080` (covers `localhost`, `127.0.0.1`, and
  any LAN IP) - if the app ever runs on a different port, update the
  `:8080` in `manifest.json`'s `content_scripts`/`host_permissions`.
- Chrome/Brave only (Manifest V3). Not tested on Firefox.
- Pressing I while a row still has tabs open abandons it (closes its
  remaining tabs) and starts the next one - whatever was already typed
  into that row's URL box is left as-is, uncommitted.
- K picks the first link on the page whose URL *shape* matches a real
  series page for that site (not a CSS selector, since site redesigns
  break those) - it's looking for the first such link in document order,
  so a "recommended"/"trending" module positioned above the actual search
  results could in principle win instead of the real first result. Wasn't
  verified against live pages while building this - if K ever grabs the
  wrong link on a given site, say so and I'll tighten the match.
- Kenmei series lookup: navigating from one kenmei.co series to another via
  an in-page (SPA) link won't re-run `content_kenmei.js`, so `I` would still
  open searches for the *previous* page's title - reload the page (or open
  the link in a new tab) after following an in-page link to a different
  series.
