"""
Autopilot: reviews the queue so the fleet keeps evolving when you are away.

Rails, because an unattended process publishing to your live site under your
name needs them:
  - off by default
  - hard daily cap on automatic publishes
  - never publishes if the critic could not actually run
  - never publishes if GitHub is not configured
  - borderline work is escalated to you, not guessed at
  - every decision logged with score and reason
  - anything it published, you can take down in one tap
"""
from pathlib import Path

from core import db, site
from core.critic import review

DEFAULTS = {
    "autopilot_enabled": False,
    "autopilot_publish": True,
    "autopilot_daily_cap": 6,
}


def get_config():
    return {k: (db.get_setting(k) if db.get_setting(k) is not None else v)
            for k, v in DEFAULTS.items()}


def set_config(**kwargs):
    for k, v in kwargs.items():
        if k in DEFAULTS:
            db.set_setting(k, v)
    return get_config()


def _read_draft(path, page=""):
    p = Path(path)
    if p.is_dir():
        return site.read_page(p, page)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        return (lines[0].lstrip("# ").strip() if lines else p.stem,
                "\n".join(lines[1:]).strip())
    except Exception:
        return "", ""


def review_pending(limit=10):
    cfg = get_config()
    out = {"enabled": bool(cfg["autopilot_enabled"]), "reviewed": 0,
           "published": 0, "approved": 0, "rejected": 0, "escalated": 0,
           "notes": []}
    if not out["enabled"]:
        return out

    may_publish = bool(cfg["autopilot_publish"])
    if may_publish:
        try:
            from core.publisher import is_configured
            if not is_configured():
                may_publish = False
                out["notes"].append("GitHub not configured — rejecting only")
        except Exception:
            may_publish = False

    budget = int(cfg["autopilot_daily_cap"]) - db.auto_published_since(24)
    if may_publish and budget <= 0:
        may_publish = False
        out["notes"].append("daily publish cap reached")

    for item in db.get_queue("needs_review", limit=limit):
        title, body = _read_draft(item["path"], item.get("page", ""))
        if not body:
            db.set_queue_status(item["id"], "needs_review",
                                notes="Autopilot could not read this draft")
            out["escalated"] += 1
            continue

        verdict = review(title, body)
        out["reviewed"] += 1
        note = f"Autopilot {verdict['score']}/100 — {verdict['reason']}"
        if verdict["fix"]:
            note += f" | Fix: {verdict['fix']}"

        if verdict["verdict"] == "reject":
            # A rejection must change the site, not just a database row.
            site.remove_page(item["bot_id"], item.get("page", ""))
            db.set_queue_status(item["id"], "auto_rejected", notes=note,
                                score=verdict["score"])
            db.log(item["bot_id"], "auto_rejected", note)
            out["rejected"] += 1

        elif verdict["verdict"] == "publish" and may_publish and budget > 0:
            try:
                from core.publisher import publish_site_folder
                url = publish_site_folder(item["path"])
                db.set_queue_status(item["id"], "auto_published", live_url=url,
                                    notes=note, score=verdict["score"])
                db.update_bot(item["bot_id"], live_url=url)
                db.log(item["bot_id"], "auto_published", f"{url} | {note}")
                out["published"] += 1
                budget -= 1
            except Exception as e:
                db.set_queue_status(item["id"], "needs_review",
                                    notes=f"{note} | publish failed: {e}",
                                    score=verdict["score"])
                out["escalated"] += 1
                out["notes"].append(f"publish failed: {e}")
        elif verdict["verdict"] == "publish":
            # The critic approved but we cannot deliver (no GitHub, cap hit, or
            # a failed upload). Record the approval anyway: it is the quality
            # signal selection runs on. Without this, a user who has not set up
            # publishing gets one bot that can never become "proven", so
            # nothing ever breeds and evolution freezes permanently.
            db.set_queue_status(item["id"], "auto_approved", notes=note,
                                score=verdict["score"])
            db.log(item["bot_id"], "auto_approved", note)
            out["approved"] = out.get("approved", 0) + 1
        else:
            db.set_queue_status(item["id"], "needs_review", notes=note,
                                score=verdict["score"])
            out["escalated"] += 1

    return out


def take_down(item_id):
    """
    Undo an automatic publish.

    Recorded as a full-weight human rejection, so correcting the critic moves
    a bot's score far more than agreeing with it ever does.
    """
    item = db.get_queue_item(item_id)
    if not item:
        raise ValueError("Item not found")

    page = item.get("page") or ""
    site_slug = Path(item["path"]).name
    removed = "left online"

    # Drop it locally first so a later publish cannot resurrect it.
    site.remove_page(item["bot_id"], page)

    try:
        from core.publisher import unpublish_page, publish_site_folder
        unpublish_page(site_slug, page)
        # Republish so the index and sitemap no longer link to it.
        if site.page_count(item["bot_id"]) > 0:
            publish_site_folder(item["path"])
        removed = "removed from your site"
    except Exception as e:
        removed = f"removed locally, but the live copy could not be deleted ({e})"

    db.set_queue_status(item_id, "rejected", notes=f"Taken down by owner — {removed}")
    db.log(item["bot_id"], "owner_takedown", removed)
    return removed
