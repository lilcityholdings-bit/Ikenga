"""Web research tools available to bots.

Everything here is a boundary the bot's LLM-driven decisions cross into
the outside world, so it all goes through core.security first: SSRF
checks before any fetch, HTML sanitization before any content is read
back into a prompt.
"""

import requests
from bs4 import BeautifulSoup

from core.security import is_safe_url, sanitize_html

USER_AGENT = "MoneyBotResearcher/1.0"
MAX_BYTES = 1_500_000
TIMEOUT = 15


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
        href = link.get("href")
        title = link.get_text(strip=True)
        if href and title:
            results.append({"title": title, "url": href})
    return results


def fetch_url(url: str) -> str:
    """Fetch a URL and return sanitized, plain-text content. Raises
    ValueError for anything that fails the SSRF check rather than fetching
    it."""
    if not is_safe_url(url):
        raise ValueError(f"Refusing to fetch unsafe URL: {url}")

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True)
    resp.raise_for_status()
    raw = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]
    html = raw.decode(resp.encoding or "utf-8", errors="ignore")
    clean_html = sanitize_html(html)
    text = BeautifulSoup(clean_html, "html.parser").get_text(separator=" ", strip=True)
    return text[:8000]
