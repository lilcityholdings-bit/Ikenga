"""Decision engine and article generation.

Free LLM tiers sometimes return malformed JSON or thin output. Both
functions here used to paper over that with a fallback stub — which meant
a bad LLM response could end up as a real, single-sentence page on the
live site, which is exactly what search engines penalize. Now a bad
response raises ContentError instead: the caller treats it as a failed
cycle (counts toward backoff, tries again next cycle) rather than
publishing garbage.
"""

from bs4 import BeautifulSoup

from core import llm
from core.security import redact_secrets

ACTIONS = ["publish_article", "discover_affiliate", "retry_failed", "idle"]
MIN_ARTICLE_WORDS = 150

DECISION_SYSTEM_PROMPT = (
    "You are the decision engine for an autonomous content bot. Given the "
    "bot's niche, real search queries people actually use in this niche, "
    "and recent activity, choose exactly one next action from this list: "
    "publish_article, discover_affiliate, retry_failed, idle. When you "
    "choose publish_article or retry_failed, set topic to one of the real "
    "search queries provided (or a close, specific variation of one) — "
    "never a generic restatement of the niche itself; specific long-tail "
    "topics are how a brand-new site can realistically rank at all. "
    'Respond with strict JSON only: {"action": "...", "reason": "...", "topic": "..."}'
)

ARTICLE_SYSTEM_PROMPT = (
    "You are a careful, factual writer producing a short, genuinely useful "
    "article for a niche content site. Write for the specific topic given, "
    "not the general niche. If a relevant affiliate link is provided, you "
    "may mention it naturally at most once, inside a normal <a> tag, only "
    "where it genuinely fits — never force one in and never mention more "
    "than one. Also produce 3-5 FAQ entries: real questions someone "
    "searching this topic would ask, each with a direct, self-contained "
    "answer of 1-3 sentences. Respond with strict JSON only: "
    '{"title": "...", "body_html": "...", '
    '"faq": [{"question": "...", "answer": "..."}]}. '
    "body_html must be simple semantic HTML (p, h2, ul, li, strong, a) with "
    "no script or style tags, at least 300 words. FAQ questions and answers "
    "must be plain text, not HTML."
)
MAX_FAQ_ENTRIES = 5


class ContentError(Exception):
    pass


def decide_action(bot: dict, recent_activity: list, suggested_queries: list) -> dict:
    activity_summary = "\n".join(
        f"- {a['ts']}: {a['kind']} ({a['detail']})" for a in recent_activity
    ) or "No recent activity."
    queries_block = "\n".join(f"- {q}" for q in suggested_queries) or "(none available)"
    user_prompt = (
        f"Bot niche: {bot['niche']}\n"
        f"Bot description: {bot['description']}\n"
        f"Real search queries people use in this niche right now:\n{queries_block}\n\n"
        f"Recent activity:\n{activity_summary}\n\n"
        "Decide the next action."
    )
    decision = llm.complete_json(user_prompt, system=DECISION_SYSTEM_PROMPT, default=None)
    if not decision or decision.get("action") not in ACTIONS:
        # Malformed JSON from a free-tier model — fall back to a real
        # suggested query rather than the bare niche, and to idle rather
        # than publish if we don't even have one, so a bad response can't
        # cascade straight into a thin/generic article.
        fallback_topic = suggested_queries[0] if suggested_queries else None
        decision = {
            "action": "publish_article" if fallback_topic else "idle",
            "reason": "fallback: malformed decision JSON",
            "topic": fallback_topic,
        }
    return decision


def generate_article(
    bot: dict,
    topic: str,
    research_notes: list,
    active_links: list,
    secrets_to_redact: list,
) -> dict:
    notes_block = "\n\n".join(research_notes) if research_notes else "(no external research available)"
    links_block = (
        "\n".join(f"- {link['name']}: {link['affiliate_url']}" for link in active_links)
        or "(no affiliate links available for this niche yet — write the article without one)"
    )
    user_prompt = (
        f"Niche: {bot['niche']}\n"
        f"Topic: {topic}\n"
        f"Research notes:\n{notes_block}\n\n"
        f"Available affiliate links:\n{links_block}\n\n"
        "Write a short, useful, honest article (300-600 words) on this topic."
    )
    result = llm.complete_json(user_prompt, system=ARTICLE_SYSTEM_PROMPT, default=None)
    if not result or not result.get("title") or not result.get("body_html"):
        raise ContentError("LLM returned no usable article (malformed JSON)")

    word_count = len(BeautifulSoup(result["body_html"], "html.parser").get_text().split())
    if word_count < MIN_ARTICLE_WORDS:
        raise ContentError(f"Generated article too thin ({word_count} words < {MIN_ARTICLE_WORDS})")

    result["title"] = redact_secrets(result["title"], secrets_to_redact)
    result["body_html"] = redact_secrets(result["body_html"], secrets_to_redact)
    result["faq"] = _clean_faq(result.get("faq"), secrets_to_redact)
    return result


def _clean_faq(raw_faq, secrets_to_redact: list) -> list:
    """Keep only well-formed question/answer pairs. A missing or malformed
    FAQ just means the article publishes without one."""
    if not isinstance(raw_faq, list):
        return []
    entries = []
    for item in raw_faq:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question or not answer:
            continue
        entries.append({
            "question": redact_secrets(question, secrets_to_redact),
            "answer": redact_secrets(answer, secrets_to_redact),
        })
        if len(entries) >= MAX_FAQ_ENTRIES:
            break
    return entries
