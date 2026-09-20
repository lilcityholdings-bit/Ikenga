"""Web research tools available to bots.

Everything here is a boundary the bot's LLM-driven decisions cross into
the outside world, so it all goes through core.security first: SSRF
checks before any fetch, HTML sanitization before any content is read
back into a prompt.
"""

import requests
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from core.security import is_safe_url, sanitize_html

USER_AGENT = "MoneyBotResearcher/1.0"
MAX_BYTES = 1_500_000
TIMEOUT = 15
MAX_REDIRECTS = 5
REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)


def _unwrap_ddg_redirect(href: str):
    """DuckDuckGo's HTML endpoint doesn't return the target URL directly —
    it wraps it in a `//duckduckgo.com/l/?uddg=<encoded>` redirect link.
    Pull the real target back out, or every result silently fails to
    fetch."""
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return unquote(target) if target else None
    return href


def search_web(query: str, max_results: int = 5) -> list:
    """Free-tier web search via DuckDuckGo's HTML endpoint (no API key)."""
    url = "https://duckduckgo.com/html/"
    if not is_safe_url(url):
        return []
    try:
        resp = requests.post(
            url,
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for link in soup.select("a.result__a")[:max_results]:
        target = _unwrap_ddg_redirect(link.get("href"))
        title = link.get_text(strip=True)
        if target and title:
            results.append({"title": title, "url": target})
    return results


def fetch_url(url: str) -> str:
    """Fetch a URL and return sanitized, plain-text content. Raises
    ValueError for anything that fails the SSRF check rather than fetching
    it.

    Redirects are followed manually, re-checking is_safe_url on every hop
    — a plain `requests.get(url)` follows redirects automatically, so a
    URL that looks public at request time (and passes the initial check)
    could still 302 to a private/metadata address and have that followed
    without ever being validated.
    """
    if not is_safe_url(url):
        raise ValueError(f"Refusing to fetch unsafe URL: {url}")

    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        resp = requests.get(
            current_url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
        )
        if resp.status_code in REDIRECT_STATUS_CODES:
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError("Redirect response with no Location header")
            next_url = urljoin(current_url, location)
            if not is_safe_url(next_url):
                raise ValueError(f"Refusing to follow redirect to unsafe URL: {next_url}")
            current_url = next_url
            continue

        resp.raise_for_status()
        raw = resp.raw.read(MAX_BYTES + 1, decode_content=True)
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
        html = raw.decode(resp.encoding or "utf-8", errors="ignore")
        clean_html = sanitize_html(html)
        text = BeautifulSoup(clean_html, "html.parser").get_text(separator=" ", strip=True)
        return text[:8000]

    raise ValueError(f"Too many redirects fetching {url}")
