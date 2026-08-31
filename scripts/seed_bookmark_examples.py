"""
One-time creation of the two example filter bookmarks discussed when the
saved-views feature was built:
  - "want to read": Plan to Read status + a "want to read" custom tag
    (creates that tag if it doesn't already exist)
  - "Default - All Ratings": same as the built-in Default, but without the
    Mature/Explicit exclusion

Usage: venv\\Scripts\\python.exe scripts\\seed_bookmark_examples.py
(or venv/bin/python3 scripts/seed_bookmark_examples.py on Linux)

Safe to re-run - skips any bookmark whose name already exists.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import (
    init_db, create_custom_tag, get_filter_bookmarks, create_filter_bookmark
)

BASE_STATE = {
    'sort': 'unread_first',
    'dir': 'asc',
    'type': [],
    'genre': [],
    'pubStatus': [],
    'readableOn': [],
}


def main():
    init_db()

    existing_names = {b['name'] for b in get_filter_bookmarks()}

    if 'want to read' in existing_names:
        print('[skip] "want to read" bookmark already exists')
    else:
        tag_id = create_custom_tag('want to read')
        if tag_id is None:
            print('[fail] Could not create/find the "want to read" custom tag')
        else:
            filter_state = {
                **BASE_STATE,
                'status': 'plan_to_read',
                'rating': [{'name': 'mature', 'mode': 'exclude'}, {'name': 'explicit', 'mode': 'exclude'}],
                'customTags': [tag_id],
            }
            new_id = create_filter_bookmark('want to read', filter_state)
            print(f'[ok]   Created "want to read" bookmark (id={new_id}, tag id={tag_id})'
                  if new_id else '[fail] Failed to create "want to read" bookmark')

    if 'Default - All Ratings' in existing_names:
        print('[skip] "Default - All Ratings" bookmark already exists')
    else:
        filter_state = {
            **BASE_STATE,
            'status': 'reading',
            'rating': [],  # no mature/explicit exclusion
            'customTags': [],
        }
        new_id = create_filter_bookmark('Default - All Ratings', filter_state)
        print(f'[ok]   Created "Default - All Ratings" bookmark (id={new_id})'
              if new_id else '[fail] Failed to create "Default - All Ratings" bookmark')


if __name__ == "__main__":
    main()
