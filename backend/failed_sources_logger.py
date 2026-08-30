# backend/failed_sources_logger.py
#
# Dead-simple companion to error_logger.py: one line per failed source add,
# "Title : URL", for quickly revisiting/re-searching sources that didn't
# make it in - no timestamps or JSON structure, just enough to scan or
# copy a link back out.
import os

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "failed_sources.log")

def log_failed_source(title, url):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        title = (title or "Unknown").strip()
        url = (url or "").strip()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{title} : {url}\n")
    except Exception:
        pass
