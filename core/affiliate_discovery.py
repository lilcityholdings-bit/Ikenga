"""Grounded affiliate program discovery.

Discovery is grounded in real search results rather than letting the LLM
invent network names and signup URLs from nothing — every candidate here
comes from an actual, safety-checked search hit.

Account signup stays a human click: this only produces a candidate list
with a checklist. CAPTCHAs, email verification, and identity/tax forms
mean the final submission has to be done by a person, and most networks'
terms forbid bot-created accounts anyway.
"""

from core.security import is_safe_url
from core.tools import search_web

SIGNUP_CHECKLIST = [
    "Read the program's terms — bot-created accounts are usually against them.",
    "Sign up manually with your own details.",
    "Complete any identity/tax forms yourself.",
    "Record the resulting affiliate ID once approved.",
]


def discover_programs(niche: str, max_results: int = 5) -> list:
    query = f"{niche} affiliate program signup"
    results = search_web(query, max_results=max_results * 3)

    programs = []
    seen = set()
    for r in results:
        url = r.get("url", "")
        if not url or url in seen or not is_safe_url(url):
            continue
        seen.add(url)
        programs.append({
            "name": r.get("title", url),
            "signup_url": url,
            "checklist": list(SIGNUP_CHECKLIST),
        })
        if len(programs) >= max_results:
            break
    return programs
