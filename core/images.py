"""Finds and fetches openly-licensed images for articles via Openverse.

Openverse (run by WordPress/Automattic) indexes CC-licensed and
public-domain images and needs no API key. Results are filtered to
licenses permitting commercial use and modification, since this is a
monetized site, and every image carries the attribution string the
license requires — rendered visibly under the image by the publisher.

Images are downloaded and republished to the site rather than hotlinked:
hotlinked images break when the source moves them, and some hosts block
external embedding outright.
"""

import requests

from core.security import is_safe_url

SEARCH_URL = "https://api.openverse.org/v1/images/"
USER_AGENT = "MoneyBotResearcher/1.0"
TIMEOUT = 15
MAX_IMAGE_BYTES = 3_000_000

# Extensions by content type — also the allowlist of what we'll accept.
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def find_image(query: str):
    """Return metadata for one openly-licensed image matching query, or
    None. Never raises — an article without an image is fine."""
    if not is_safe_url(SEARCH_URL):
        return None
    try:
        resp = requests.get(
            SEARCH_URL,
            params={
                "q": query,
                "page_size": 5,
                # Commercial use + modification: this is a monetized site.
                "license_type": "commercial,modification",
                "mature": "false",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (requests.RequestException, ValueError):
        return None

    for item in results:
        # Prefer Openverse's own thumbnail proxy over the origin URL: many
        # upstream CDNs (Flickr's among them) return 403 to non-browser
        # user agents, so hotlinking the origin mostly fails.
        url = item.get("thumbnail") or item.get("url")
        if not url or not is_safe_url(url):
            continue
        return {
            "url": url,
            "title": item.get("title") or query,
            "creator": item.get("creator") or "Unknown",
            "creator_url": item.get("creator_url") or "",
            "license": (item.get("license") or "").upper(),
            "license_version": item.get("license_version") or "",
            "license_url": item.get("license_url") or "",
            "source_url": item.get("foreign_landing_url") or url,
            "attribution": item.get("attribution") or "",
        }
    return None


def download_image(url: str):
    """Fetch an image, returning (bytes, extension) or None.

    SSRF-checked, size-capped, and content-type validated — this fetches a
    URL chosen from third-party search results, so none of that is
    optional.
    """
    if not is_safe_url(url):
        return None
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if not extension:
        resp.close()
        return None

    try:
        data = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
    except Exception:
        return None
    finally:
        resp.close()

    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    return data, extension
