"""Default bot population. Adjust this list to change which bots exist —
new entries are seeded on next startup; existing bots (matched by name)
are left alone."""

from core import database as db

DEFAULT_BOTS = [
    {
        "name": "budget-finance-bot",
        "niche": "personal finance and budgeting",
        "description": "Publishes practical, no-nonsense budgeting and saving-money articles.",
    },
    {
        "name": "home-office-bot",
        "niche": "home office gear and productivity",
        "description": "Reviews and explains home office setups, tools, and productivity gear.",
    },
    {
        "name": "budget-travel-bot",
        "niche": "budget travel",
        "description": "Publishes tips for traveling well on a small budget.",
    },
    {
        "name": "side-hustle-bot",
        "niche": "side hustles and freelancing",
        "description": "Publishes practical guides on side income and freelance work.",
    },
]


def ensure_bots_seeded():
    existing_names = {bot["name"] for bot in db.get_bots()}
    for template in DEFAULT_BOTS:
        if template["name"] not in existing_names:
            db.add_bot(template["name"], template["niche"], template["description"])
