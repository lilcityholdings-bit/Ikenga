"""
One site per bot, growing over time.

The old build created a brand new one-page site every other article, so twenty
articles became ten orphan sites with no internal links. Search engines reward
one site that deepens, so every article joins the same site here.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from config.settings import MIN_ARTICLE_CHARS
from core.safe_html import esc, paragraphs

ROOT = Path(__file__).resolve().parent.parent
SITES = ROOT / "sites"
SITES.mkdir(parents=True, exist_ok=True)

_FILLER = re.compile(
    r"(?i)\b(in today's (fast-paced|digital) world|in conclusion|when it comes to"
    r"|at the end of the day|dive into|unlock the (secrets|power))\b"
)


def _mattr(words, window=60):
    """
    Moving-average type-token ratio.

    A plain unique/total ratio falls as text gets longer no matter how well it
    is written, so a fixed threshold on it silently rejects longer articles.
    This averages the ratio over sliding windows instead, which stays flat with
    length and measures what we actually care about: local repetition.
    """
    if len(words) <= window:
        return len(set(words)) / max(len(words), 1)
    ratios = [len(set(words[i:i + window])) / window
              for i in range(0, len(words) - window + 1, max(1, window // 4))]
    return sum(ratios) / len(ratios)


def _repeated_sentences(text):
    """Exact duplicate sentences are the real spam signal."""
    parts = [s.strip().lower() for s in re.split(r"[.!?\n]+", text or "") if len(s.strip()) > 30]
    return len(parts) - len(set(parts))


def slugify(text, n=45):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "page").lower()).strip("-")
    return s[:n] or "page"


def quality_check(title, body):
    """Cheap gate before anything is written to disk."""
    title, body = (title or "").strip(), (body or "").strip()
    if len(title) < 8:
        return False, "Title too short"
    if len(body) < MIN_ARTICLE_CHARS:
        return False, f"Body under {MIN_ARTICLE_CHARS} characters"
    words = re.findall(r"[a-zA-Z]{3,}", body.lower())
    if len(words) < 45:
        return False, "Not enough real words"
    if _mattr(words) < 0.55:
        return False, "Repetitive"
    if _repeated_sentences(body) >= 3:
        return False, "Duplicate sentences"
    if body.lower().count("http") > 12:
        return False, "Too many links"
    if len(_FILLER.findall(body)) >= 5:
        return False, "Heavy filler phrasing"
    return True, "ok"


def site_dir(bot_id):
    return SITES / f"bot{bot_id}"


def _analytics():
    """Owner-supplied snippet. Without it the whole system optimises blind."""
    try:
        from core.db import get_setting
        snip = (get_setting("analytics_snippet", "") or "").strip()
        # Only allow a script tag, and only from the owner's own settings.
        return snip if snip.startswith("<script") else ""
    except Exception:
        return ""


def _page_html(site_name, title, inner, nav):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} — {esc(site_name)}</title>
<meta name="description" content="{esc(title[:150])}">
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 700px;
         margin: 0 auto; padding: 32px 18px; line-height: 1.65; color: #1a1a1a; }}
  nav {{ font-size: 0.85em; padding-bottom: 12px; margin-bottom: 28px;
         border-bottom: 1px solid #e5e5e5; }}
  h1 {{ font-size: 1.7em; line-height: 1.25; }}
  h2 {{ font-size: 1.15em; margin-top: 1.8em; }}
  a {{ color: #0057d8; }}
  li {{ margin: 6px 0; }}
  footer {{ margin-top: 56px; padding-top: 14px; border-top: 1px solid #e5e5e5;
            font-size: 0.85em; color: #666; }}
</style>
{_analytics()}
</head>
<body>
<nav>{nav}</nav>
<h1>{esc(title)}</h1>
{inner}
<footer><p>Some links on this page may be affiliate links.</p></footer>
</body>
</html>
"""


def add_page(bot_id, title, body, site_name=""):
    """Add one page and rebuild the site. Returns (ok, path_or_reason, slug)."""
    ok, reason = quality_check(title, body)
    if not ok:
        return False, reason, ""

    d = site_dir(bot_id)
    d.mkdir(parents=True, exist_ok=True)
    index = d / "pages.json"

    try:
        pages = json.loads(index.read_text(encoding="utf-8"))
    except Exception:
        pages = []

    # Counter, not a timestamp. Two pages written in the same second would
    # otherwise get the same slug and silently overwrite each other.
    base = slugify(title)
    existing = {p["slug"] for p in pages}
    slug, n = base, 2
    while slug in existing:
        slug = f"{base}-{n}"
        n += 1

    pages.append({"slug": slug, "title": title, "body": body,
                  "added": datetime.utcnow().isoformat()})
    index.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")

    _rebuild(d, pages, site_name or f"Site {bot_id}")
    return True, str(d), slug


def _rebuild(d, pages, site_name):
    nav = '<a href="index.html">Home</a>'
    for p in pages:
        (d / f"{p['slug']}.html").write_text(
            _page_html(site_name, p["title"], paragraphs(p["body"]), nav),
            encoding="utf-8",
        )
    listing = "\n  ".join(
        f'<li><a href="{esc(p["slug"])}.html">{esc(p["title"])}</a></li>'
        for p in reversed(pages)
    )
    (d / "index.html").write_text(
        _page_html(site_name, site_name, f"<ul>\n  {listing}\n</ul>", ""),
        encoding="utf-8",
    )
    sm = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    sm += "  <url><loc>index.html</loc></url>\n"
    for p in pages:
        sm += f"  <url><loc>{esc(p['slug'])}.html</loc></url>\n"
    sm += "</urlset>\n"
    (d / "sitemap.xml").write_text(sm, encoding="utf-8")
    (d / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: sitemap.xml\n", encoding="utf-8")


def remove_page(bot_id, slug):
    """
    Take a page out of the site and rebuild.

    Rejection used to only flip a database row, so a rejected page stayed in
    pages.json and went live again the moment any sibling was published. A
    rejection has to change what is actually on the site.
    """
    d = site_dir(bot_id)
    index = d / "pages.json"
    try:
        pages = json.loads(index.read_text(encoding="utf-8"))
    except Exception:
        return False, "no pages file"

    keep = [pg for pg in pages if pg["slug"] != slug]
    if len(keep) == len(pages):
        return False, "page not found"

    index.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    stale = d / f"{slug}.html"
    if stale.exists():
        stale.unlink()

    if keep:
        _rebuild(d, keep, _site_name(d))
    else:
        for f in d.glob("*.html"):
            f.unlink()
    return True, slug


def _site_name(d):
    try:
        first = (d / "index.html").read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r"<title>(.*?) —", first)
        if m:
            return m.group(1)
    except Exception:
        pass
    return d.name


def read_page(path, slug=""):
    """Read one specific page back out, for the critic and the dashboard."""
    try:
        pages = json.loads((Path(path) / "pages.json").read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    if not pages:
        return "", ""
    if slug:
        for pg in pages:
            if pg["slug"] == slug:
                return pg["title"], pg["body"]
    return pages[-1]["title"], pages[-1]["body"]


def page_count(bot_id):
    try:
        return len(json.loads((site_dir(bot_id) / "pages.json").read_text(encoding="utf-8")))
    except Exception:
        return 0
