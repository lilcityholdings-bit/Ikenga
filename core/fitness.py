"""
Outcome-based fitness, now with two grades of evidence.

Your verdicts are ground truth. The critic's verdicts are a useful but weaker
proxy, worth 35%. That ratio is the whole point: the fleet keeps evolving
while you are away, and the moment you actually review something, your opinion
outweighs roughly three of the machine's.
"""
from datetime import datetime

MONEY_WEIGHT = 50.0
QUALITY_WEIGHT = 40.0
PUBLISHED_WEIGHT = 3.0
PUBLISHED_CAP = 10
PENDING_WEIGHT = 0.5
PENDING_CAP = 5

AUTO_WEIGHT = 0.35         # a critic verdict counts for a third of yours

GRACE_HOURS = 48
CULL_FLOOR = 0.0
CULL_FRACTION = 0.5


def _effective(outcomes: dict):
    pub = float(outcomes.get("published", 0))
    rej = float(outcomes.get("rejected", 0))
    # "approved" = the critic said publish but delivery was unavailable. Same
    # quality signal, so it carries the same weight.
    auto_pub = float(outcomes.get("auto_published", 0)
                     + outcomes.get("auto_approved", 0))
    auto_rej = float(outcomes.get("auto_rejected", 0))
    return (pub + auto_pub * AUTO_WEIGHT,
            rej + auto_rej * AUTO_WEIGHT)


def score(outcomes: dict, earnings: float = 0.0, recent: dict = None) -> float:
    """
    outcomes: lifetime record, used for the acceptance rate.
    recent:   the last ranking window, used for output volume.

    Splitting these matters. Acceptance rate is a rate, so it compares a young
    bot and an old one fairly. Volume is a total, so if it were measured over a
    lifetime the oldest bot would always win regardless of quality, and every
    child would be culled before it could prove a better genome.
    """
    pub_eff, rej_eff = _effective(outcomes)
    pend = int(outcomes.get("pending", 0))
    recent_pub, _ = _effective(recent) if recent is not None else (pub_eff, rej_eff)

    money_points = float(earnings or 0.0) * MONEY_WEIGHT

    # Laplace smoothing: one lucky accept does not crown a bot, and a bot with
    # no reviews at all lands on 0.5, which scores zero rather than negative.
    acceptance = (pub_eff + 1.0) / (pub_eff + rej_eff + 2.0)
    quality_points = (acceptance - 0.5) * QUALITY_WEIGHT

    production_points = min(recent_pub, PUBLISHED_CAP) * PUBLISHED_WEIGHT
    pending_points = min(pend, PENDING_CAP) * PENDING_WEIGHT

    return money_points + quality_points + production_points + pending_points


def age_hours(created_at: str) -> float:
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", ""))
        return (datetime.utcnow() - dt).total_seconds() / 3600.0
    except Exception:
        return 999.0


def in_grace(bot: dict) -> bool:
    return age_hours(bot.get("created_at", "")) < GRACE_HOURS


def median(values: list) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def should_cull(candidate_score: float, all_scores: list) -> bool:
    if candidate_score < CULL_FLOOR:
        return True
    med = median(all_scores)
    if med <= 0:
        return False
    return candidate_score < med * CULL_FRACTION


MIN_REVIEWED_TO_JUDGE = 3


def enough_evidence(recent: dict) -> bool:
    """
    Whether we know enough about a bot this window to cull it.

    Culling on one or two reviewed articles is culling on noise. In a small
    population that is actively destructive: good genomes get thrown away on
    an unlucky window and the population drifts downward instead of upward.
    """
    if not recent:
        return False
    reviewed = (int(recent.get("published", 0)) + int(recent.get("rejected", 0))
                + int(recent.get("auto_published", 0))
                + int(recent.get("auto_rejected", 0)))
    return reviewed >= MIN_REVIEWED_TO_JUDGE


def proven(outcomes: dict, earnings: float = 0.0) -> bool:
    """
    Who is allowed to breed.

    Your approval or real money qualifies immediately. Critic approval also
    qualifies, but needs three of them — a weaker signal should require more
    evidence before it shapes the next generation.
    """
    if float(earnings or 0) > 0:
        return True
    if int(outcomes.get("published", 0)) >= 1:
        return True
    return (int(outcomes.get("auto_published", 0))
            + int(outcomes.get("auto_approved", 0))) >= 3
