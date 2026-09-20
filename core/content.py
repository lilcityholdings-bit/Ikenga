"""Decision engine and article generation.

Free LLM tiers sometimes return malformed JSON. Both functions here fall
back to a safe default (publish_article, or a bare-bones article) rather
than crashing the worker loop — this is a known rough edge of free tiers,
not a bug when you see it happen occasionally.
"""

from core import llm
from core.security import redact_secrets

ACTIONS = ["publish_article", "discover_affiliate", "retry_failed", "idle"]

DECISION_SYSTEM_PROMPT = (
    "You are the decision engine for an autonomous content bot. Given the "
    "bot's niche and its recent activity, choose exactly one next action "
    "from this list: publish_article, discover_affiliate, retry_failed, idle. "
    'Respond with strict JSON only: {"action": "...", "reason": "...", "topic": "..."}'
)

ARTICLE_SYSTEM_PROMPT = (
    "You are a careful, factual writer producing a short, genuinely useful "
    "article for a niche content site. Respond with strict JSON only: "
    '{"title": "...", "body_html": "..."}. body_html must be simple semantic '
    "HTML (p, h2, ul, li, strong, a) with no script or style tags."
)


def decide_action(bot: dict, recent_activity: list) -> dict:
    activity_summary = "\n".join(
        f"- {a['ts']}: {a['kind']} ({a['detail']})" for a in recent_activity
    ) or "No recent activity."
    user_prompt = (
        f"Bot niche: {bot['niche']}\n"
        f"Bot description: {bot['description']}\n"
        f"Recent activity:\n{activity_summary}\n\n"
        "Decide the next action."
    )
    decision = llm.complete_json(user_prompt, system=DECISION_SYSTEM_PROMPT, default=None)
    if not decision or decision.get("action") not in ACTIONS:
        decision = {
            "action": "publish_article",
            "reason": "fallback: malformed decision JSON",
            "topic": bot["niche"],
        }
    return decision


def generate_article(bot: dict, topic: str, research_notes: list, secrets_to_redact: list) -> dict:
    notes_block = "\n\n".join(research_notes) if research_notes else "(no external research available)"
    user_prompt = (
        f"Niche: {bot['niche']}\n"
        f"Topic: {topic}\n"
        f"Research notes:\n{notes_block}\n\n"
        "Write a short, useful, honest article (300-600 words) on this topic."
    )
    result = llm.complete_json(user_prompt, system=ARTICLE_SYSTEM_PROMPT, default=None)
    if not result or not result.get("title") or not result.get("body_html"):
        result = {
            "title": (topic or bot["niche"]).strip().title(),
            "body_html": f"<p>{topic}</p>",
        }
    result["title"] = redact_secrets(result["title"], secrets_to_redact)
    result["body_html"] = redact_secrets(result["body_html"], secrets_to_redact)
    return result
