# backend/anilist.py

import requests

def search_manga_by_title(title):
    query = '''
    query ($search: String) {
      Media(search: $search, type: MANGA) {
        id
        title {
          english
          romaji
          native
        }
        synonyms
        coverImage {
          extraLarge
          large
        }
        bannerImage
        status
        chapters
        volumes
      }
    }
    '''
    url = 'https://graphql.anilist.co'  # No spaces
    try:
        response = requests.post(
            url,
            json={'query': query, 'variables': {'search': title}},
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            media = data.get('data', {}).get('Media')
            if media:
                cover_img = media.get('coverImage') or {}
                synonyms = media.get('synonyms') or []
                return {
                    'anilist_id': media['id'],
                    'title_en': media['title'].get('english'),
                    'title_romaji': media['title'].get('romaji'),
                    'title_native': media['title'].get('native'),
                    'synonyms': synonyms,  # ← NEW
                    'cover_url': cover_img.get('extraLarge') or cover_img.get('large'),
                    'banner_url': media.get('bannerImage'),
                    'status': media.get('status'),
                    'chapters': media.get('chapters'),
                    'volumes': media.get('volumes')
                }
    except Exception as e:
        print(f"[AniList] Search failed for '{title}': {e}")
    return None