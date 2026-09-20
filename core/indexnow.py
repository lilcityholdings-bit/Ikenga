"""Pushes newly-published URLs to IndexNow (Bing, Yandex, and other
participating engines) instead of waiting for organic re-crawl to
discover them.

Free, no signup: the protocol's only "credential" is a random key that
gets published as a text file at the site root, proving control of the
domain. Google doesn't participate in IndexNow (and retired its own
sitemap-ping endpoint in 2023), so this doesn't help Google discovery —
robots.txt + sitemap.xml is what that relies on — but it's a real,
working, zero-cost lever for the engines that do support it.
"""

import requests

from config import settings
from core import database as db, publisher

TIMEOUT = 10
_key_file_published = False


def _get_or_create_key() -> str:
    key = db.get_setting("indexnow_key")
    if not key:
        import secrets as _secrets

        key = _secrets.token_hex(16)
        db.set_setting("indexnow_key", key)
    return key


def ensure_key_file_published():
    """Idempotent per-process: only bothers GitHub once per worker run,
    not on every publish."""
    global _key_file_published
    if _key_file_published or not publisher.is_configured():
        return
    key = _get_or_create_key()
    folder = settings.GITHUB_PAGES_FOLDER.strip("/")
    publisher.put_text_file(f"{folder}/{key}.txt", key, message="IndexNow key file")
    _key_file_published = True


def submit_url(url: str):
    if not url or not settings.GITHUB_USERNAME:
        return
    key = _get_or_create_key()
    host = f"{settings.GITHUB_USERNAME}.github.io"
    base = publisher.site_url()
    if not base:
        return
    try:
        requests.post(
            "https://api.indexnow.org/indexnow",
            json={
                "host": host,
                "key": key,
                "keyLocation": f"{base}{key}.txt",
                "urlList": [url],
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        pass  # best-effort — normal crawling still finds the page either way
