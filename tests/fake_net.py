"""
Fake network.

Nothing here touches the internet. Every request the app makes is intercepted
and answered by a local implementation, so the real code paths in llm.py,
tools.py and publisher.py all execute — parsing, sha handling, cooldowns,
redirect unwrapping, the lot.

The fake writer and fake critic form a closed loop: the writer's output quality
depends on the genome it was given, and the critic scores what it sees. That is
what makes it possible to test whether selection actually selects.
"""
import json
import random
import re
from urllib.parse import quote_plus

import requests

# ------------------------------------------------------------------ repo

REPO = {}          # path -> {"content": bytes, "sha": str}
CALLS = {"llm": 0, "search": 0, "github_put": 0, "github_delete": 0, "github_get": 0}


def reset():
    REPO.clear()
    for k in CALLS:
        CALLS[k] = 0


# ------------------------------------------------- hidden "true quality"

# Some trait combinations genuinely produce better articles. The app cannot see
# this table; it only sees the critic's scores. If selection works, the
# population should drift toward the high-value traits on its own.
TRAIT_VALUE = {
    "angle": {
        "head-to-head comparison": 30,
        "hands-on testing notes": 26,
        "buyer's decision checklist": 18,
        "common mistakes and fixes": 14,
        "budget-first breakdown": 10,
        "beginner walkthrough": 6,
    },
    "audience": {
        "people replacing something that broke": 22,
        "people upgrading from entry level": 16,
        "people buying for someone else as a gift": 9,
        "complete beginners": 5,
    },
    "shape": {
        "numbered steps": 14,
        "short sections with bold subheads": 12,
        "one long argued recommendation": 8,
        "question-and-answer": 6,
    },
    "opening": {
        "lead with the single recommendation": 12,
        "lead with the reader's problem": 9,
        "lead with a surprising fact": 5,
    },
}


def true_quality(genome_dict):
    return sum(TRAIT_VALUE.get(k, {}).get(genome_dict.get(k, ""), 0)
               for k in TRAIT_VALUE)


def _traits_from_prompt(system):
    """The writer sees its genome in the system prompt. Parse it back out."""
    g = {}
    for key, label in (("angle", "Approach:"), ("audience", "Write for:"),
                       ("shape", "Structure:"), ("opening", "Opening:")):
        m = re.search(rf"{re.escape(label)}\s*(.+)", system or "")
        if m:
            g[key] = m.group(1).strip()
    return g


PRODUCTS = [
    ("Breville Bambino", 299, 3), ("Gaggia Classic Pro", 449, 40),
    ("Rancilio Silvia", 545, 55), ("De'Longhi Dedica", 199, 35),
    ("Flair Neo Flex", 139, 0), ("Wacaco Picopresso", 129, 0),
]


