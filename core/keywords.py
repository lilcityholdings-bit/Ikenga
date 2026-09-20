"""Grounds topic selection in real search queries instead of letting the
LLM invent topics from nothing.

A brand-new site has no domain authority and can't compete for broad head
terms ("budget travel") against established sites. The standard playbook
for a new site is long-tail: specific, low-competition queries real
people type. DuckDuckGo's autocomplete endpoint is a free, keyless proxy
for "what people actually search" — not as good as paid keyword-volume
tools, but real signal instead of an LLM's guess.
"""

import requests

from core.security import is_safe_url

TIMEOUT = 10
AUTOCOMPLETE_URL = "https://duckduckgo.com/ac/"

# Modifiers that tend to surface long-tail, buyer-intent or informational
# queries rather than the single-word head term.
MODIFIERS = ["how to", "best", "cheap", "for beginners", "vs", "mistakes to avoid"]


def _autocomplete(seed: str) -> list:
    if not is_safe_url(AUTOCOMPLETE_URL):
        return []
    try:
        resp = requests.get(
            AUTOCOMPLETE_URL,
            params={"q": seed, "type": "list"},
            headers={"User-Agent": "MoneyBotResearcher/1.0"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []
    # Response shape is [query, [suggestion, suggestion, ...]].
    if isinstance(data, list) and len(data) == 2 and isinstance(data[1], list):
        return [s for s in data[1] if isinstance(s, str)]
    return []


def suggest_queries(niche: str, max_results: int = 10) -> list:
    seeds = [f"{mod} {niche}" for mod in MODIFIERS]
    seen = set()
    suggestions = []
    for seed in seeds:
        for phrase in _autocomplete(seed):
            key = phrase.lower()
            if key not in seen:
                seen.add(key)
                suggestions.append(phrase)
        if len(suggestions) >= max_results:
            break
    return suggestions[:max_results]
