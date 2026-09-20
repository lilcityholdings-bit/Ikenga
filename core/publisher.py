"""Publishes articles to a GitHub Pages site via the GitHub Contents API.

Path traversal is blocked structurally: every file path is built from a
slug produced by core.security.safe_slug, which only ever contains
[a-z0-9-], so generated content can never write outside its own article
folder or into something like a CI workflow file.

Also owns the on-page SEO a static-HTML publisher has to do by hand:
meta description, Open Graph tags, JSON-LD article markup, an FTC
affiliate disclosure, and a sitemap.xml + robots.txt so search engines
have something to crawl from. None of this guarantees ranking — a brand
new site with no backlinks is going to be slow to get noticed regardless
— but skipping it makes indexing slower still for no reason.
"""

import base64
import json
import re

import requests

from config import settings
from core import database as db
from core.security import safe_slug, sanitize_html

API_ROOT = "https://api.github.com"
TIMEOUT = 30
DISCLOSURE = (
    "Disclosure: this site may earn a commission from links on this page, "
    "at no extra cost to you."
)


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


def put_text_file(path: str, content_str: str, message: str):
    """Public wrapper for callers outside this module (e.g. IndexNow's key
    file) that need to publish an arbitrary text file to the site repo."""
    return _put_file(path, content_str, message)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _meta_description(body_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 155:
        text = text[:152].rsplit(" ", 1)[0] + "..."
    return text


def _mark_sponsored_links(body_html: str, affiliate_urls: set) -> str:
    """Force rel="sponsored nofollow noopener" onto any link that points
    at one of our own affiliate URLs, regardless of whether the LLM
    included it — this is required by Google's link-attribution
    guidelines for paid/affiliate links and shouldn't depend on the model
    remembering to add it."""
    if not affiliate_urls:
        return body_html

    def repl(match):
        tag = match.group(0)
        href_match = re.search(r'href="([^"]*)"', tag)
        if not href_match or href_match.group(1) not in affiliate_urls:
            return tag
        tag = re.sub(r'\s+rel="[^"]*"', "", tag)
        return tag[:-1] + ' rel="sponsored nofollow noopener">'

    return re.sub(r"<a\b[^>]*>", repl, body_html)


ARTICLE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{canonical}">
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"Article","headline":{title_json},"description":{description_json},"url":{canonical_json}}}
</script>
</head>
<body>
<article>
<h1>{title}</h1>
<p><em>{disclosure}</em></p>
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

ABOUT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About</title>
</head>
<body>
<h1>About this site</h1>
<p>Articles here are researched and drafted with the help of an AI
writing assistant, working from real web research, and published
automatically. {disclosure}</p>
</body>
</html>
"""

ROBOTS_TEMPLATE = """User-agent: *
Allow: /

Sitemap: {sitemap_url}
"""


def _json_string(text: str) -> str:
    return json.dumps(text)


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


def _update_sitemap(folder: str):
    base = site_url()
    if not base:
        return
    urls = [base] + [
        f"{base}articles/{a['slug']}.html" for a in db.list_articles()
    ]
    body = "".join(f"  <url><loc>{_escape(u)}</loc></url>\n" for u in urls)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}"
        "</urlset>\n"
    )
    _put_file(f"{folder}/sitemap.xml", xml, message="Update sitemap.xml")


def _ensure_robots_txt(folder: str):
    path = f"{folder}/robots.txt"
    if _get_file(path):
        return
    base = site_url()
    content = ROBOTS_TEMPLATE.format(sitemap_url=f"{base}sitemap.xml" if base else "")
    _put_file(path, content, message="Add robots.txt")


def _ensure_about_page(folder: str):
    path = f"{folder}/about.html"
    if _get_file(path):
        return
    content = ABOUT_TEMPLATE.format(disclosure=DISCLOSURE)
    _put_file(path, content, message="Add about page")


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


def publish_article(bot_name: str, title: str, body_html: str, affiliate_urls: set = None) -> dict:
    if not is_configured():
        raise PublishError(
            "Publishing isn't set up yet — GITHUB_TOKEN/GITHUB_USERNAME/GITHUB_REPO missing."
        )

    folder = settings.GITHUB_PAGES_FOLDER.strip("/")
    slug = _unique_slug(safe_slug(title)[:80])
    clean_body = sanitize_html(body_html)
    clean_body = _mark_sponsored_links(clean_body, affiliate_urls or set())

    canonical = f"{site_url()}articles/{slug}.html" if site_url() else ""
    description = _meta_description(clean_body)
    html = ARTICLE_TEMPLATE.format(
        title=_escape(title),
        description=_escape(description),
        canonical=canonical,
        title_json=_json_string(title),
        description_json=_json_string(description),
        canonical_json=_json_string(canonical),
        disclosure=DISCLOSURE,
        body=clean_body,
    )
    article_path = f"{folder}/articles/{slug}.html"

    _put_file(article_path, html, message=f"Publish: {title} ({bot_name})")
    _update_index(folder, title, slug)
    _update_sitemap(folder)
    _ensure_robots_txt(folder)
    _ensure_about_page(folder)

    return {"slug": slug, "path": article_path, "url": canonical or None}
