"""Orchestrates one worker tick: heartbeat, then a decide-and-act cycle for
every started bot, gated by Auto Mode.

Auto Mode gates the whole loop; each bot's own started/stopped status
gates whether that bot runs. Both are required for anything to publish.
"""

import json

from config import settings
from core import (
    accounts, content, database as db, images, indexnow, keywords, publisher, tools,
)
from core.security import redact_secrets


def get_health() -> dict:
    age = db.get_heartbeat_age_seconds()
    alive = age is not None and age < settings.HEARTBEAT_STALE_MINUTES * 60
    return {"alive": alive, "age_seconds": age}


def _configured_secrets() -> list:
    return [
        s
        for s in [
            settings.GROQ_API_KEY,
            settings.GOOGLE_API_KEY,
            settings.OPENROUTER_API_KEY,
            settings.GITHUB_TOKEN,
            settings.DASHBOARD_PASSWORD,
        ]
        if s
    ]


def _do_publish(bot: dict, decision: dict):
    topic = decision.get("topic") or bot["niche"]

    research_notes = []
    for result in tools.search_web(f"{topic} {bot['niche']}", max_results=3):
        try:
            research_notes.append(tools.fetch_url(result["url"]))
        except Exception:
            continue

    active_links = db.list_active_affiliate_links(bot["id"])
    article = content.generate_article(
        bot, topic, research_notes, active_links, _configured_secrets()
    )
    affiliate_urls = {link["affiliate_url"] for link in active_links}

    image = None
    if settings.ENABLE_ARTICLE_IMAGES:
        try:
            image = images.find_image(topic)
        except Exception:
            image = None

    result = publisher.publish_article(
        bot["name"],
        article["title"],
        article["body_html"],
        affiliate_urls,
        faq=article.get("faq"),
        image=image,
    )

    db.add_article(bot["id"], article["title"], result["slug"], result["path"], result["url"])
    db.record_publish(bot["id"])
    db.log_activity(
        bot["id"], "published", json.dumps({"title": article["title"], "url": result["url"]})
    )

    if result["url"]:
        try:
            indexnow.ensure_key_file_published()
            indexnow.submit_url(result["url"])
        except Exception:
            # The article is already published and logged at this point —
            # a hiccup pushing the IndexNow key file shouldn't count as a
            # failed cycle and trip the failure-backoff counter.
            pass


def run_cycle(bot: dict):
    recent_failures = db.count_recent_failures(bot["id"], settings.FAILURE_WINDOW_MINUTES)
    if recent_failures >= settings.FAILURE_THRESHOLD:
        db.log_activity(
            bot["id"],
            "execution_skipped",
            f"Backing off after {recent_failures} failures in "
            f"{settings.FAILURE_WINDOW_MINUTES}m",
        )
        return

    # Check the publish cap before spending an LLM call on a decision.
    # At the default 4-minute loop interval a bot ticks ~15x/hour against
    # a cap of 4 publishes/hour, so without this a capped bot would still
    # burn a decision call (and usually get "publish_article" back) on
    # every tick for the rest of the hour, for nothing.
    if db.count_recent_publishes(bot["id"], window_minutes=60) >= settings.MAX_PUBLISHES_PER_HOUR:
        db.log_activity(bot["id"], "execution_skipped", "Hourly publish cap reached")
        return

    recent_activity = db.get_recent_activity(bot_id=bot["id"], limit=10)
    suggested_queries = keywords.suggest_queries(bot["niche"])
    decision = content.decide_action(bot, recent_activity, suggested_queries)
    db.log_activity(bot["id"], "decision", json.dumps(decision))

    action = decision.get("action")
    if action == "idle":
        return
    if action == "discover_affiliate":
        accounts.run_discovery_for_bot(bot)
        return
    if action in ("publish_article", "retry_failed"):
        # retry_failed doesn't yet vary its approach from a fresh publish.
        _do_publish(bot, decision)


def run_worker_tick():
    db.record_heartbeat()

    if db.get_setting("auto_mode", "false") != "true":
        return

    for bot in db.get_bots(status="started"):
        try:
            run_cycle(bot)
        except Exception as exc:
            db.record_failure(bot["id"])
            # Exception text can carry request details (a failed HTTP call's
            # URL, a truncated response body); redact before it lands in the
            # activity log the dashboard displays.
            db.log_activity(
                bot["id"], "execution_failed", redact_secrets(str(exc), _configured_secrets())
            )
