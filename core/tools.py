"""
Research tools: parallel, cached, resilient.

Three changes from the old version:
  1. The three searches run at the same time instead of one after another.
  2. Results are cached on disk and shared by every bot, so six bots asking
     the same question costs one search, not six.
  3. Multiple search backends, because DuckDuckGo's HTML layout changes and
     the old single CSS selector was silently returning junk.
"""
import ipaddress
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup

TIMEOUT = 8
CACHE_TTL = 6 * 60 * 60          # 6 hours
CACHE_FILE = Path(__file__).resolve().parent.parent / "knowledge" / "research_cache.json"

_UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"

# One pooled session instead of a fresh TCP + TLS handshake per request.
_session = requests.Session()
_session.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})

_lock = threading.Lock()
_cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        _cache = {}
    return _cache


def _cache_get(key: str):
    c = _load_cache()
    hit = c.get(key)
    if not hit:
        return None
    if time.time() - hit.get("ts", 0) > CACHE_TTL:
        return None
    return hit.get("value")


def _cache_put(key: str, value: str):
    with _lock:
        c = _load_cache()
        c[key] = {"ts": time.time(), "value": value}
        # Keep the file small: drop anything already expired.
        cutoff = time.time() - CACHE_TTL
        for k in [k for k, v in c.items() if v.get("ts", 0) < cutoff]:
            c.pop(k, None)
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(c), encoding="utf-8")
        except Exception:
            pass


def _is_safe_url(url: str) -> bool:
    """fetch_page() follows links the bots find in search results — pages an
    attacker can influence just by getting them to rank. Without this, a
    result pointing at http://169.254.169.254/ (the cloud metadata endpoint
    on most hosts) or a private/internal address gets fetched like any other
    page."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip_str = info[4][0]
        if ip_str == "169.254.169.254":
            return False
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def _clean_ddg_link(href: str) -> str:
    """DuckDuckGo wraps results in a redirect. Unwrap it."""
    if "uddg=" in href:
        try:
            return unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        except Exception:
            return href
    if href.startswith("//"):
        return "https:" + href
    return href


def _parse_results(html: str, max_results: int) -> list:
    soup = BeautifulSoup(html, "lxml")
    out = []
    # Try several selectors; layouts change and one alone goes stale.
    for selector in ("a.result__a", "a.result-link", "h2 a", "article a[href]"):
        for a in soup.select(selector):
            text = a.get_text(strip=True)
            href = _clean_ddg_link(a.get("href", ""))
            if len(text) > 15 and href.startswith("http"):
                out.append(f"- {text[:120]}\n  {href}")
            if len(out) >= max_results:
                return out
        if out:
            return out
    return out


def web_search(query: str, max_results: int = 5) -> str:
    key = f"search::{query}::{max_results}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    endpoints = [
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}",
    ]
    for url in endpoints:
        try:
            r = _session.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            results = _parse_results(r.text, max_results)
            if results:
                value = "\n".join(results)
                _cache_put(key, value)
                return value
        except Exception:
            continue

    fallback = (
        "Search unavailable right now. Known free static hosts: "
        "GitHub Pages, Netlify, Cloudflare Pages."
    )
    # Cache failures briefly too, so six bots don't all retry a dead endpoint.
    _cache_put(key, fallback)
    return fallback


def fetch_page(url: str, max_chars: int = 3000) -> str:
    key = f"page::{url}::{max_chars}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if not _is_safe_url(url):
        return "Could not read page: refusing to fetch a private/internal address"
    try:
        r = _session.get(url, timeout=TIMEOUT, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location", "")
            if not location or not _is_safe_url(location):
                return "Could not read page: refused an unsafe redirect"
            r = _session.get(location, timeout=TIMEOUT, allow_redirects=False)
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n", strip=True))
        value = text[:max_chars]
        _cache_put(key, value)
        return value
    except Exception as e:
        return f"Could not read page: {e}"


def search_many(queries: list, max_results: int = 5) -> dict:
    """
    Run several searches at once. Returns {query: result}.
    Cached queries cost nothing and never hit the network.
    """
    queries = list(dict.fromkeys(q for q in queries if q))
    if not queries:
        return {}
    results = {}
    todo = []
    for q in queries:
        cached = _cache_get(f"search::{q}::{max_results}")
        if cached is not None:
            results[q] = cached
        else:
            todo.append(q)
    if todo:
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            for q, value in zip(todo, pool.map(lambda x: web_search(x, max_results), todo)):
                results[q] = value
    return results