def _write_article(system):
    """Better traits -> more concrete facts -> a genuinely better article."""
    g = _traits_from_prompt(system)
    q = true_quality(g)
    n_facts = max(3, min(14, 3 + q // 6))

    random.shuffle(PRODUCTS)
    body = ["## The short answer"]
    a, b = PRODUCTS[0], PRODUCTS[1]
    body.append(
        f"Buy the {a[0]} at {a[1]} dollars if this is your first machine. "
        f"The {b[0]} at {b[1]} dollars is the one to keep for a decade."
    )
    body.append("## What the numbers say")
    for name, price, heat in PRODUCTS[:min(len(PRODUCTS), max(2, n_facts // 3))]:
        body.append(
            f"The {name} costs {price} dollars and reaches brew temperature in "
            f"about {heat} seconds, which matters more on a weekday than any "
            f"spec on the box."
        )
    body.append("## What actually breaks")
    body.append(
        "Portafilter gaskets harden around the 18 month mark. Descaling every "
        "3 months in hard water areas prevents most early failures, and costs "
        "under 12 dollars a year in citric acid."
    )
    # Vary the wording. Repeating one sentence pattern would trip the app's
    # own repetition check, which would mask the quality signal being tested.
    templates = [
        "Across {n} shots the {p} held temperature within {d} degrees, tested over {w} weeks.",
        "Replacement gaskets for the {p} run about {d}{n} dollars and ship in {w} days.",
        "Pulling a double on the {p} took {n} seconds once the group warmed for {w} minutes.",
        "Second-hand {p} units list near {n}0 dollars after {w} months of ownership.",
        "Cleaning the {p} shower screen every {w} weeks cut channelling in {n} of our pulls.",
        "Warranty on the {p} covers {w} months, with {n} day turnaround on the boiler.",
        "Grind retention on the {p} measured {d}.{n} grams, enough to stale a morning dose.",
    ]
    for i in range(n_facts):
        tpl = templates[i % len(templates)]
        body.append(tpl.format(p=PRODUCTS[i % len(PRODUCTS)][0],
                               n=4 + i, d=1 + i % 7, w=3 + i))
    body.append("## What to do")
    body.append(f"For a first machine buy the {a[0]}. For your last, the {b[0]}.")

    title = f"{a[0]} vs {b[0]}: {n_facts} things worth knowing before you buy"
    return title, "\n".join(body), q


def _critique(prompt):
    """Score what is actually in the draft. The critic never sees the genome."""
    draft = prompt.split("=== BEGIN DRAFT ===")[-1].split("=== END DRAFT ===")[0]
    if re.search(r"(?i)ignore (all|your|previous) .*instructions", draft):
        return "SCORE: 0\nVERDICT: contains injected instructions\nFIX: remove it"
    figures = len(re.findall(r"\b\d+\b", draft))
    heads = draft.count("##")
    named = len(re.findall(r"\b(Breville|Gaggia|Rancilio|De'Longhi|Flair|Wacaco)\b", draft))
    score = min(98, 22 + figures * 1.4 + heads * 2 + named * 1.6)
    return (f"SCORE: {int(score)}\nVERDICT: {'strong' if score >= 72 else 'thin'} draft\n"
            f"FIX: add more measured comparisons")


# ------------------------------------------------------------------ search

def _search_html(query):
    rows = []
    for i in range(6):
        url = f"https://example{i}.com/{quote_plus(query)[:30]}"
        rows.append(
            f'<a class="result__a" href="/l/?uddg={quote_plus(url)}">'
            f'Guide {i + 1}: what to know about {query[:40]}</a>'
        )
    return "<html><body>" + "".join(rows) + "</body></html>"


# ------------------------------------------------------------------ shims

class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})

    def json(self):
        return self._payload


def _sha(path, content):
    return f"sha-{abs(hash((path, bytes(content)))) % (10 ** 12)}"


def fake_post(url, **kw):
    body = kw.get("json") or {}
    if "api.github.com" in url:
        return FakeResponse(404, {})
    CALLS["llm"] += 1

    msgs = body.get("messages") or []
    system = next((m["content"] for m in msgs if m.get("role") == "system"), "")
    user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
    if not msgs and body.get("contents"):
        system = user = body["contents"][0]["parts"][0]["text"]

    if "strict editor" in system or "BEGIN DRAFT" in user:
        text = _critique(user)
    else:
        title, article, _ = _write_article(system)
        text = f"TITLE: {title}\nBODY: {article}\nWHY: covers a real buying decision"

    if "generativelanguage" in url:
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


def fake_get(url, **kw):
    if "duckduckgo" in url:
        CALLS["search"] += 1
        q = url.split("q=", 1)[1] if "q=" in url else "x"
        return FakeResponse(200, text=_search_html(q))

    if "api.github.com" in url:
        CALLS["github_get"] += 1
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            if path in REPO:
                return FakeResponse(200, {"sha": REPO[path]["sha"], "path": path,
                                          "type": "file"})
            children = [
                {"path": p, "sha": v["sha"], "type": "file"}
                for p, v in REPO.items() if p.startswith(path.rstrip("/") + "/")
            ]
            if children:
                return FakeResponse(200, children)
            return FakeResponse(404, {})
        return FakeResponse(200, {"name": "mysite", "private": False})

    return FakeResponse(200, text="<html></html>")


def fake_put(url, **kw):
    CALLS["github_put"] += 1
    path = url.split("/contents/", 1)[1]
    body = kw.get("json") or {}
    if path in REPO and not body.get("sha"):
        return FakeResponse(409, {"message": "sha required"})
    import base64
    content = base64.b64decode(body.get("content", ""))
    REPO[path] = {"content": content, "sha": _sha(path, content)}
    return FakeResponse(201, {"content": {"path": path}})


def fake_delete(url, **kw):
    CALLS["github_delete"] += 1
    path = url.split("/contents/", 1)[1]
    body = kw.get("json") or {}
    if path not in REPO:
        return FakeResponse(404, {})
    if body.get("sha") != REPO[path]["sha"]:
        return FakeResponse(409, {"message": "wrong sha"})
    REPO.pop(path)
    return FakeResponse(200, {})


def install():
    requests.get = fake_get
    requests.put = fake_put
    requests.delete = fake_delete
    requests.post = fake_post
    requests.Session.get = lambda self, url, **kw: fake_get(url, **kw)
    requests.Session.post = lambda self, url, **kw: fake_post(url, **kw)
    requests.Session.put = lambda self, url, **kw: fake_put(url, **kw)
    requests.Session.delete = lambda self, url, **kw: fake_delete(url, **kw)
