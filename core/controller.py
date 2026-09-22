"""
Owner controls and the selection cycle.

Selection here acts on real signals — your verdicts, the critic's verdicts and
actual revenue — and offspring inherit a mutated copy of the parent's writing
genome. The old build ranked clones on how many files they had written, which
selected for nothing.
"""
import random
from datetime import datetime, timedelta

from config.settings import (
    COPIES_PER_PARENT, DEFAULT_OBJECTIVE, GUARDRAILS, MAX_BOTS,
    RANKING_CYCLE_DAYS, STARTING_BOTS,
)
from core import db, fitness, genome


SMALL_IMMIGRATION = 0.12


class Controller:

    # ------------------------------------------------ basic controls

    def start(self, bot_id):
        db.update_bot(bot_id, status="running")
        db.log(bot_id, "started", "by owner")

    def pause(self, bot_id):
        db.update_bot(bot_id, status="paused")
        db.log(bot_id, "paused", "by owner")

    def stop(self, bot_id):
        db.update_bot(bot_id, status="stopped")
        db.log(bot_id, "stopped", "by owner")

    def start_all(self):
        bots = db.get_bots()
        for b in bots:
            self.start(b["id"])
        return len(bots)

    def pause_all(self):
        bots = db.get_bots()
        for b in bots:
            self.pause(b["id"])
        return len(bots)

    def stop_all(self):
        bots = db.get_bots()
        for b in bots:
            self.stop(b["id"])
        return len(bots)

    def set_niche(self, bot_id, niche):
        niche = (niche or "").strip()
        db.update_bot(bot_id, niche=niche)
        bot = db.get_bot(bot_id)
        g = genome.load(bot.get("genome") or "")
        g["niche"] = niche
        db.update_bot(bot_id, genome=genome.dump(g))
        db.log(bot_id, "niche_set", niche)

    def set_objective(self, bot_id, objective):
        db.update_bot(bot_id, objective=objective)
        db.log(bot_id, "objective_set", objective[:200])

    def order(self, bot_id, text):
        """Orders are read by the bot at the top of its next prompt."""
        db.set_order(bot_id, text)
        db.log(bot_id, "order", text[:200])

    def order_all(self, text):
        bots = db.get_bots()
        for b in bots:
            self.order(b["id"], text)
        return len(bots)

    def guardrails(self):
        return db.get_setting("guardrails", GUARDRAILS)

    def add_guardrail(self, rule):
        rules = self.guardrails()
        if rule not in rules:
            rules.append(rule)
            db.set_setting("guardrails", rules)
        return rules

    # ------------------------------------------------ population

    def seed(self):
        if db.get_bots():
            return
        for i in range(STARTING_BOTS):
            g = genome.new_genome("")
            db.create_bot(
                name=f"Bot-{i + 1}",
                objective=DEFAULT_OBJECTIVE,
                genome=genome.dump(g),
            )

    def ranking_due(self):
        last = db.get_setting("last_ranking")
        if not last:
            db.set_setting("last_ranking", datetime.utcnow().isoformat())
            return False
        try:
            last_dt = datetime.fromisoformat(str(last))
        except Exception:
            return True
        return datetime.utcnow() >= last_dt + timedelta(days=RANKING_CYCLE_DAYS)

    def next_deadline(self):
        last = db.get_setting("last_ranking")
        if not last:
            return "after the first full cycle"
        try:
            return (datetime.fromisoformat(str(last))
                    + timedelta(days=RANKING_CYCLE_DAYS)).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return "unknown"

    def maybe_rank(self):
        if not self.ranking_due():
            return None
        return self.run_selection()

    def scoreboard(self):
        """Every living bot with its current score. Used by the dashboard too."""
        outcomes = db.all_outcomes()
        recent = db.all_outcomes(since_days=RANKING_CYCLE_DAYS)
        money = db.earnings_by_bot()
        rows = []
        for b in db.get_bots():
            o = outcomes.get(b["id"], {"published": 0, "rejected": 0, "pending": 0,
                                       "auto_published": 0, "auto_rejected": 0})
            m = float(money.get(b["id"], 0.0))
            r = recent.get(b["id"], {"published": 0, "rejected": 0, "pending": 0,
                                     "auto_published": 0, "auto_rejected": 0})
            rows.append({"bot": b, "outcomes": o, "earnings": m, "recent": r,
                         "score": fitness.score(o, m, r)})
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows

    def run_selection(self):
        board = self.scoreboard()
        if not board:
            return {"message": "No bots alive"}

        db.set_setting("last_ranking", datetime.utcnow().isoformat())
        scores = [r["score"] for r in board]
        result = {
            "when": datetime.utcnow().isoformat(),
            "standings": [
                {"name": r["bot"]["name"], "score": round(r["score"], 1),
                 "published": r["outcomes"]["published"],
                 "auto_published": r["outcomes"]["auto_published"],
                 "rejected": r["outcomes"]["rejected"] + r["outcomes"]["auto_rejected"],
                 "earned": round(r["earnings"], 2)}
                for r in board
            ],
            "removed": [], "bred": [], "notes": [],
        }

        # ---- cull
        # Two different rules, because the situations differ.
        #
        # Below the cap there is room to grow, so only genuine
        # underperformers go. Killing someone every cycle regardless of how
        # well they are doing was the old build's mistake.
        #
        # At the cap there is no room, and without turnover the population
        # freezes forever: nothing is culled, so there is no headroom, so
        # nothing breeds, so no new genome is ever tried again. So at the cap
        # the weakest is replaced whenever there is a real spread between best
        # and worst. If everyone is genuinely equal, nobody moves.
        at_cap = len(board) >= MAX_BOTS
        # Elitism: never cull the current leader. Scores are noisy over a
        # single window, so without this the best genome in the population can
        # be thrown away on one bad week and never recovered.
        leader_id = board[0]["bot"]["id"]
        cullable = [r for r in board
                    if not fitness.in_grace(r["bot"])
                    and r["bot"]["id"] != leader_id
                    and fitness.enough_evidence(r.get("recent"))]
        if len(board) > 1 and cullable:
            worst = cullable[-1]
            best = board[0]["score"]
            spread = best - worst["score"]
            forced = at_cap and spread > 1.0 and worst["score"] < best * 0.9
            if fitness.should_cull(worst["score"], scores) or forced:
                db.kill_bot(worst["bot"]["id"])
                db.log(worst["bot"]["id"], "culled", f"score {worst['score']:.1f}")
                result["removed"].append(
                    f"{worst['bot']['name']} (score {worst['score']:.1f})")
            elif at_cap:
                result["notes"].append(
                    "At the cap, but no clear underperformer with enough "
                    "reviewed work to judge fairly — no replacement made.")
            else:
                result["notes"].append("Nobody underperformed enough to cull.")
        elif len(board) > 1:
            result["notes"].append("All bots still within their 48h grace period.")

        # ---- breed: only from parents whose work was actually accepted
        alive = len(db.get_bots())
        headroom = max(0, MAX_BOTS - alive)
        if headroom <= 0:
            result["notes"].append(f"At the {MAX_BOTS}-bot cap.")
            return result

        parents = [r for r in board
                   if r["bot"].get("status") != "dead"
                   and fitness.proven(r["outcomes"], r["earnings"])]
        if not parents:
            result["notes"].append(
                "No bot has had work accepted yet, so nothing was copied. "
                "Publish something, or turn on autopilot.")
            return result

        for r in parents:
            if headroom <= 0:
                break
            parent = db.get_bot(r["bot"]["id"])
            if not parent or parent["status"] == "dead":
                continue
            pg = genome.load(parent.get("genome") or "")
            for _ in range(COPIES_PER_PARENT):
                if headroom <= 0:
                    break
                # Diversity. Every bot descends from one ancestor, so copying
                # parents forever collapses the gene pool and the population
                # settles on the first decent combination it finds. One new
                # bot in four gets a fresh random genome instead, which keeps
                # unexplored trait combinations entering the pool.
                # Immigration keeps the gene pool from collapsing, but in a
                # small population too much of it drowns out what selection
                # has already found. Scale it to the population size.
                immigration = 0.25 if len(board) >= 4 else SMALL_IMMIGRATION
                if random.random() < immigration:
                    child = genome.new_genome(parent.get("niche", ""))
                    child["generation"] = parent["generation"] + 1
                    origin = "fresh genome"
                else:
                    child = genome.mutate(pg)
                    origin = genome.summary(child)
                name = (f"{parent['name'].split('-g')[0]}-g{child['generation']}"
                        f"-{random.randint(100, 999)}")
                db.create_bot(
                    name=name,
                    objective=parent["objective"],
                    niche=child.get("niche", ""),
                    genome=genome.dump(child),
                    generation=parent["generation"] + 1,
                    parent_id=parent["id"],
                )
                result["bred"].append(f"{name} — {origin}")
                headroom -= 1

        return result
