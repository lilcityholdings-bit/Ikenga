"""The shop: agents, verified traces, the repair market, settlement, warranty and canary rollout.

The loop, end to end
--------------------
1. An agent reports each run (input, output, error). Its self-reported *failures* are trusted
   (nobody lies to look worse); its self-reported *successes* are not. The shop checks every
   output against the agent's declared contract itself, and anyone other than the agent can
   grade a run with the single-use feedback token that comes back with it.
2. Every verified failure with enough to replay becomes a *case*: an input plus a grader. A
   sample of verified passes become *guards* (things a repair must not break).
3. When enough new failures pile up, a *bounty* opens. It can be posted by the owner, or by the
   agent itself under a spending mandate, with no human involved. The referee splits the cases
   at random into a *visible* half (published, for repairers to work from) and a *hidden* half,
   and publishes a hash commitment to the terms, every case and the split seed before anyone submits.
4. Repair bots submit candidate configs. Each submission pays a referee fee, which funds the model
   calls and prices out spam. It is scored on the visible cases (the score is returned to the bot)
   and on the hidden cases. The hidden score stays secret until close, so a bot can't tune itself
   to the hidden cases by resubmitting.
5. At the deadline the referee settles automatically. The winner fixed the most hidden failures
   without breaking a single guard or earlier fix. Its payout is proportional to fixes, against a
   target committed up front ("no cure, no pay", as in marine salvage). The seed and case hashes
   are revealed so anyone can check the commitment.
6. The fix itself is held in escrow too. The owner never sees a losing submission, and sees the
   winning one only once it's paid for. The repairer can't be stiffed, and the owner can't look at
   a fix, decline it, and copy it (Arrow's information paradox, which is at its worst for prompts:
   a prompt is pure copyable text).
7. Part of the award, the *warranty*, is held back while the fix runs as a canary on a slice of
   live traffic. Real outcomes either promote it (warranty paid to the repairer) or roll it back
   automatically. A rollback refunds the warranty to the owner only if the referee can reproduce
   the harm on the failing live inputs, since the owner controls that telemetry and could fake it.
   The repairer is ultimately paid for production results, not for passing a test set.
8. Every failure a winning fix repaired becomes a permanent *regression* case. Each repair
   raises the bar that future repairs must clear.
"""
import difflib
import json
import math
import queue
import re
import secrets
import threading

import contracts
import ledger
from db import ApiError, canonical, sha256

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
CONFIG_FIELDS = ("system_instruction", "examples", "params", "model", "tools")
# What an outside repairer may change unless the owner opts in to more. Letting a stranger swap
# the agent's tools or model would let a "fix" add an exfiltration tool or point the referee at
# the most expensive model; the instruction and examples are what repair is about.
DEFAULT_MUTABLE = ("system_instruction", "examples")
Z_INTERIM, Z_FINAL, MIN_INTERIM = 2.576, 1.645, 30

DEFAULT_POLICY = {
    "auto_bounty": None,   # bounty terms + "min_failures"; amount may be "auto" (priced from value_per_failure)
    "auto_promote": False,
    "canary": {"fraction": 0.1, "min_samples": 200, "duration_s": 7 * 86400},
    # Pricing: what one failed run costs the owner (refund, human escalation, lost customer), in
    # atomic units. With it, the shop can price a repair from the failures it would prevent.
    "value_per_failure": None,
    "payback_days": 30,    # horizon over which a repair should pay for itself
    "offer_share": 0.5,    # share of the expected savings offered to the repairer
}
QUOTE_WINDOW_S = 7 * 86400


def _num(body, key, default, lo, hi, cast=float):
    val = body.get(key, default)
    try:
        val = cast(val)
    except (TypeError, ValueError):
        raise ApiError(422, f"{key} must be a number")
    if isinstance(body.get(key), bool) or not (lo <= val <= hi):
        raise ApiError(422, f"{key} must be between {lo} and {hi}")
    return val


def normalize_config(c):
    if not isinstance(c, dict):
        raise ApiError(422, "config must be an object")
    si = c.get("system_instruction")
    if not isinstance(si, str) or not si.strip():
        raise ApiError(422, "config.system_instruction is required")
    out = {"system_instruction": si, "model": c.get("model"), "params": c.get("params") or {},
           "tools": c.get("tools") or [], "examples": c.get("examples") or []}
    if out["model"] is not None and not isinstance(out["model"], str):
        raise ApiError(422, "config.model must be a string")
    if not isinstance(out["params"], dict) or not isinstance(out["tools"], list):
        raise ApiError(422, "config.params must be an object and config.tools a list")
    if not isinstance(out["examples"], list) or any(
            not isinstance(e, dict) or "input" not in e or "output" not in e for e in out["examples"]):
        raise ApiError(422, "config.examples must be a list of {input, output}")
    if len(canonical(out)) > 200_000:
        raise ApiError(413, "config too large")
    return out


def normalize_policy(p, base=None):
    policy = json.loads(json.dumps(base or DEFAULT_POLICY))
    if p is None:
        return policy
    if not isinstance(p, dict):
        raise ApiError(422, "policy must be an object")
    if "auto_promote" in p:
        if not isinstance(p["auto_promote"], bool):
            raise ApiError(422, "policy.auto_promote must be a boolean")
        policy["auto_promote"] = p["auto_promote"]
    if "canary" in p:
        c = p["canary"] or {}
        policy["canary"] = {"fraction": _num(c, "fraction", 0.1, 0.01, 0.5),
                            "min_samples": _num(c, "min_samples", 200, MIN_INTERIM, 10**7, int),
                            "duration_s": _num(c, "duration_s", 7 * 86400, 60, 90 * 86400)}
    if "value_per_failure" in p:
        policy["value_per_failure"] = None if p["value_per_failure"] is None else _num(p, "value_per_failure", None, 1, 10**12, int)
    if "payback_days" in p:
        policy["payback_days"] = _num(p, "payback_days", 30, 1, 365)
    if "offer_share" in p:
        policy["offer_share"] = _num(p, "offer_share", 0.5, 0.01, 1.0)
    if "auto_bounty" in p:
        ab = p["auto_bounty"]
        policy["auto_bounty"] = None if ab is None else dict(bounty_terms(ab, allow_auto=True), min_failures=_num(
            ab, "min_failures", 5, 1, 10000, int))
    if policy["auto_bounty"] and policy["auto_bounty"]["amount"] == "auto" and not policy["value_per_failure"]:
        raise ApiError(422, "auto_bounty.amount 'auto' needs policy.value_per_failure")
    return policy


def bounty_terms(b, allow_auto=False):
    if not isinstance(b, dict):
        raise ApiError(422, "bounty terms must be an object")
    private = b.get("private_to") or []
    if not isinstance(private, list) or len(private) > 50 or not all(isinstance(x, str) for x in private):
        raise ApiError(422, "private_to must be a list of up to 50 account ids")
    auto = allow_auto and b.get("amount") == "auto"
    return {"amount": "auto" if auto else _num(b, "amount", None, 1, 10**15, int),
            "min_amount": _num(b, "min_amount", 1, 1, 10**15, int),
            "max_amount": _num(b, "max_amount", 10**15, 1, 10**15, int),
            "private_to": sorted(set(private)),
            "threshold": _num(b, "threshold", 0.5, 0.01, 1.0),
            "warranty_bps": _num(b, "warranty_bps", 3000, 0, 10000, int),
            "duration_s": _num(b, "duration_s", 86400, 60, 30 * 86400),
            "warranty_s": _num(b, "warranty_s", 7 * 86400, 60, 90 * 86400),
            "max_submissions": _num(b, "max_submissions", 3, 1, 100, int),
            "share_config": bool(b.get("share_config", True)),
            "mutable_fields": _mutable(b.get("mutable_fields"))}


def _mutable(fields):
    if fields is None:
        return list(DEFAULT_MUTABLE)
    if not isinstance(fields, list) or not set(fields) <= set(CONFIG_FIELDS) or "system_instruction" not in fields:
        raise ApiError(422, f"mutable_fields must be a list drawn from {', '.join(CONFIG_FIELDS)}, including system_instruction")
    return sorted(set(fields))


