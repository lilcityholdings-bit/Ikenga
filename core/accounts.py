"""Bridges affiliate discovery and code proposals into persistent storage.

Nothing here moves money or rewrites code: there's no payment integration,
so a "spending approval" gate is symbolic, and there's no engine that
applies a code proposal to the bot's own files — approving one only
records the approval.
"""

import json

from core import database as db
from core.affiliate_discovery import discover_programs


def run_discovery_for_bot(bot: dict) -> list:
    programs = discover_programs(bot["niche"])
    created_ids = []
    for p in programs:
        program_id = db.add_affiliate_program(
            bot_id=bot["id"],
            name=p["name"],
            signup_url=p["signup_url"],
            checklist=json.dumps(p["checklist"]),
        )
        created_ids.append(program_id)
    return created_ids


def approve_code_proposal(proposal_id: int):
    db.update_code_proposal_status(proposal_id, "approved")


def reject_code_proposal(proposal_id: int):
    db.update_code_proposal_status(proposal_id, "rejected")
