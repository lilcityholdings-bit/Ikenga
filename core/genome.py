"""
The missing variation operator.

An evolutionary system needs three things: variation, heredity, selection.
This build had selection only, so the ranking did nothing. A genome is a
small set of writing traits that (a) measurably change the output, (b) pass
to offspring, and (c) mutate slightly each generation.

Keep the trait lists short. Too many options and you never sample any single
combination often enough to tell whether it works.
"""
import json
import random

TRAITS = {
    "angle": [
        "hands-on testing notes",
        "buyer's decision checklist",
        "common mistakes and fixes",
        "head-to-head comparison",
        "beginner walkthrough",
        "budget-first breakdown",
    ],
    "audience": [
        "complete beginners",
        "people replacing something that broke",
        "people upgrading from entry level",
        "people buying for someone else as a gift",
    ],
    "shape": [
        "short sections with bold subheads",
        "numbered steps",
        "question-and-answer",
        "one long argued recommendation",
    ],
    "opening": [
        "lead with the single recommendation",
        "lead with the reader's problem",
        "lead with a surprising fact",
    ],
}

MUTATION_RATE = 0.34  # roughly one trait changes per generation


def new_genome(niche: str = "") -> dict:
    g = {k: random.choice(v) for k, v in TRAITS.items()}
    g["niche"] = (niche or "").strip()
    g["generation"] = 1
    return g


def mutate(parent: dict, rate: float = MUTATION_RATE) -> dict:
    """Child is the parent with about one trait changed."""
    child = dict(parent or {})
    for key, options in TRAITS.items():
        if random.random() < rate:
            alternatives = [o for o in options if o != child.get(key)]
            if alternatives:
                child[key] = random.choice(alternatives)
    child["generation"] = int(parent.get("generation", 1)) + 1 if parent else 1
    return child


def load(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        g = json.loads(raw)
        return g if isinstance(g, dict) else new_genome()
    except Exception:
        return new_genome()


def dump(genome: dict) -> str:
    return json.dumps(genome or {})


def describe(genome: dict) -> str:
    """The part of the prompt that actually differs between bots."""
    g = genome or {}
    lines = []
    if g.get("niche"):
        lines.append(f"Niche: {g['niche']}")
    if g.get("audience"):
        lines.append(f"Write for: {g['audience']}")
    if g.get("angle"):
        lines.append(f"Approach: {g['angle']}")
    if g.get("shape"):
        lines.append(f"Structure: {g['shape']}")
    if g.get("opening"):
        lines.append(f"Opening: {g['opening']}")
    return "\n".join(lines)


def summary(genome: dict) -> str:
    g = genome or {}
    bits = [g.get("angle", ""), g.get("audience", "")]
    return " / ".join(b for b in bits if b) or "unset"
