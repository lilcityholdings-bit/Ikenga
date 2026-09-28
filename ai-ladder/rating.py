"""Skill ratings: one continuous scale from kindergarten to a bachelor's degree.

Every student has a rating per skill; every challenge has a difficulty on the
same scale. After each attempt the rating moves Elo-style: beat a hard
challenge and it jumps, miss an easy one and it drops. Because the scale is
shared across grade bands, a rating reads directly as "working at about this
level", which is what teachers and parents actually want to know.
"""

# Starting rating for each level (grade band). Each band spans ~300 points.
LEVEL_BASE = [500, 800, 1100, 1400, 1700, 2000]
# Tier 1/2/3 challenges sit 100 below / at / 100 above their level's base.
TIER_OFFSET = {1: -100, 2: 0, 3: 100}
# Rating needed to be "working at" each level.
LEVEL_FLOOR = [0, 650, 950, 1250, 1550, 1850]


def difficulty(item):
    return LEVEL_BASE[item["level"]] + TIER_OFFSET[item["tier"]]


def expected(rating, diff):
    """Probability the student succeeds, per the Elo model."""
    return 1.0 / (1.0 + 10 ** ((diff - rating) / 400.0))


def k_factor(n_attempts):
    # Move fast while we're still learning where a student is, then settle.
    # Counts are per skill, so the fast "placement" phase lasts a while.
    if n_attempts < 5:
        return 80
    if n_attempts < 15:
        return 48
    if n_attempts < 40:
        return 32
    return 20


def update(rating, n_attempts, diff, score):
    """score is in [0, 1]; partial credit is allowed."""
    return rating + k_factor(n_attempts) * (score - expected(rating, diff))


def level_of(rating):
    lvl = 0
    for i, floor in enumerate(LEVEL_FLOOR):
        if rating >= floor:
            lvl = i
    return lvl


def progress_in_level(rating):
    """0.0-1.0 progress from this level's floor to the next one's."""
    lvl = level_of(rating)
    if lvl == len(LEVEL_FLOOR) - 1:
        top = LEVEL_FLOOR[-1] + 300
    else:
        top = LEVEL_FLOOR[lvl + 1]
    return max(0.0, min(1.0, (rating - LEVEL_FLOOR[lvl]) / (top - LEVEL_FLOOR[lvl])))


def target_difficulty(rating):
    # Aim a little below the student's rating (~64% expected success): hard
    # enough to learn from, easy enough that kids don't give up.
    return rating - 100
