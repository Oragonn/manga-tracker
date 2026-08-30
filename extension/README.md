# Kenmei Import Helper

Companion Chrome/Brave extension for `/import-kenmei`. Speeds up matching
rows from a Kenmei CSV export to MangaDex/Atsumaru/AsuraScans/Kagane.

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

There's no separate "confirm" key - once all 4 of a row's tabs are closed
(by Y, U, or even a manual Ctrl+W), whatever got captured is submitted
automatically: no clicking Add per source, no 1-by-1 adds. Keys are
ignored while focus is in a text field, so they don't interfere with
actually using the sites' own search boxes. A small status badge
(bottom-right of the import page) shows the current row and how many of
the 4 tabs are still open/captured.

Moving to the next row is always manual (**I**) - nothing auto-advances,
so the loop is "I to start, look/Y/U ×4, [auto-submits], I for the next
row" whenever you're ready.

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
