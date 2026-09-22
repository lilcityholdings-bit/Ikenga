"""
Publish a generated site straight to GitHub Pages using the GitHub REST API.

No git, no terminal, no file manager. This is what makes the whole thing
usable from a phone.

Required environment variables:
  GITHUB_TOKEN     fine-grained token, Contents: Read and write, THIS REPO ONLY
  GITHUB_USERNAME  your github username
  GITHUB_REPO      the repo name that serves your site

Repo setup (one time, from the GitHub mobile site):
  1. Create a PUBLIC repo
  2. Settings > Pages > Source: Deploy from a branch
  3. Branch: main, Folder: /docs
"""
import base64
import json
import os
import re
from pathlib import Path

import requests

API = "https://api.github.com"
TIMEOUT = 20


def _cfg():
    from core.db import get_secret
    return (get_secret("GITHUB_TOKEN"), get_secret("GITHUB_USERNAME"),
            get_secret("GITHUB_REPO"))


def is_configured() -> bool:
    token, user, repo = _cfg()
    return bool(token and user and repo)


def _headers(token: str):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe_slug(text: str) -> str:
    """Allowlist-only slug: only [a-z0-9-] survive. This is the entire
    defense against a slug escaping the docs/<slug>/ prefix it's built into
    — a bot- or LLM-chosen title like "../../.github/workflows/evil" must
    never reach the GitHub API path unsanitized, since that path can write
    a workflow file that runs with the repo's own secrets. A denylist on
    ".." alone (what some call sites here used to do) misses this case:
    every character survives it, only the literal substring is blocked."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or "untitled"


def _put_file(token: str, user: str, repo: str, repo_path: str, content_bytes: bytes, message: str):
    url = f"{API}/repos/{user}/{repo}/contents/{repo_path}"
    headers = _headers(token)

    sha = None
    try:
        existing = requests.get(url, headers=headers, timeout=TIMEOUT)
        if existing.status_code == 200:
            sha = existing.json().get("sha")
    except Exception:
        pass

    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=headers, json=payload, timeout=TIMEOUT)
    if r.status_code in (401, 403):
        # Fine-grained tokens expire after 90 days. Say so plainly rather than
        # letting publishing quietly stop working.
        raise RuntimeError(
            "GitHub refused the token. It has probably expired (fine-grained "
            "tokens last 90 days) or lost Contents: Read and write. "
            "Make a new one and paste it into Setup.")
    if r.status_code == 409:
        raise RuntimeError("GitHub had a conflicting write. Try publishing again.")
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub rejected {repo_path}: {r.status_code} {r.text[:200]}")
    return True


def publish_site_folder(site_dir: str, slug: str = "") -> str:
    """
    Upload every .html/.xml/.txt/.css file in a site folder to docs/<slug>/.
    Returns the live URL.
    """
    token, user, repo = _cfg()
    if not (token and user and repo):
        raise RuntimeError(
            "GitHub publishing is not configured. Set GITHUB_TOKEN, "
            "GITHUB_USERNAME and GITHUB_REPO."
        )

    folder = Path(site_dir)
    if not folder.is_dir():
        raise RuntimeError(f"Not a folder: {site_dir}")

    slug = _safe_slug(slug or folder.name)
    allowed = {".html", ".xml", ".txt", ".css", ".md"}
    # Owner-facing kit files stay local; they are not part of the website.
    skip = {"pages.json", ".published.json"}

    # Only send what changed. A 50-page site was 50 API writes and 50 commits
    # on every publish, which GitHub throttles. A local manifest of content
    # hashes means a normal publish sends one new page and the index.
    import hashlib
    manifest_file = folder / ".published.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        manifest = {}

    uploaded, skipped = 0, 0
    seen = set()
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in allowed or f.name in skip:
            continue
        data = f.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        seen.add(f.name)
        if manifest.get(f.name) == digest:
            skipped += 1
            continue
        _put_file(token, user, repo, f"docs/{slug}/{f.name}", data,
                  f"Publish {slug}/{f.name}")
        manifest[f.name] = digest
        uploaded += 1

    # Anything gone locally (a rejected page) is deleted from the live site.
    for name in [n for n in manifest if n not in seen]:
        try:
            unpublish_page(slug, name[:-5] if name.endswith(".html") else name)
        except Exception:
            pass
        manifest.pop(name, None)

    try:
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    except Exception:
        pass

    if uploaded == 0 and skipped == 0:
        raise RuntimeError("No publishable files found in that folder")

    return f"https://{user}.github.io/{repo}/{slug}/"


def publish_article_file(md_path: str, slug: str = "") -> str:
    """Turn a single markdown article into an HTML page and publish it."""
    from core.safe_html import escape_text, paragraphs

    token, user, repo = _cfg()
    if not (token and user and repo):
        raise RuntimeError("GitHub publishing is not configured.")

    p = Path(md_path)
    if not p.is_file():
        raise RuntimeError(f"Not a file: {md_path}")

    raw = p.read_text(encoding="utf-8")
    lines = raw.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else p.stem
    body = "\n".join(lines[1:]).strip()

    slug = _safe_slug(slug or p.stem)
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape_text(title)}</title>
  <meta name="description" content="{escape_text(title[:150])}">
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; line-height: 1.6; }}
    footer {{ margin-top: 40px; font-size: 0.9em; color: #666; }}
  </style>
</head>
<body>
  <h1>{escape_text(title)}</h1>
  {paragraphs(body)}
  <footer><p>Some links may be affiliate links.</p></footer>
</body>
</html>
"""
    _put_file(token, user, repo, f"docs/{slug}/index.html",
              html_doc.encode("utf-8"), f"Publish article {slug}")
    return f"https://{user}.github.io/{repo}/{slug}/"