def config_changes(old, new):
    """What a fix changes, for the owner to read before (or after) it goes live: which fields,
    and a line diff of the instruction. A fix is text written by a stranger; a hidden
    instruction in it would pass any test set, so it has to be visible."""
    changed = [f for f in CONFIG_FIELDS if old.get(f) != new.get(f)]
    diff = list(difflib.unified_diff(old["system_instruction"].splitlines(), new["system_instruction"].splitlines(),
                                     "before", "after", lineterm="", n=1))
    return {"fields": changed, "instruction_diff": "\n".join(diff)[:20000]}


def z_score(n_c, f_c, n_b, f_b):
    """One-sided two-proportion z: positive means the candidate fails more often."""
    if n_c == 0 or n_b == 0:
        return 0.0
    p = (f_c + f_b) / (n_c + n_b)
    var = p * (1 - p) * (1 / n_c + 1 / n_b)
    return 0.0 if var == 0 else (f_c / n_c - f_b / n_b) / math.sqrt(var)


class Shop:
    def __init__(self, store, runner, settings):
        self.store, self.runner, self.s = store, runner, settings
        self.jobs = queue.Queue()
        self._blocked = {}  # agent_id -> last auto-bounty refusal reason, so it's logged once, not every tick
        threading.Thread(target=self._work, daemon=True, name="referee").start()
        with store.tx() as db:
            pending = ([("baseline", r[0]) for r in db.execute("SELECT id FROM bounties WHERE status='PREPARING'")] +
                       [("submission", r[0]) for r in db.execute("SELECT id FROM submissions WHERE status='EVALUATING'")] +
                       [("warranty", r[0]) for r in db.execute("SELECT id FROM bounties WHERE warranty_state='VERIFYING'")])
        for job in pending:
            self.jobs.put(job)

    def now(self):
        return self.store.now()

    # ---- accounts & keys ------------------------------------------------------------------

    def create_account(self, name):
        if not isinstance(name, str) or not (1 <= len(name) <= 100):
            raise ApiError(422, "name must be 1-100 characters")
        account_id = "acc_" + secrets.token_hex(8)
        with self.store.tx() as db:
            db.execute("INSERT INTO accounts (id, name, created_at) VALUES (?,?,?)", (account_id, name, self.now()))
            key_id, secret = self.mint_key(account_id, "owner", None, "initial owner key")
        return {"account_id": account_id, "key_id": key_id, "owner_key": secret}

    def mint_key(self, account_id, scope, agent_id, label):
        secret = "abs_" + secrets.token_urlsafe(32)
        key_id = "key_" + secrets.token_hex(6)
        with self.store.tx() as db:
            db.execute("INSERT INTO keys (id, hash, account_id, scope, agent_id, label, created_at) VALUES (?,?,?,?,?,?,?)",
                       (key_id, sha256(secret), account_id, scope, agent_id, label, self.now()))
        return key_id, secret

    def resolve_key(self, secret):
        with self.store.tx() as db:
            row = db.execute("SELECT k.id, k.account_id, k.scope, k.agent_id, a.frozen FROM keys k JOIN accounts a"
                             " ON a.id=k.account_id WHERE k.hash=? AND k.revoked=0", (sha256(secret),)).fetchone()
        return None if row is None else {"key_id": row["id"], "account_id": row["account_id"],
                                         "scope": row["scope"], "agent_id": row["agent_id"], "frozen": bool(row["frozen"])}

    def new_bot_key(self, principal, body):
        agent_id = body.get("agent_id")
        if agent_id is not None:
            self.agent(agent_id, principal)
        key_id, secret = self.mint_key(principal["account_id"], "bot", agent_id, str(body.get("label", ""))[:100])
        return {"key_id": key_id, "key": secret, "scope": "bot", "agent_id": agent_id}

    def revoke_key(self, principal, key_id):
        with self.store.tx() as db:
            cur = db.execute("UPDATE keys SET revoked=1 WHERE id=? AND account_id=?", (key_id, principal["account_id"]))
            if cur.rowcount == 0:
                raise ApiError(404, "Key not found")

    def me(self, principal):
        aid = principal["account_id"]
        with self.store.tx() as db:
            acct = db.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
            keys = db.execute("SELECT id, scope, agent_id, label, revoked, created_at FROM keys WHERE account_id=?", (aid,)).fetchall()
            mandates = db.execute("SELECT id, agent_id, purpose, max_per_tx, max_per_day, expires_at, revoked, terms_hash"
                                  " FROM mandates WHERE account_id=?", (aid,)).fetchall()
            agents = [r[0] for r in db.execute("SELECT agent_id FROM agents WHERE account_id=?", (aid,))]
            return {"account_id": aid, "name": acct["name"], "balance": ledger.balance(db, ledger.acct(aid)),
                    "key": {"id": principal["key_id"], "scope": principal["scope"], "agent_id": principal["agent_id"]},
                    "keys": [dict(k) for k in keys], "mandates": [dict(m) for m in mandates], "agents": agents}

    def revoke_mandate(self, principal, mandate_id):
        with self.store.tx() as db:
            if db.execute("UPDATE mandates SET revoked=1 WHERE id=? AND account_id=?",
                          (mandate_id, principal["account_id"])).rowcount == 0:
                raise ApiError(404, "Mandate not found")
        self.store.event("mandate_revoked", {"id": mandate_id}, account_id=principal["account_id"])

    # ---- agents & versions ----------------------------------------------------------------

    def agent(self, agent_id, principal=None, owner_only=False):
        """Loads an agent and checks the caller may act on it: its owner account, or a bot key of
        that account that isn't bound to a different agent."""
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None or (principal is not None and row["account_id"] != principal["account_id"]):
            raise ApiError(404, f"Agent {agent_id} not found")
        if principal is not None:
            if owner_only and principal["scope"] != "owner":
                raise ApiError(403, "This action needs an owner key")
            if principal["agent_id"] not in (None, agent_id):
                raise ApiError(403, "This key is bound to a different agent")
        return {"agent_id": row["agent_id"], "account_id": row["account_id"],
                "contract": json.loads(row["contract"]), "policy": json.loads(row["policy"])}

    def create_agent(self, principal, body):
        if principal["scope"] != "owner":
            raise ApiError(403, "This action needs an owner key")
        agent_id = body.get("agent_id")
        if not isinstance(agent_id, str) or not AGENT_ID_RE.match(agent_id):
            raise ApiError(422, "agent_id must match [A-Za-z0-9_.:-]{1,128}")
        config = normalize_config(body.get("config"))
        contract = contracts.normalize_contract(body.get("contract"))
        policy = normalize_policy(body.get("policy"))
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM agents WHERE agent_id=?", (agent_id,)).fetchone():
                raise ApiError(409, f"Agent {agent_id} already exists")
            db.execute("INSERT INTO agents (agent_id, account_id, contract, policy, created_at) VALUES (?,?,?,?,?)",
                       (agent_id, principal["account_id"], canonical(contract), canonical(policy), self.now()))
            version = self._insert_config(db, agent_id, config, "ACTIVE", "initial", None)
            key_id, secret = self.mint_key(principal["account_id"], "bot", agent_id, f"agent {agent_id}")
        self.store.event("agent_created", {"version": version}, agent_id=agent_id, account_id=principal["account_id"])
        return {"agent_id": agent_id, "version": version, "agent_key": secret, "agent_key_id": key_id}

    def update_agent(self, principal, agent_id, body):
        a = self.agent(agent_id, principal, owner_only=True)
        with self.store.tx() as db:
            if "contract" in body:
                db.execute("UPDATE agents SET contract=? WHERE agent_id=?",
                           (canonical(contracts.normalize_contract(body["contract"])), agent_id))
            if "policy" in body:
                db.execute("UPDATE agents SET policy=? WHERE agent_id=?",
                           (canonical(normalize_policy(body["policy"], a["policy"])), agent_id))
        self._blocked.pop(agent_id, None)
        return self.agent(agent_id)

    def _insert_config(self, db, agent_id, config, status, source, base):
        n = int(self.now())
        while db.execute("SELECT 1 FROM configs WHERE agent_id=? AND version=?", (agent_id, f"v{n}")).fetchone():
            n += 1
        version = f"v{n}"
        db.execute("INSERT INTO configs (agent_id, version, config, fingerprint, status, source, base_version,"
                   " created_at, activated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (agent_id, version, canonical(config), sha256(canonical(config)), status, source, base,
                    self.now(), self.now() if status == "ACTIVE" else None))
        return version

    def stage(self, principal, body):
        agent_id = body.get("agent_id")
        self.agent(agent_id, principal, owner_only=True)
        cfg = body.get("config") or {"system_instruction": body.get("system_instruction")}
        with self.store.tx() as db:
            return self._insert_config(db, agent_id, normalize_config(cfg), "STAGED", "manual", None)

    def active_row(self, db, agent_id):
        return db.execute("SELECT * FROM configs WHERE agent_id=? AND status='ACTIVE'", (agent_id,)).fetchone()

    def _cancel_rollout(self, db, agent_id, why):
        db.execute("UPDATE rollouts SET status='CANCELLED', decision=? WHERE agent_id=? AND status='RUNNING'",
                   (canonical({"rule": why}), agent_id))

    def approve(self, principal, agent_id, version):
        self.agent(agent_id, principal, owner_only=True)
        with self.store.tx() as db:
            self._promote(db, agent_id, version, "manual approve")
        self.store.event("approved", {"version": version}, agent_id=agent_id)

    def _promote(self, db, agent_id, version, why):
        row = db.execute("SELECT status FROM configs WHERE agent_id=? AND version=?", (agent_id, version)).fetchone()
        if row is None or row["status"] != "STAGED":
            state = "not found" if row is None else f"is {row['status']}, not STAGED"
            raise ApiError(404, f"Candidate {version} for {agent_id} {state}")
        self._cancel_rollout(db, agent_id, why)
        db.execute("UPDATE configs SET status='ARCHIVED' WHERE agent_id=? AND status='ACTIVE'", (agent_id,))
        db.execute("UPDATE configs SET status='ACTIVE', activated_at=? WHERE agent_id=? AND version=?",
                   (self.now(), agent_id, version))

    def rollback(self, principal, agent_id, version):
        self.agent(agent_id, principal, owner_only=True)
        with self.store.tx() as db:
            cur = self.active_row(db, agent_id)
            if cur is None:
                raise ApiError(404, f"No active configuration found for {agent_id}")
            if cur["version"] != version:
                raise ApiError(409, f"{version} is not the active version of {agent_id} "
                                    f"(active is {cur['version']}); refusing to roll back")
            prev = db.execute("SELECT version FROM configs WHERE agent_id=? AND status='ARCHIVED'"
                              " ORDER BY activated_at DESC, id DESC LIMIT 1", (agent_id,)).fetchone()
            if prev is None:
                raise ApiError(409, f"No archived stable version of {agent_id} to restore")
            self._cancel_rollout(db, agent_id, "manual rollback")
            db.execute("UPDATE configs SET status='ROLLED_BACK' WHERE agent_id=? AND version=?", (agent_id, version))
            db.execute("UPDATE configs SET status='ACTIVE', activated_at=? WHERE agent_id=? AND version=?",
                       (self.now(), agent_id, prev["version"]))
        self.store.event("rolled_back", {"from": version, "to": prev["version"]}, agent_id=agent_id)
        return prev["version"]

    def get_config(self, principal, agent_id, session):
        self.agent(agent_id, principal)
        with self.store.tx() as db:
            active = self.active_row(db, agent_id)
            if active is None:
                raise ApiError(404, f"No active configuration found for {agent_id}")
            row, arm = active, "baseline"
            ro = db.execute("SELECT * FROM rollouts WHERE agent_id=? AND status='RUNNING'", (agent_id,)).fetchone()
            if ro is not None and session:
                bucket = int(sha256(f"{ro['id']}:{session}")[:8], 16) / 2**32
                if bucket < ro["fraction"]:
                    row = db.execute("SELECT * FROM configs WHERE agent_id=? AND version=?",
                                     (agent_id, ro["candidate"])).fetchone()
                    arm = "candidate"
        cfg = json.loads(row["config"])
        return {"version": row["version"], "system_instruction": cfg["system_instruction"], "config": cfg,
                "fingerprint": row["fingerprint"],
                "rollout": None if ro is None else {"id": ro["id"], "arm": arm if session else "baseline"}}

    def versions(self, principal, agent_id):
        self.agent(agent_id, principal)
        with self.store.tx() as db:
            rows = db.execute(
                "SELECT c.version, c.status, c.source, c.base_version, c.fingerprint, c.created_at, c.activated_at,"
                "       c.config, COUNT(t.id) AS traces, SUM(t.verdict='fail') AS verified_failures"
                "  FROM configs c LEFT JOIN traces t ON t.agent_id=c.agent_id AND t.version=c.version"
                " WHERE c.agent_id=? GROUP BY c.id ORDER BY c.id", (agent_id,)).fetchall()
            rollouts = db.execute("SELECT * FROM rollouts WHERE agent_id=? ORDER BY id", (agent_id,)).fetchall()
            cases = db.execute("SELECT kind, COUNT(*) n, SUM(bounty_id IS NULL) unassigned FROM cases"
                               " WHERE agent_id=? GROUP BY kind", (agent_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r, config=json.loads(r["config"]))
            d["verified_failure_rate"] = (d["verified_failures"] or 0) / d["traces"] if d["traces"] else None
            out.append(d)
        return {"agent_id": agent_id, "versions": out,
                "rollouts": [dict(r, decision=json.loads(r["decision"]) if r["decision"] else None) for r in rollouts],
                "cases": {r["kind"]: {"total": r["n"], "unassigned": r["unassigned"]} for r in cases}}

    def events(self, principal, agent_id, after):
        self.agent(agent_id, principal)
        with self.store.tx() as db:
            rows = db.execute("SELECT id, kind, detail, created_at FROM events WHERE agent_id=? AND id>? ORDER BY id LIMIT 500",
                              (agent_id, after)).fetchall()
        return [dict(r, detail=json.loads(r["detail"])) for r in rows]

    def quote(self, principal, agent_id, threshold=None):
        """What this agent's failures cost, and what a repair is worth. It is the basis of
        value-based pricing: a bounty is a share of the savings it's expected to produce, so the
        owner never has to guess a number and the repairer knows what's at stake.

            failures/day   verified failures over the last 7 days (or since the first trace)
            savings        value_per_failure x failures/day x payback_days x threshold
            bounty         savings x offer_share   (then clamped to the policy's min/max)
        """
        a = self.agent(agent_id, principal)
        p = a["policy"]
        since = self.now() - QUOTE_WINDOW_S
        with self.store.tx() as db:
            r = db.execute("SELECT COUNT(*) n, COALESCE(SUM(verdict='fail'), 0) f, MIN(created_at) first FROM traces"
                           " WHERE agent_id=? AND created_at>=?", (agent_id, since)).fetchone()
        # Never divide by less than a day: a new agent with a few early failures would otherwise be
        # extrapolated to hundreds a day, and an "auto" bounty would overpay for it.
        span_days = max(1.0, min(7.0, (self.now() - r["first"]) / 86400)) if r["first"] else None
        fpd = r["f"] / span_days if span_days else 0.0
        out = {"agent_id": agent_id, "window_days": round(span_days, 3) if span_days else 0, "runs": r["n"],
               "verified_failures": r["f"], "failure_rate": r["f"] / r["n"] if r["n"] else None,
               "failures_per_day": round(fpd, 3), "value_per_failure": p["value_per_failure"],
               "payback_days": p["payback_days"], "offer_share": p["offer_share"]}
        if p["value_per_failure"]:
            t = threshold if threshold is not None else ((p["auto_bounty"] or {}).get("threshold") or 0.5)
            daily = p["value_per_failure"] * fpd
            savings = daily * p["payback_days"] * t
            out.update({"failure_cost_per_day": round(daily), "threshold": t,
                        "expected_savings": round(savings), "suggested_bounty": round(savings * p["offer_share"]),
                        "owner_net_if_full_award": round(savings * (1 - p["offer_share"])),
                        "formula": "value_per_failure x failures_per_day x payback_days x threshold x offer_share"})
        return out

    # ---- traces, verdicts, cases ------------------------------------------------------------

    def ingest(self, principal, body):
        a = self.agent(body["agent_id"], principal)
        version, session = body.get("version"), body.get("session")
        inp, out, err = body.get("input"), body.get("output"), body.get("error")
        for name, val in (("input", inp), ("output", out), ("error", err), ("session", session)):
            if val is not None and not isinstance(val, str):
                raise ApiError(422, f"{name} must be a string")
            if val is not None and len(val) > self.s["max_field_bytes"]:
                raise ApiError(413, f"{name} exceeds {self.s['max_field_bytes']} bytes")
        if not body["success"] or err:
            verdict, source = "fail", "self_fail"
        else:
            ok, _ = contracts.check_contract(a["contract"], out)
            verdict, source = {True: ("pass", "contract"), False: ("fail", "contract"), None: ("unverified", "none")}[ok]
        token = "fb_" + secrets.token_urlsafe(24)
        with self.store.tx() as db:
            if version is None:
                row = self.active_row(db, a["agent_id"])
                version = row["version"] if row else None
            elif not db.execute("SELECT 1 FROM configs WHERE agent_id=? AND version=?", (a["agent_id"], version)).fetchone():
                raise ApiError(404, f"Version {version} not found for {a['agent_id']}")
            trace_id = db.execute(
                "INSERT INTO traces (agent_id, version, session, latency_ms, success, input, output, error, verdict,"
                " verdict_source, feedback_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (a["agent_id"], version, session, body["latency_ms"], int(body["success"]), inp, out, err,
                 verdict, source, sha256(token), self.now())).lastrowid
            total = db.execute("SELECT COUNT(*) FROM traces WHERE agent_id=?", (a["agent_id"],)).fetchone()[0]
            if inp is not None and a["contract"]["type"] != "none":
                if verdict == "fail":
                    # Replayable: the contract says what a correct output must look like.
                    self._add_case(db, a["agent_id"], trace_id, "failure", inp, {"type": "contract"}, source)
                elif verdict == "pass" and trace_id % self.s["guard_every"] == 0:
                    self._add_case(db, a["agent_id"], trace_id, "guard", inp, {"type": "contract"}, source)
        return {"status": "INGESTED", "total_traces": total, "trace_id": trace_id, "version": version,
                "verdict": verdict, "verdict_source": source, "feedback_token": token}

    def _add_case(self, db, agent_id, trace_id, kind, inp, grader, source):
        db.execute("INSERT INTO cases (agent_id, trace_id, kind, input, grader, source, created_at) VALUES (?,?,?,?,?,?,?)"
                   " ON CONFLICT(trace_id) DO UPDATE SET kind=excluded.kind, grader=excluded.grader, source=excluded.source"
                   " WHERE cases.bounty_id IS NULL",
                   (agent_id, trace_id, kind, inp, canonical(grader), source, self.now()))

    def feedback(self, principal, body):
        token = body.get("feedback_token")
        if not isinstance(token, str):
            raise ApiError(422, "feedback_token is required")
        verdict = body.get("verdict")
        if verdict not in ("pass", "fail"):
            raise ApiError(422, "verdict must be pass or fail")
        with self.store.tx() as db:
            t = db.execute("SELECT * FROM traces WHERE feedback_hash=?", (sha256(token),)).fetchone()
            if t is None:
                raise ApiError(404, "Unknown or already used feedback token")
            owner = db.execute("SELECT account_id FROM agents WHERE agent_id=?", (t["agent_id"],)).fetchone()[0]
            if principal["agent_id"] == t["agent_id"]:
                raise ApiError(403, "An agent can't grade its own output")
            source = "owner" if principal["account_id"] == owner else "consumer"
            # Consumers can't attach regex graders: a pattern from an outsider would be run by the
            # referee, and a catastrophic one could stall it (see contracts.safe_regex).
            grader = (contracts.make_grader(body, contracts.GRADERS if source == "owner" else contracts.CONSUMER_GRADERS)
                      if ("expected" in body or "grader" in body) else None)
            db.execute("UPDATE traces SET verdict=?, verdict_source=?, feedback_hash=NULL WHERE id=?",
                       (verdict, source, t["id"]))
            case = None
            if t["input"] is not None:
                if verdict == "fail" and grader is not None:
                    case = "failure"
                    self._add_case(db, t["agent_id"], t["id"], "failure", t["input"], grader, source)
                elif verdict == "pass" and t["output"] is not None:
                    try:
                        json.loads(t["output"])
                        g = {"type": "json_equals", "expected": t["output"]}
                    except ValueError:
                        g = grader or {"type": "contract"}
                    case = "guard"
                    self._add_case(db, t["agent_id"], t["id"], "guard", t["input"], g, source)
        return {"status": "RECORDED", "trace_id": t["id"], "verdict": verdict, "source": source, "case": case}

    # ---- bounties -------------------------------------------------------------------------

    def open_bounty(self, principal, agent_id, body, auto=False):
        if self.runner is None:
            raise ApiError(503, "No referee runner configured (set ABS_RUNNER); bounties can't be scored")
        a = self.agent(agent_id, principal)
        terms = bounty_terms(body, allow_auto=True)
        pricing = None
        if terms["amount"] == "auto":
            q = self.quote(principal, agent_id, terms["threshold"])
            if "suggested_bounty" not in q:
                raise ApiError(422, "amount 'auto' needs policy.value_per_failure")
            terms["amount"] = max(terms["min_amount"], min(terms["max_amount"], q["suggested_bounty"]))
            pricing = {k: q[k] for k in ("failures_per_day", "value_per_failure", "payback_days", "offer_share",
                                         "expected_savings", "suggested_bounty")}
        s = self.s
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM bounties WHERE agent_id=? AND status IN ('PREPARING','OPEN')", (agent_id,)).fetchone():
                raise ApiError(409, f"{agent_id} already has an open bounty")
            base = self.active_row(db, agent_id)
            if base is None:
                raise ApiError(409, f"{agent_id} has no active configuration")
            failures = db.execute("SELECT * FROM cases WHERE agent_id=? AND kind='failure' AND bounty_id IS NULL"
                                  " ORDER BY id LIMIT ?", (agent_id, s["max_failures"])).fetchall()
            if not failures:
                raise ApiError(409, f"{agent_id} has no unassigned verified failures to repair")
            guards = db.execute("SELECT * FROM cases WHERE agent_id=? AND kind='guard' ORDER BY id DESC LIMIT ?",
                                (agent_id, s["max_guards"])).fetchall()
            regressions = db.execute("SELECT * FROM cases WHERE agent_id=? AND kind='regression' ORDER BY id DESC LIMIT ?",
                                     (agent_id, s["max_guards"])).fetchall()
            seed = secrets.token_hex(32)
            split = {}
            for group in (failures, guards, regressions):
                ranked = sorted(group, key=lambda c: sha256(f"{seed}:{c['id']}"))
                n_vis = min(len(ranked) - 1, math.ceil(len(ranked) * s["visible_fraction"])) if len(ranked) > 1 else 0
                for i, c in enumerate(ranked):
                    split[c["id"]] = "visible" if i < n_vis else "hidden"
            cases = [(c, role) for group, role in ((failures, "failure"), (guards, "guard"), (regressions, "regression"))
                     for c in group]
            runs = len(cases) * s["trials"]
            terms.update({"agent_id": agent_id, "baseline_version": base["version"], "contract": a["contract"],
                          "pricing": pricing, "max_cost_per_run": s["max_cost_per_run"],
                          "margin_bps": s["margin_bps"], "take_bps": s["take_bps"], "trials": s["trials"],
                          "max_regressions": 0, "visible_fraction": s["visible_fraction"],
                          "split_rule": "cases ranked by sha256(seed:case_id); first ceil(n*visible_fraction) of each role visible (at most n-1)"})
            listing = sorted([{"case_id": c["id"], "role": role, "split": split[c["id"]],
                               "content_hash": sha256(canonical({"input": c["input"], "grader": json.loads(c["grader"])}))}
                              for c, role in cases], key=lambda x: x["case_id"])
            commitment = sha256(canonical({"terms": terms, "cases": listing, "seed": seed}))
            baseline_fee = self.max_fee(runs)
            bounty_id = db.execute(
                "INSERT INTO bounties (agent_id, account_id, amount, baseline_fee, terms, commitment, seed,"
                " baseline_version, status, created_at) VALUES (?,?,?,?,?,?,?,?,'PREPARING',?)",
                (agent_id, a["account_id"], terms["amount"], baseline_fee, canonical(terms), commitment, seed,
                 base["version"], self.now())).lastrowid
            # One mandate check covers the whole cost of posting: the reward plus the most that
            # scoring the baseline can cost. The scoring is held, and captured at metered cost later.
            mandate_id = ledger.authorize(self.store, principal, terms["amount"] + baseline_fee, "bounty", agent_id)
            ledger.transfer(self.store, ledger.acct(a["account_id"]), f"escrow:bounty:{bounty_id}", terms["amount"],
                            "bounty_escrow", f"bounty:{bounty_id}", mandate_id)
            ledger.transfer(self.store, ledger.acct(a["account_id"]), f"hold:bounty:{bounty_id}:baseline", baseline_fee,
                            "hold", f"bounty:{bounty_id}:baseline", mandate_id)
            for c, role in cases:
                entry = next(x for x in listing if x["case_id"] == c["id"])
                db.execute("INSERT INTO bounty_cases (bounty_id, case_id, split, role, content_hash) VALUES (?,?,?,?,?)",
                           (bounty_id, c["id"], split[c["id"]], role, entry["content_hash"]))
            for c in failures:
                db.execute("UPDATE cases SET bounty_id=? WHERE id=?", (bounty_id, c["id"]))
        self.store.event("bounty_opened", {"bounty_id": bounty_id, "amount": terms["amount"], "auto": auto,
                                           "mandate_id": mandate_id, "commitment": commitment,
                                           "cases": len(cases)}, agent_id=agent_id, account_id=a["account_id"])
        self.jobs.put(("baseline", bounty_id))
        return {"bounty_id": bounty_id, "status": "PREPARING", "commitment": commitment, "terms": terms,
                "baseline_fee_held": baseline_fee, "mandate_id": mandate_id}

    def max_fee(self, runs):
        """The most `runs` model calls can be charged: the per-run cost cap plus margin."""
        return math.ceil(runs * self.s["max_cost_per_run"] * (10000 + self.s["margin_bps"]) / 10000)

    def fee_for(self, cost):
        return math.ceil(cost * (10000 + self.s["margin_bps"]) / 10000)

    def _bounty(self, db, bounty_id):
        b = db.execute("SELECT * FROM bounties WHERE id=?", (bounty_id,)).fetchone()
        if b is None:
            raise ApiError(404, "Bounty not found")
        return b

    def list_bounties(self, status):
        with self.store.tx() as db:
            rows = db.execute("SELECT id, agent_id, amount, status, closes_at, commitment, terms FROM bounties"
                              " WHERE status=? ORDER BY id DESC LIMIT 200", (status,)).fetchall()
        # Private bounties (their cases may hold sensitive data) never appear in the public list.
        return [dict(r, terms=t) for r in rows for t in [json.loads(r["terms"])] if not t.get("private_to")]

    @staticmethod
    def _may_see(principal, b, terms):
        allowed = terms.get("private_to") or []
        return not allowed or principal["account_id"] == b["account_id"] or principal["account_id"] in allowed

    def bounty_view(self, principal, bounty_id):
        with self.store.tx() as db:
            b = self._bounty(db, bounty_id)
            terms = json.loads(b["terms"])
            if not self._may_see(principal, b, terms):
                raise ApiError(404, "Bounty not found")
            rows = db.execute("SELECT bc.case_id, bc.split, bc.role, bc.content_hash, c.input, c.grader FROM bounty_cases bc"
                              " JOIN cases c ON c.id=bc.case_id WHERE bc.bounty_id=? ORDER BY bc.case_id", (bounty_id,)).fetchall()
            base_cfg = db.execute("SELECT config FROM configs WHERE agent_id=? AND version=?",
                                  (b["agent_id"], b["baseline_version"])).fetchone()[0]
            n_subs = db.execute("SELECT COUNT(*) FROM submissions WHERE bounty_id=?", (bounty_id,)).fetchone()[0]
        settled = b["status"] in ("SETTLED", "NO_WINNER")
        baseline = json.loads(b["baseline_results"]) if b["baseline_results"] else {}
        visible = [{"case_id": r["case_id"], "role": r["role"], "input": r["input"], "grader": json.loads(r["grader"]),
                    "content_hash": r["content_hash"], "baseline_pass": baseline.get(str(r["case_id"]))}
                   for r in rows if r["split"] == "visible"]
        hidden = [{"case_id": r["case_id"], "role": r["role"], "content_hash": r["content_hash"]}
                  for r in rows if r["split"] == "hidden"]
        out = {"bounty_id": b["id"], "agent_id": b["agent_id"], "status": b["status"], "amount": b["amount"],
               "closes_at": b["closes_at"], "terms": terms, "commitment": b["commitment"],
               "visible_cases": visible, "hidden_case_count": len(hidden), "submissions": n_subs,
               "baseline_config": json.loads(base_cfg) if terms["share_config"] else None,
               "warranty": {"state": b["warranty_state"], "until": b["warranty_until"]}}
        if settled:
            # The reveal: with the seed and the full case listing, anyone can recompute the commitment
            # and check that the split followed the published rule.
            out.update({"seed": b["seed"], "hidden_cases": hidden, "baseline_results": baseline,
                        "result": json.loads(b["result"]) if b["result"] else None})
        return out

    def submit(self, principal, bounty_id, body):
        raw = body.get("config")
        if not isinstance(raw, dict):
            raise ApiError(422, "config must be an object")
        with self.store.tx() as db:
            b = self._bounty(db, bounty_id)
            if b["status"] != "OPEN" or self.now() >= b["closes_at"]:
                raise ApiError(409, f"Bounty {bounty_id} is not open for submissions")
            terms = json.loads(b["terms"])
            if not self._may_see(principal, b, terms):
                raise ApiError(404, "Bounty not found")
            base = json.loads(db.execute("SELECT config FROM configs WHERE agent_id=? AND version=?",
                                         (b["agent_id"], b["baseline_version"])).fetchone()[0])
            mutable = terms.get("mutable_fields", DEFAULT_MUTABLE)
            # Leaving a locked field out means "keep it"; only an explicit different value is refused.
            config = normalize_config({f: raw[f] if (f in raw) else base.get(f) for f in CONFIG_FIELDS})
            locked = [f for f in CONFIG_FIELDS if f not in mutable and config.get(f) != base.get(f)]
            if locked:
                raise ApiError(422, f"This bounty doesn't allow changing {', '.join(locked)}; "
                                    f"copy them from the baseline config")
            # The owner may submit too ("self-repair"): the whole verified pipeline (hidden tests,
            # regressions, canary) is useful with no outside repairers at all, and if the owner
            # wins, the reward simply comes back with no take.
            n = db.execute("SELECT COUNT(*) FROM submissions WHERE bounty_id=? AND account_id=?",
                           (bounty_id, principal["account_id"])).fetchone()[0]
            if n >= terms["max_submissions"]:
                raise ApiError(429, f"Submission limit ({terms['max_submissions']}) reached for this bounty")
            runs = db.execute("SELECT COUNT(*) FROM bounty_cases WHERE bounty_id=?", (bounty_id,)).fetchone()[0] * terms["trials"]
            fee = self.max_fee(runs)
            sid = db.execute("INSERT INTO submissions (bounty_id, account_id, config, fingerprint, status, fee, created_at)"
                             " VALUES (?,?,?,?,'EVALUATING',?,?)", (bounty_id, principal["account_id"], canonical(config),
                                                                    sha256(canonical(config)), fee, self.now())).lastrowid
            mandate_id = ledger.hold(self.store, principal, fee, "eval_fee", f"submission:{sid}", b["agent_id"]
                                     if principal["account_id"] == b["account_id"] else None)
        self.jobs.put(("submission", sid))
        return {"submission_id": sid, "status": "EVALUATING", "fee_held": fee, "mandate_id": mandate_id}

    def submission_view(self, principal, sid):
        with self.store.tx() as db:
            s = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            if s is None or s["account_id"] != principal["account_id"]:
                raise ApiError(404, "Submission not found")
            b = self._bounty(db, s["bounty_id"])
        out = {"submission_id": sid, "bounty_id": s["bounty_id"], "status": s["status"], "error": s["error"],
               "fee": s["fee"], "metered_cost": s["cost"],
               "visible_result": json.loads(s["visible_result"]) if s["visible_result"] else None,
               "bounty_status": b["status"]}
        if b["status"] in ("SETTLED", "NO_WINNER"):
            result = json.loads(b["result"] or "{}")
            out["score"] = next((x for x in result.get("scores", []) if x["submission_id"] == sid), None)
            out["won"] = result.get("winner", {}).get("submission_id") == sid
        return out

    # ---- referee (runs outside the DB lock: model calls are slow) ---------------------------

    def _work(self):
        while True:
            kind, ident = self.jobs.get()
            try:
                {"baseline": self._run_baseline, "submission": self._run_submission,
                 "warranty": self._run_warranty}[kind](ident)
            except Exception as e:  # recorded, never kills the referee thread
                with self.store.tx() as db:
                    if kind == "submission":
                        s = db.execute("SELECT * FROM submissions WHERE id=?", (ident,)).fetchone()
                        if s is not None and s["status"] == "EVALUATING":
                            ledger.settle_hold(self.store, f"submission:{ident}", s["account_id"], 0, "platform:fees", "referee_fee")
                            db.execute("UPDATE submissions SET status='FAILED', error=?, fee=0 WHERE id=?", (repr(e)[:500], ident))
                    elif kind == "baseline":
                        self._fail_bounty(db, ident, repr(e)[:500])
                    else:
                        # Couldn't check the claim: an unverifiable claim doesn't move the money.
                        b = db.execute("SELECT * FROM bounties WHERE id=?", (ident,)).fetchone()
                        if b is not None and b["warranty_state"] == "VERIFYING":
                            self._pay_warranty(db, b, False, {"reason": f"claim could not be verified: {e!r}"[:300]})

    def _cases_for(self, bounty_id):
        with self.store.tx() as db:
            return db.execute("SELECT bc.case_id, bc.split, bc.role, c.input, c.grader FROM bounty_cases bc"
                              " JOIN cases c ON c.id=bc.case_id WHERE bc.bounty_id=? ORDER BY bc.case_id",
                              (bounty_id,)).fetchall()

    def _evaluate(self, config, cases, contract, trials):
        """{case_id: pass}. With several trials a case passes on a strict majority, which damps
        model nondeterminism instead of rewarding a lucky sample."""
        results, errors, last, cost = {}, 0, None, 0
        for c in cases:
            wins = 0
            for _ in range(trials):
                try:
                    out = self.runner(config, c["input"])
                    out, run_cost = out if isinstance(out, tuple) else (out, None)
                    cost += self.s["unmetered_cost_per_run"] if run_cost is None else min(run_cost, self.s["max_cost_per_run"])
                except Exception as e:
                    out, errors, last = None, errors + 1, e
                wins += contracts.grade(json.loads(c["grader"]), contract, out)
            results[str(c["case_id"])] = wins * 2 > trials
        if cases and errors == len(cases) * trials:
            # Every single run crashed: that's the referee's runner failing (bad credentials, model
            # down), not the config. Scoring it as "fixed nothing" would keep a fee for no work.
            raise RuntimeError(f"runner failed on every case: {last!r}")
        return results, cost

    def _run_baseline(self, bounty_id):
        with self.store.tx() as db:
            b = self._bounty(db, bounty_id)
            if b["status"] != "PREPARING":
                return
            cfg = json.loads(db.execute("SELECT config FROM configs WHERE agent_id=? AND version=?",
                                        (b["agent_id"], b["baseline_version"])).fetchone()[0])
        terms = json.loads(b["terms"])
        results, cost = self._evaluate(cfg, self._cases_for(bounty_id), terms["contract"], terms["trials"])
        with self.store.tx() as db:
            if db.execute("UPDATE bounties SET baseline_results=?, status='OPEN', closes_at=? WHERE id=? AND status='PREPARING'",
                          (canonical(results), self.now() + terms["duration_s"], bounty_id)).rowcount:
                fee = ledger.settle_hold(self.store, f"bounty:{bounty_id}:baseline", b["account_id"], self.fee_for(cost),
                                         "platform:fees", "referee_fee")
                db.execute("UPDATE bounties SET baseline_fee=? WHERE id=?", (fee, bounty_id))

    def _run_submission(self, sid):
        with self.store.tx() as db:
            s = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            if s is None or s["status"] != "EVALUATING":
                return
            b = self._bounty(db, s["bounty_id"])
        terms = json.loads(b["terms"])
        cases = self._cases_for(b["id"])
        results, cost = self._evaluate(json.loads(s["config"]), cases, terms["contract"], terms["trials"])
        split = {str(c["case_id"]): c["split"] for c in cases}
        visible = {k: v for k, v in results.items() if split[k] == "visible"}
        hidden = {k: v for k, v in results.items() if split[k] == "hidden"}
        baseline = json.loads(b["baseline_results"])
        vis_summary = {"passed": sum(visible.values()), "total": len(visible),
                       "cases": [{"case_id": int(k), "pass": v, "baseline_pass": baseline.get(k)} for k, v in visible.items()]}
        with self.store.tx() as db:
            if db.execute("SELECT status FROM submissions WHERE id=?", (sid,)).fetchone()[0] == "EVALUATING":
                fee = ledger.settle_hold(self.store, f"submission:{sid}", s["account_id"], self.fee_for(cost),
                                         "platform:fees", "referee_fee")
                db.execute("UPDATE submissions SET status='EVALUATED', visible_result=?, hidden_result=?, fee=?, cost=?"
                           " WHERE id=?", (canonical(vis_summary), canonical(hidden), fee, cost, sid))

    def _fail_bounty(self, db, bounty_id, why):
        b = db.execute("SELECT * FROM bounties WHERE id=?", (bounty_id,)).fetchone()
        if b is None or b["status"] not in ("PREPARING", "OPEN"):
            return
        ledger.transfer(self.store, f"escrow:bounty:{bounty_id}", ledger.acct(b["account_id"]), b["amount"],
                        "bounty_refund", f"bounty:{bounty_id}")
        if b["status"] == "PREPARING":
            ledger.settle_hold(self.store, f"bounty:{bounty_id}:baseline", b["account_id"], 0, "platform:fees", "referee_fee")
        db.execute("UPDATE bounties SET status='FAILED', result=?, settled_at=? WHERE id=?",
                   (canonical({"error": why}), self.now(), bounty_id))
        db.execute("UPDATE cases SET bounty_id=NULL WHERE bounty_id=?", (bounty_id,))

    # ---- settlement ---------------------------------------------------------------------------

    def _settle(self, db, b):
        terms = json.loads(b["terms"])
        baseline = json.loads(b["baseline_results"])
        roles = {str(r["case_id"]): (r["role"], r["split"]) for r in
                 db.execute("SELECT case_id, role, split FROM bounty_cases WHERE bounty_id=?", (b["id"],))}
        hidden_fail = [k for k, (role, sp) in roles.items() if sp == "hidden" and role == "failure" and not baseline[k]]
        protected = [k for k, (role, sp) in roles.items() if sp == "hidden" and role != "failure" and baseline[k]]
        required = max(1, math.ceil(terms["threshold"] * len(hidden_fail)))
        scores, best = [], None
        for s in db.execute("SELECT * FROM submissions WHERE bounty_id=? AND status='EVALUATED' ORDER BY id", (b["id"],)):
            hidden = json.loads(s["hidden_result"])
            fixed = [k for k in hidden_fail if hidden.get(k)]
            broke = [k for k in protected if not hidden.get(k)]
            eligible = bool(hidden_fail) and len(fixed) >= 1 and len(broke) <= terms["max_regressions"]
            score = {"submission_id": s["id"], "account_id": s["account_id"], "fixed": len(fixed),
                     "regressions": len(broke), "eligible": eligible}
            scores.append(score)
            if eligible and (best is None or len(fixed) > best[1]):
                best = (s, len(fixed), fixed)
        escrow, amount = f"escrow:bounty:{b['id']}", b["amount"]
        result = {"hidden_failures": len(hidden_fail), "required_fixes": required, "protected_cases": len(protected),
                  "scores": scores}
        # Unfixed hidden failures were never shown to anyone, so they can go back in the pool.
        # Visible ones were published and are spent.
        fixed_set = set(best[2]) if best else set()
        for k, (role, sp) in roles.items():
            if role == "failure" and sp == "hidden" and k not in fixed_set:
                db.execute("UPDATE cases SET bounty_id=NULL WHERE id=?", (int(k),))
        if best is None:
            ledger.transfer(self.store, escrow, ledger.acct(b["account_id"]), amount, "bounty_refund", f"bounty:{b['id']}")
            db.execute("UPDATE bounties SET status='NO_WINNER', result=?, settled_at=? WHERE id=?",
                       (canonical(result), self.now(), b["id"]))
            return None
        sub, n_fixed, fixed = best
        self_repair = sub["account_id"] == b["account_id"]
        award = amount * min(n_fixed, required) // required
        # A self-repair just returns the owner's own money: no take, and no warranty to hold.
        take = 0 if self_repair else award * terms["take_bps"] // 10000
        warranty = 0 if self_repair else (award - take) * terms["warranty_bps"] // 10000
        pay_now = award - take - warranty
        refund = amount - award
        ref = f"bounty:{b['id']}"
        ledger.transfer(self.store, escrow, ledger.acct(sub["account_id"]), pay_now, "bounty_award", ref)
        ledger.transfer(self.store, escrow, "platform:fees", take, "platform_take", ref)
        ledger.transfer(self.store, escrow, f"escrow:warranty:{b['id']}", warranty, "warranty_hold", ref)
        ledger.transfer(self.store, escrow, ledger.acct(b["account_id"]), refund, "bounty_refund", ref)
        new_cfg = json.loads(sub["config"])
        base_cfg = json.loads(db.execute("SELECT config FROM configs WHERE agent_id=? AND version=?",
                                         (b["agent_id"], b["baseline_version"])).fetchone()[0])
        version = self._insert_config(db, b["agent_id"], new_cfg, "STAGED", f"bounty:{b['id']}", b["baseline_version"])
        result["changes"] = config_changes(base_cfg, new_cfg)
        for k in fixed:
            db.execute("UPDATE cases SET kind='regression' WHERE id=?", (int(k),))
        result["winner"] = {"submission_id": sub["id"], "account_id": sub["account_id"], "fixed": n_fixed,
                            "version": version, "award": award, "take": take, "paid_now": pay_now,
                            "warranty": warranty, "refund": refund, "self_repair": self_repair}
        db.execute("UPDATE bounties SET status='SETTLED', result=?, settled_at=?, warranty_state=?, warranty_until=? WHERE id=?",
                   (canonical(result), self.now(), "HELD" if warranty > 0 else None,
                    self.now() + terms["warranty_s"], b["id"]))
        policy = json.loads(db.execute("SELECT policy FROM agents WHERE agent_id=?", (b["agent_id"],)).fetchone()[0])
        active = self.active_row(db, b["agent_id"])
        if policy["auto_promote"] and active is not None and active["version"] == b["baseline_version"]:
            c = policy["canary"]
            ends = min(self.now() + c["duration_s"], self.now() + terms["warranty_s"])
            self._cancel_rollout(db, b["agent_id"], "superseded by new repair")
            db.execute("INSERT INTO rollouts (agent_id, candidate, baseline, fraction, min_samples, bounty_id, status,"
                       " started_at, ends_at) VALUES (?,?,?,?,?,?,'RUNNING',?,?)",
                       (b["agent_id"], version, active["version"], c["fraction"], c["min_samples"], b["id"],
                        self.now(), ends))
        return dict(result["winner"], changes=result["changes"])

    def start_rollout(self, principal, agent_id, body):
        a = self.agent(agent_id, principal, owner_only=True)
        c = normalize_policy({"canary": body}, a["policy"])["canary"]
        version = body.get("version")
        with self.store.tx() as db:
            row = db.execute("SELECT status FROM configs WHERE agent_id=? AND version=?", (agent_id, version)).fetchone()
            if row is None or row["status"] != "STAGED":
                raise ApiError(404, f"Candidate {version} for {agent_id} not found or not STAGED")
            active = self.active_row(db, agent_id)
            if active is None:
                raise ApiError(409, "No active configuration to compare against")
            self._cancel_rollout(db, agent_id, "superseded")
            rid = db.execute("INSERT INTO rollouts (agent_id, candidate, baseline, fraction, min_samples, status, started_at,"
                             " ends_at) VALUES (?,?,?,?,?,'RUNNING',?,?)",
                             (agent_id, version, active["version"], c["fraction"], c["min_samples"], self.now(),
                              self.now() + c["duration_s"])).lastrowid
        return {"rollout_id": rid, "candidate": version, "baseline": active["version"], **c}

    def _decide_rollout(self, db, ro):
        def arm(version):
            r = db.execute("SELECT COUNT(*), COALESCE(SUM(verdict='fail'), 0) FROM traces WHERE agent_id=? AND version=?"
                           " AND created_at>=?", (ro["agent_id"], version, ro["started_at"])).fetchone()
            return r[0], r[1]
        n_c, f_c = arm(ro["candidate"])
        n_b, f_b = arm(ro["baseline"])
        z = z_score(n_c, f_c, n_b, f_b)
        stats = {"candidate": {"n": n_c, "failures": f_c}, "baseline": {"n": n_b, "failures": f_b}, "z": round(z, 3)}
        outcome = None
        if n_c >= MIN_INTERIM and n_b >= MIN_INTERIM and z > Z_INTERIM:
            outcome, rule = "ROLLED_BACK", f"interim: candidate fails more (z > {Z_INTERIM})"
        elif n_c >= ro["min_samples"] and n_b >= ro["min_samples"]:
            outcome, rule = (("ROLLED_BACK", f"final: candidate fails more (z > {Z_FINAL})") if z > Z_FINAL
                             else ("PROMOTED", f"final: no evidence candidate is worse (z <= {Z_FINAL})"))
        elif self.now() >= ro["ends_at"]:
            outcome, rule = (("PROMOTED", f"window ended: {n_c} candidate runs, no evidence of harm")
                             if n_c >= MIN_INTERIM and n_b >= MIN_INTERIM and z <= Z_FINAL
                             else ("REVERTED", "window ended without enough traffic to judge; candidate left STAGED"))
        if outcome is None:
            return
        stats["rule"] = rule
        db.execute("UPDATE rollouts SET status=?, decision=? WHERE id=?", (outcome, canonical(stats), ro["id"]))
        if outcome == "PROMOTED":
            active = self.active_row(db, ro["agent_id"])
            if active is not None and active["version"] == ro["baseline"]:
                self._promote(db, ro["agent_id"], ro["candidate"], "canary promoted")
        elif outcome == "ROLLED_BACK":
            db.execute("UPDATE configs SET status='ROLLED_BACK' WHERE agent_id=? AND version=? AND status='STAGED'",
                       (ro["agent_id"], ro["candidate"]))
        self.store.event("rollout_" + outcome.lower(), dict(stats, rollout_id=ro["id"], candidate=ro["candidate"]),
                         agent_id=ro["agent_id"])

    def _resolve_warranty(self, db, b):
        """Money follows evidence. The warranty goes to the repairer once the canary is promoted,
        or once the warranty period ends (an owner who never deploys the fix doesn't get it back).
        A canary rollback doesn't refund it by itself: live telemetry comes from the owner's own
        agent, so an owner could fake failures on the candidate arm. A rollback only opens a
        claim, which the referee checks by reproduction (see _run_warranty)."""
        ro = db.execute("SELECT status FROM rollouts WHERE bounty_id=? ORDER BY id DESC LIMIT 1", (b["id"],)).fetchone()
        if ro is not None and ro["status"] == "RUNNING":
            return
        if ro is not None and ro["status"] == "ROLLED_BACK":
            db.execute("UPDATE bounties SET warranty_state='VERIFYING' WHERE id=?", (b["id"],))
            self.jobs.put(("warranty", b["id"]))
            return
        if self.now() < b["warranty_until"] and (ro is None or ro["status"] != "PROMOTED"):
            return
        self._pay_warranty(db, b, harmed=False, detail={"reason": "promoted" if ro and ro["status"] == "PROMOTED"
                                                        else "warranty period ended without a verified claim"})

    def _pay_warranty(self, db, b, harmed, detail, fee=0):
        result = json.loads(b["result"])
        escrow = f"escrow:warranty:{b['id']}"
        amount = ledger.balance(db, escrow)
        ledger.transfer(self.store, escrow, "platform:fees", min(fee, amount), "referee_fee", f"bounty:{b['id']}:warranty")
        rest = amount - min(fee, amount)
        to = ledger.acct(b["account_id"]) if harmed else ledger.acct(result["winner"]["account_id"])
        ledger.transfer(self.store, escrow, to, rest, "warranty_refund" if harmed else "warranty_release", f"bounty:{b['id']}")
        state = "REFUNDED" if harmed else "RELEASED"
        db.execute("UPDATE bounties SET warranty_state=? WHERE id=?", (state, b["id"]))
        self.store.event("warranty_" + state.lower(), dict(detail, bounty_id=b["id"], amount=rest, referee_fee=min(fee, amount)),
                         agent_id=b["agent_id"])

    def _run_warranty(self, bounty_id):
        """Verify a warranty claim by reproduction: re-run the candidate arm's failing live inputs
        through both the fix and the version it replaced. Harm counts only where the fix fails and
        the old version passes on the same input. The refund needs at least one reproduction, and
        reproductions in at least a fifth of the sample. The checking runs are paid for out of
        the warranty, whoever ends up receiving it."""
        with self.store.tx() as db:
            b = self._bounty(db, bounty_id)
            if b["warranty_state"] != "VERIFYING":
                return
            ro = db.execute("SELECT * FROM rollouts WHERE bounty_id=? ORDER BY id DESC LIMIT 1", (bounty_id,)).fetchone()
            # Only graders the owner didn't write: the owner is the one claiming the warranty, and a
            # grader demanding the old version's exact wording would make any change "reproduce".
            traces = db.execute(
                "SELECT t.input, CASE WHEN c.source='consumer' THEN c.grader END AS grader FROM traces t"
                " LEFT JOIN cases c ON c.trace_id=t.id WHERE t.agent_id=? AND"
                " t.version=? AND t.verdict='fail' AND t.input IS NOT NULL AND t.created_at>=? ORDER BY t.id DESC LIMIT ?",
                (ro["agent_id"], ro["candidate"], ro["started_at"], self.s["warranty_sample"])).fetchall()
            cfg = {v: json.loads(db.execute("SELECT config FROM configs WHERE agent_id=? AND version=?",
                                            (ro["agent_id"], v)).fetchone()[0]) for v in (ro["candidate"], ro["baseline"])}
        terms = json.loads(b["terms"])
        cases = [{"case_id": i, "input": t["input"], "grader": t["grader"] or canonical({"type": "contract"})}
                 for i, t in enumerate(traces)]
        cand, cost_c = self._evaluate(cfg[ro["candidate"]], cases, terms["contract"], terms["trials"]) if cases else ({}, 0)
        base, cost_b = self._evaluate(cfg[ro["baseline"]], cases, terms["contract"], terms["trials"]) if cases else ({}, 0)
        reproduced = sum(1 for k in cand if not cand[k] and base[k])
        harmed = reproduced >= 1 and reproduced * 5 >= len(cases)
        fee = self.fee_for(cost_c + cost_b)
        with self.store.tx() as db:
            b = self._bounty(db, bounty_id)
            if b["warranty_state"] == "VERIFYING":
                self._pay_warranty(db, b, harmed, {"reason": "warranty claim", "checked": len(cases),
                                                   "reproduced": reproduced}, fee)

    def _auto_bounties(self):
        with self.store.tx() as db:
            agents = db.execute("SELECT a.agent_id, a.account_id, a.policy FROM agents a JOIN accounts x ON x.id=a.account_id"
                                " WHERE x.frozen=0").fetchall()
        for a in agents:
            policy = json.loads(a["policy"])
            ab = policy.get("auto_bounty")
            if not ab:
                continue
            with self.store.tx() as db:
                busy = (db.execute("SELECT 1 FROM bounties WHERE agent_id=? AND (status IN ('PREPARING','OPEN') OR"
                                   " warranty_state='HELD')", (a["agent_id"],)).fetchone()
                        or db.execute("SELECT 1 FROM rollouts WHERE agent_id=? AND status='RUNNING'", (a["agent_id"],)).fetchone()
                        # Cool down after a bounty nobody could win, instead of re-posting the same
                        # unsolved failures (and paying for another baseline run) every tick.
                        or db.execute("SELECT 1 FROM bounties WHERE agent_id=? AND status IN ('NO_WINNER','FAILED')"
                                      " AND settled_at>?", (a["agent_id"], self.now() - ab["duration_s"])).fetchone())
                n = db.execute("SELECT COUNT(*) FROM cases WHERE agent_id=? AND kind='failure' AND bounty_id IS NULL",
                               (a["agent_id"],)).fetchone()[0]
            if busy or n < ab["min_failures"]:
                continue
            # The agent pays for its own repair, as a bot: only a mandate can authorize this.
            principal = {"key_id": None, "account_id": a["account_id"], "scope": "bot", "agent_id": a["agent_id"]}
            try:
                self.open_bounty(principal, a["agent_id"], ab, auto=True)
                self._blocked.pop(a["agent_id"], None)
            except ApiError as e:
                if self._blocked.get(a["agent_id"]) != e.detail:
                    self._blocked[a["agent_id"]] = e.detail
                    self.store.event("auto_bounty_blocked", {"reason": e.detail, "failures_waiting": n},
                                     agent_id=a["agent_id"], account_id=a["account_id"])

    def tick(self):
        """Everything time-driven: settle closed bounties, decide canaries, resolve warranties,
        post automatic bounties. Runs on a timer in the server; tests call it directly."""
        with self.store.tx() as db:
            for b in db.execute("SELECT * FROM bounties WHERE status='OPEN' AND closes_at<=?", (self.now(),)).fetchall():
                if db.execute("SELECT 1 FROM submissions WHERE bounty_id=? AND status='EVALUATING'", (b["id"],)).fetchone():
                    continue  # never settle while a paid-for evaluation is still running
                winner = self._settle(db, b)
                self.store.event("bounty_settled" if winner else "bounty_no_winner",
                                 {"bounty_id": b["id"], "winner": winner}, agent_id=b["agent_id"])
            for ro in db.execute("SELECT * FROM rollouts WHERE status='RUNNING'").fetchall():
                self._decide_rollout(db, ro)
            for b in db.execute("SELECT * FROM bounties WHERE warranty_state='HELD'").fetchall():
                self._resolve_warranty(db, b)
        self._auto_bounties()

    def reputation(self, account_id):
        with self.store.tx() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ApiError(404, "Account not found")
            subs = db.execute("SELECT COUNT(*) FROM submissions WHERE account_id=?", (account_id,)).fetchone()[0]
            won = db.execute("SELECT result, warranty_state FROM bounties WHERE status='SETTLED'").fetchall()
            wins = [(json.loads(r["result"])["winner"], r["warranty_state"]) for r in won
                    if json.loads(r["result"])["winner"]["account_id"] == account_id]
            owned = db.execute("SELECT status, COUNT(*) n FROM bounties WHERE account_id=? GROUP BY status",
                               (account_id,)).fetchall()
        return {"account_id": account_id,
                "as_repairer": {"submissions": subs, "wins": len(wins),
                                "awarded": sum(w["award"] for w, _ in wins),
                                "warranties_released": sum(1 for _, s in wins if s == "RELEASED"),
                                "warranties_refunded": sum(1 for _, s in wins if s == "REFUNDED")},
                "as_owner": {r["status"].lower(): r["n"] for r in owned}}
