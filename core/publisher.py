"""Publishes articles to a GitHub Pages site via the GitHub Contents API.

Path traversal is blocked structurally: every file path is built from a
slug produced by core.security.safe_slug, which only ever contains
[a-z0-9-], so generated content can never write outside its own article
folder or into something like a CI workflow file.
"""

import base64

import requests

from config import settings
from core import database as db
from core.security import safe_slug, sanitize_html

API_ROOT = "https://api.github.com"
TIMEOUT = 30


class PublishError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.GITHUB_TOKEN and settings.GITHUB_USERNAME and settings.GITHUB_REPO)


def site_url():
    if not settings.GITHUB_USERNAME or not settings.GITHUB_REPO:
        return None
    return f"https://{settings.GITHUB_USERNAME}.github.io/{settings.GITHUB_REPO}/"


def _headers():
    return {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _contents_url(path: str) -> str:
    return f"{API_ROOT}/repos/{settings.GITHUB_USERNAME}/{settings.GITHUB_REPO}/contents/{path}"


def _get_file(path: str):
    resp = requests.get(
        _contents_url(path),
        headers=_headers(),
        params={"ref": settings.GITHUB_BRANCH},
        timeout=TIMEOUT,
    )
    if resp.status_code == 200:
        return resp.json()
    return None


def _put_file(path: str, content_str: str, message: str):
    existing = _get_file(path)
    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
        "branch": settings.GITHUB_BRANCH,
    }
    if existing:
        payload["sha"] = existing["sha"]
    resp = requests.put(_contents_url(path), headers=_headers(), json=payload, timeout=TIMEOUT)
    if resp.status_code not in (200, 201):
        raise PublishError(f"GitHub publish failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


ARTICLE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
</head>
<body>
<article>
<h1>{title}</h1>
{body}
</article>
<p><a href="../index.html">&larr; Back to home</a></p>
</body>
</html>
"""

INDEX_HEADER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Articles</title>
</head>
<body>
<h1>Articles</h1>
<ul id="article-list">
"""
INDEX_FOOTER = """</ul>
</body>
</html>
"""


def _get_existing_index_items(index_path: str) -> list:
    existing = _get_file(index_path)
    if not existing:
        return []
    content = base64.b64decode(existing["content"]).decode("utf-8", errors="ignore")
    return [line + "\n" for line in content.splitlines() if "<li>" in line]


def _update_index(folder: str, title: str, slug: str):
    index_path = f"{folder}/index.html"
    items = _get_existing_index_items(index_path)
    # Match on the href, not the whole line: a re-publish under the same
    # slug with a changed title would otherwise fail the exact-line check
    # and add a second, stale entry pointing at the same page instead of
    # replacing the old one.
    href_marker = f'href="articles/{slug}.html"'
    items = [line for line in items if href_marker not in line]
    entry = f'  <li><a href="articles/{slug}.html">{_escape(title)}</a></li>\n'
    items.insert(0, entry)
    html = INDEX_HEADER + "".join(items) + INDEX_FOOTER
    _put_file(index_path, html, message=f"Update index: {title}")


def _unique_slug(base_slug: str) -> str:
    """Disambiguate a slug against already-published articles so two
    different titles that happen to truncate/sanitize to the same slug
    don't silently overwrite each other's page on the live site."""
    existing = {a["slug"] for a in db.list_articles()}
    if base_slug not in existing:
        return base_slug
    n = 2
    while f"{base_slug}-{n}" in existing:
        n += 1
    return f"{base_slug}-{n}"


def publish_article(bot_name: str, title: str, body_html: str) -> dict:
    if not is_configured():
        raise PublishError(
            "Publishing isn't set up yet — GITHUB_TOKEN/GITHUB_USERNAME/GITHUB_REPO missing."
        )

    folder = settings.GITHUB_PAGES_FOLDER.strip("/")
    slug = _unique_slug(safe_slug(title)[:80])
    clean_body = sanitize_html(body_html)
    article_path = f"{folder}/articles/{slug}.html"
    html = ARTICLE_TEMPLATE.format(title=_escape(title), body=clean_body)

    _put_file(article_path, html, message=f"Publish: {title} ({bot_name})")
    _update_index(folder, title, slug)

    url = f"{site_url()}articles/{slug}.html" if site_url() else None
    return {"slug": slug, "path": article_path, "url": url}