def create_site_repo(name: str = "mysite") -> dict:
    """
    Create the public site repo and switch GitHub Pages on, from the app.

    This replaces the fiddliest part of setup: making a repo, finding Settings,
    finding Pages, and picking main + /docs on a phone browser.

    The token needs 'Administration: Read and write' as well as Contents for
    this. If it does not have that, we say so and fall back to the manual path.
    """
    from core.db import get_secret, set_secret
    token = get_secret("GITHUB_TOKEN")
    if not token:
        return {"ok": False, "detail": "Paste your GitHub token first."}

    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "mysite")).strip("-") or "mysite"
    h = _headers(token)

    who = requests.get(f"{API}/user", headers=h, timeout=TIMEOUT)
    if who.status_code != 200:
        return {"ok": False, "detail": "Token rejected by GitHub. Check you copied all of it."}
    user = who.json().get("login", "")
    set_secret("GITHUB_USERNAME", user)

    exists = requests.get(f"{API}/repos/{user}/{name}", headers=h, timeout=TIMEOUT)
    if exists.status_code != 200:
        made = requests.post(
            f"{API}/user/repos", headers=h,
            json={"name": name, "private": False, "auto_init": True,
                  "description": "Published by Revenue Bots"},
            timeout=TIMEOUT)
        if made.status_code not in (200, 201):
            return {"ok": False,
                    "detail": "Could not create the repo. Your token likely lacks "
                              "'Administration: Read and write'. Create the repo by "
                              "hand and type its name below instead."}

    set_secret("GITHUB_REPO", name)

    # Pages needs something in docs/ before it will build.
    try:
        _put_file(token, user, name, "docs/index.html",
                  b"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                  b"<title>Coming soon</title></head><body>"
                  b"<p>This site is being set up.</p></body></html>",
                  "Initialise site")
    except Exception:
        pass

    pages = requests.post(
        f"{API}/repos/{user}/{name}/pages", headers=h,
        json={"source": {"branch": "main", "path": "/docs"}}, timeout=TIMEOUT)
    if pages.status_code in (201, 204):
        state = "Pages enabled"
    elif pages.status_code == 409:
        state = "Pages already on"
    else:
        state = ("repo ready, but turn Pages on by hand: Settings > Pages > "
                 "main + /docs")

    return {"ok": True, "user": user, "repo": name, "detail": state,
            "url": f"https://{user}.github.io/{name}/"}


def check_connection() -> dict:
    """Used by the health panel."""
    token, user, repo = _cfg()
    if not (token and user and repo):
        return {"ok": False, "detail": "Missing GITHUB_TOKEN / GITHUB_USERNAME / GITHUB_REPO"}
    try:
        r = requests.get(f"{API}/repos/{user}/{repo}", headers=_headers(token), timeout=TIMEOUT)
        if r.status_code == 200:
            return {"ok": True, "detail": f"Connected to {user}/{repo}"}
        if r.status_code == 404:
            return {"ok": False, "detail": "Repo not found, or the token cannot see it"}
        if r.status_code in (401, 403):
            return {"ok": False, "detail": "Token rejected. Check it has Contents: Read and write"}
        return {"ok": False, "detail": f"GitHub returned {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": f"Could not reach GitHub: {e}"}

def unpublish_page(site_slug: str, page_slug: str) -> int:
    """
    Remove ONE page from a published site.

    The old takedown passed the site folder to unpublish(), which deleted every
    page that bot had ever written. Taking down one bad article should not
    destroy the rest of the site.
    """
    token, user, repo = _cfg()
    if not (token and user and repo):
        raise RuntimeError("GitHub publishing is not configured.")
    if not (site_slug or "").strip() or not (page_slug or "").strip():
        raise RuntimeError("Bad slug")
    site_slug = _safe_slug(site_slug)
    page_slug = _safe_slug(page_slug)

    path = f"docs/{site_slug}/{page_slug}.html"
    r = requests.get(f"{API}/repos/{user}/{repo}/contents/{path}",
                     headers=_headers(token), timeout=TIMEOUT)
    if r.status_code == 404:
        return 0
    if r.status_code != 200:
        raise RuntimeError(f"Could not find {path}: {r.status_code}")
    sha = r.json().get("sha")
    d = requests.delete(f"{API}/repos/{user}/{repo}/contents/{path}",
                        headers=_headers(token),
                        json={"message": f"Take down {path}", "sha": sha},
                        timeout=TIMEOUT)
    return 1 if d.status_code in (200, 201) else 0


def unpublish(slug: str) -> int:
    """
    Delete a published folder from the site. Used by autopilot take-downs.
    Returns how many files were removed.
    """
    token, user, repo = _cfg()
    if not (token and user and repo):
        raise RuntimeError("GitHub publishing is not configured.")

    if not (slug or "").strip():
        raise RuntimeError("Bad slug")
    slug = _safe_slug(slug)

    listing = requests.get(
        f"{API}/repos/{user}/{repo}/contents/docs/{slug}",
        headers=_headers(token), timeout=TIMEOUT,
    )
    if listing.status_code == 404:
        return 0
    if listing.status_code != 200:
        raise RuntimeError(f"Could not list docs/{slug}: {listing.status_code}")

    removed = 0
    for entry in listing.json():
        if entry.get("type") != "file":
            continue
        r = requests.delete(
            f"{API}/repos/{user}/{repo}/contents/{entry['path']}",
            headers=_headers(token),
            json={"message": f"Take down {entry['path']}", "sha": entry["sha"]},
            timeout=TIMEOUT,
        )
        if r.status_code in (200, 201):
            removed += 1
    return removed
