"""The rules: who may be served, what gets counted, and how usage becomes a bill.

Three kinds of limit protect the people involved:

- The **service owner** picks the lowest Keptvow trust level they'll serve, and how much an agent
  with no track record ("unknown") may spend with them per month before it has to earn more.
- The **agent owner** sets a monthly spending cap per agent, across every service. Clear enforces
  it when usage is recorded, so no service can charge past it, even one that skips the check.
- **The network:** an agent owner with an overdue bill anywhere is refused everywhere until they
  pay. That is the reason to bill through Clear rather than alone.

Clear never holds or moves money. Bills become Stripe invoices on the service owner's own Stripe
account (stripe_api.py), and the agent owner pays the service owner directly.
"""
import json
import threading

from . import tokens
from .store import new_id, now_ms, period_of
from .stripe_api import StripeError
from .trust import LEVELS, at_least

STRIPE_MIN_CENTS = 50  # Stripe won't charge less than $0.50; smaller bills roll into next month
DEFAULT_POLICY = {"min_trust": "unknown", "unknown_monthly_cap": 5_000_000}  # $5 for agents with no record
DAY_MS = 86_400_000


class Denied(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def dollars(micros):
    s = f"{micros / 1_000_000:,.6f}".rstrip("0")
    whole, frac = s.split(".")
    return f"${whole}.{frac.ljust(2, '0')}"


def cents_str(c):
    return f"${c / 100:,.2f}"


def next_period(p):
    y, m = map(int, p.split("-"))
    return f"{y + (m == 12):04d}-{m % 12 + 1:02d}"


def prev_period(p):
    y, m = map(int, p.split("-"))
    return f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"


def split_cents(amounts_micro):
    """Round micro-dollar amounts to cents so the parts add up to the rounded total exactly."""
    total = (sum(amounts_micro) + 5_000) // 10_000
    floors = [a // 10_000 for a in amounts_micro]
    order = sorted(range(len(amounts_micro)), key=lambda i: -(amounts_micro[i] % 10_000))
    for i in order[: total - sum(floors)]:
        floors[i] += 1
    return floors


class Clear:
    def __init__(self, cfg, store, keptvow, stripe=None):
        self.cfg, self.store, self.keptvow, self.stripe = cfg, store, keptvow, stripe
        self._lock = threading.Lock()

    # -- passes ---------------------------------------------------------------------------------
    def issue_pass(self, agent_key, service_id):
        agent = self.store.agent_by_key(agent_key)
        if not agent or not agent["active"]:
            raise Denied("UNAUTHORIZED", "unknown or inactive agent key")
        if not self.store.service(service_id):
            raise Denied("UNKNOWN_SERVICE", f"no service {service_id!r}")
        tok, exp = tokens.issue(self.cfg.token_secret, agent["id"], service_id)
        return {"pass": tok, "expires_at": exp, "agent_id": agent["id"], "service_id": service_id}

    def level(self, agent):
        """Keptvow's verdict, but only for a confirmed link. A claimed id could be anyone's."""
        if agent and agent["keptvow_id"] and agent["keptvow_verified"]:
            return self.keptvow.level(agent["keptvow_id"])
        return "unknown"

    # -- the check ------------------------------------------------------------------------------
    def open_period(self, ms=None):
        p = period_of(ms or now_ms())
        while self.store.one("SELECT 1 AS x FROM closed_periods WHERE period=?", p):
            p = next_period(p)
        return p

    def _spent(self, agent_id, period, service_id=None):
        sql = "SELECT COALESCE(SUM(amount),0) AS s FROM usage WHERE agent_id=? AND period=?"
        args = [agent_id, period]
        if service_id:
            sql += " AND service_id=?"
            args.append(service_id)
        return self.store.one(sql, *args)["s"]

    def _evaluate(self, service, pass_, tool, units, level=None):
        try:
            agent_id = tokens.verify(self.cfg.token_secret, pass_, service["id"])
        except ValueError as e:
            raise Denied("BAD_PASS", str(e))
        agent = self.store.agent(agent_id)
        if not agent or not agent["active"]:
            raise Denied("UNKNOWN_AGENT", "this agent is not registered with Clear or was switched off")
        if tool not in service["prices"]:
            raise Denied("UNKNOWN_TOOL", f"{service['name']} has no price for {tool!r}")
        if not isinstance(units, int) or isinstance(units, bool) or not 1 <= units <= 1_000_000:
            raise Denied("BAD_UNITS", "units must be a whole number from 1 to 1,000,000")
        cost = service["prices"][tool] * units
        overdue = self.store.one("SELECT COUNT(*) AS n FROM bills WHERE owner_id=? AND status='overdue'", agent["owner_id"])["n"]
        if overdue:
            raise Denied("OWNER_OVERDUE", "this agent's owner has an overdue Clear bill; it can't be served anywhere until it's paid")
        policy = {**DEFAULT_POLICY, **service["policy"]}
        level = level or self.level(agent)
        if not at_least(level, policy["min_trust"]):
            raise Denied("TRUST_TOO_LOW", f"Keptvow rates this agent '{level}'; {service['name']} serves '{policy['min_trust']}' and above")
        period = self.open_period()
        here = self._spent(agent_id, period, service["id"])
        if level == "unknown" and policy.get("unknown_monthly_cap") is not None and here + cost > policy["unknown_monthly_cap"]:
            raise Denied("NEW_AGENT_LIMIT", f"agents with no Keptvow record may spend up to {dollars(policy['unknown_monthly_cap'])} a month here")
        if policy.get("agent_monthly_cap") is not None and here + cost > policy["agent_monthly_cap"]:
            raise Denied("SERVICE_LIMIT", f"{service['name']} lets each agent spend up to {dollars(policy['agent_monthly_cap'])} a month")
        everywhere = self._spent(agent_id, period)
        if agent["monthly_cap"] is not None and everywhere + cost > agent["monthly_cap"]:
            raise Denied("OWNER_LIMIT", f"the agent's owner capped its spending at {dollars(agent['monthly_cap'])} a month")
        left = None if agent["monthly_cap"] is None else agent["monthly_cap"] - everywhere - cost
        return agent, cost, period, level, left

    def check(self, service, pass_, tool, units=1, ref=None):
        """Should the service serve this call? Advisory: record() enforces the same rules."""
        if ref:
            seen = self.store.one("SELECT agent_id FROM usage WHERE service_id=? AND ref=?", service["id"], ref)
            try:
                same_agent = seen and seen["agent_id"] == tokens.verify(self.cfg.token_secret, pass_, service["id"])
            except ValueError:
                same_agent = False
            if same_agent:  # a retry of this agent's call that's already counted
                return {"allow": True, "already_counted": True}
            if seen:
                return {"allow": False, "code": "REF_TAKEN", "reason": "that ref belongs to another call; use a unique ref"}
        try:
            agent, cost, _, level, left = self._evaluate(service, pass_, tool, units)
        except Denied as d:
            return {"allow": False, "code": d.code, "reason": d.message}
        return {"allow": True, "agent_id": agent["id"], "trust_level": level, "cost": dollars(cost),
                "owner_budget_left": None if left is None else dollars(left)}

    def record(self, service, pass_, tool, ref, units=1):
        """Count one served call. The same ref twice is counted once, so retries are safe."""
        if not ref or not isinstance(ref, str) or len(ref) > 200:
            raise Denied("NO_REF", "each call needs a unique ref (up to 200 characters) so retries aren't double-counted")
        seen = self.store.one("SELECT * FROM usage WHERE service_id=? AND ref=?", service["id"], ref)
        if seen:
            try:
                mine = seen["agent_id"] == tokens.verify(self.cfg.token_secret, pass_, service["id"])
            except ValueError as e:
                raise Denied("BAD_PASS", str(e))
            if not mine:
                raise Denied("REF_TAKEN", "that ref belongs to another call; use a unique ref")
            return seen, False
        try:  # trust is a network call, so it is read before taking the lock
            level = self.level(self.store.agent(tokens.verify(self.cfg.token_secret, pass_, service["id"])))
        except ValueError:
            level = None
        with self._lock:
            seen = self.store.one("SELECT * FROM usage WHERE service_id=? AND ref=?", service["id"], ref)
            if seen:
                return seen, False
            agent, cost, period, _, _ = self._evaluate(service, pass_, tool, units, level)
            self.store.run("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)", service["id"], ref, agent["id"],
                           agent["owner_id"], tool, units, cost, period, now_ms())
        return self.store.one("SELECT * FROM usage WHERE service_id=? AND ref=?", service["id"], ref), True

    # -- bills ----------------------------------------------------------------------------------
    def close_period(self, period, force=False):
        """Turn a month's usage into one bill per (service, agent owner)."""
        if not force and period >= period_of(now_ms()):
            raise Denied("PERIOD_OPEN", f"{period} isn't over yet")
        with self._lock:
            if self.store.one("SELECT 1 AS x FROM closed_periods WHERE period=?", period):
                return []
            self.store.run("INSERT INTO closed_periods VALUES (?,?)", period, now_ms())
        rows = self.store.all(
            "SELECT u.service_id, u.owner_id, u.agent_id, a.name AS agent, u.tool, s.prices,"
            " SUM(u.units) AS units, SUM(u.amount) AS amount FROM usage u"
            " JOIN agents a ON a.id=u.agent_id JOIN services s ON s.id=u.service_id"
            " WHERE u.period=? GROUP BY u.service_id, u.owner_id, u.agent_id, u.tool"
            " ORDER BY a.name, u.tool", period)
        groups = {}
        for r in rows:
            groups.setdefault((r["service_id"], r["owner_id"]), []).append(r)
        for b in self.store.all("SELECT service_id, owner_id FROM bills WHERE status='carried_forward' AND period<?", period):
            groups.setdefault((b["service_id"], b["owner_id"]), [])
        made = []
        for (sid, oid), items in groups.items():
            cents = split_cents([i["amount"] for i in items]) if items else []
            lines = [{"agent_id": i["agent_id"], "agent": i["agent"], "tool": i["tool"], "units": i["units"],
                      "unit_price": dollars(json.loads(i["prices"]).get(i["tool"], 0)), "cents": c,
                      "description": f"{i['agent']} · {i['tool']} · {i['units']:,} × "
                                     f"{dollars(json.loads(i['prices']).get(i['tool'], 0))}"}
                     for i, c in zip(items, cents)]
            carried = self.store.all("SELECT id, period, total_cents FROM bills WHERE service_id=? AND owner_id=?"
                                     " AND status='carried_forward' AND period<?", sid, oid, period)
            carried_in = sum(c["total_cents"] for c in carried)
            if carried_in:
                lines.append({"agent_id": None, "agent": "", "tool": "", "units": 0, "unit_price": "", "cents": carried_in,
                              "description": "Carried from " + ", ".join(c["period"] for c in carried) + " (under Stripe's $0.50 minimum)"})
            usage_cents = sum(cents)
            total = usage_cents + carried_in
            status = "ready" if total >= STRIPE_MIN_CENTS else "carried_forward"
            bid = new_id("bill")
            self.store.run("INSERT INTO bills (id,service_id,owner_id,period,lines,usage_cents,carried_in_cents,total_cents,"
                           "status,note,created_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           bid, sid, oid, period, json.dumps(lines), usage_cents, carried_in, total, status,
                           None if status == "ready" else "under Stripe's $0.50 minimum; rolls into next month",
                           now_ms(), now_ms())
            for c in carried:
                self._set(c["id"], status="rolled_over", note=f"rolled into {bid}")
            made.append(bid)
        return made

    def _set(self, bid, **fields):
        fields["updated_ms"] = now_ms()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.store.run(f"UPDATE bills SET {cols} WHERE id=?", *fields.values(), bid)

    def bill(self, bid):
        b = self.store.one("SELECT * FROM bills WHERE id=?", bid)
        if b:
            b["lines"] = json.loads(b["lines"])
        return b

    def bills(self, service_id=None, owner_id=None):
        sql, args = "SELECT * FROM bills WHERE 1=1", []
        if service_id:
            sql, args = sql + " AND service_id=?", args + [service_id]
        if owner_id:
            sql, args = sql + " AND owner_id=?", args + [owner_id]
        out = self.store.all(sql + " ORDER BY period DESC, created_ms", *args)
        for b in out:
            b["lines"] = json.loads(b["lines"])
        return out

    def send_bills(self):
        """Create Stripe invoices for every ready bill. Returns {bill id: outcome}."""
        out = {}
        for b in self.bills():
            if b["status"] != "ready":
                continue
            svc, owner = self.store.service(b["service_id"]), self.store.owner(b["owner_id"])
            why = (not self.stripe and "Stripe isn't set up (no STRIPE_SECRET_KEY)") or \
                  (not svc["stripe_account"] and "the service owner hasn't connected a Stripe account") or \
                  (not owner["email"] and "the agent owner has no email to send the invoice to")
            if why:
                self._set(b["id"], note=why)
                out[b["id"]] = why
                continue
            try:
                cus = self.store.one("SELECT customer_id FROM stripe_customers WHERE stripe_account=? AND owner_id=?",
                                     svc["stripe_account"], owner["id"])
                if cus:
                    cus = cus["customer_id"]
                else:
                    cus = self.stripe.create_customer(svc["stripe_account"], owner["id"], owner["name"], owner["email"])
                    self.store.run("INSERT OR IGNORE INTO stripe_customers VALUES (?,?,?)", svc["stripe_account"], owner["id"], cus)
                inv, url = self.stripe.create_invoice(svc["stripe_account"], cus, b, self.cfg.days_until_due)
            except StripeError as e:
                self._set(b["id"], note=str(e))
                out[b["id"]] = str(e)
                continue
            self._set(b["id"], status="sent", stripe_invoice_id=inv, invoice_url=url, note=None,
                      due_ms=now_ms() + self.cfg.days_until_due * DAY_MS)
            out[b["id"]] = "sent"
        return out

    def mark_paid(self, bid, note="marked paid by the operator"):
        b = self.bill(bid)
        if not b or b["status"] not in ("ready", "sent", "overdue"):
            raise Denied("BAD_STATE", "only a ready, sent or overdue bill can be marked paid")
        self._set(bid, status="paid", note=note)
        self._report(self.bill(bid), "cleared_cleanly")

    def sync(self, now=None):
        """Ask Stripe what's been paid, flag what's late, and tell Keptvow how each agent paid."""
        now = now or now_ms()
        changed = {}
        for b in self.bills():
            if b["status"] not in ("sent", "overdue"):
                continue
            if self.stripe and b["stripe_invoice_id"]:
                svc = self.store.service(b["service_id"])
                try:
                    st = self.stripe.invoice_status(svc["stripe_account"], b["stripe_invoice_id"])
                except StripeError as e:
                    self._set(b["id"], note=str(e))
                    continue
                if st == "paid":
                    self._set(b["id"], status="paid", note=None)
                    self._report(self.bill(b["id"]), "cleared_cleanly")
                    changed[b["id"]] = "paid"
                    continue
                if st in ("void", "uncollectible"):
                    self._set(b["id"], status="void", note=f"Stripe marked it {st}")
                    changed[b["id"]] = "void"
                    continue
            if b["status"] == "sent" and b["due_ms"] and now > b["due_ms"]:
                self._set(b["id"], status="overdue", note="past due; the owner's agents are paused everywhere until it's paid")
                changed[b["id"]] = "overdue"
            elif b["status"] == "overdue" and now > b["due_ms"] + self.cfg.grace_days * DAY_MS:
                self._report(b, "ghosted")
        return changed

    def _report(self, b, event):
        if (b.get("reported_to_keptvow") or "") == event:
            return
        agents = [self.store.agent(a) for a in sorted({l["agent_id"] for l in b["lines"] if l["agent_id"]})]
        linked = [a["keptvow_id"] for a in agents if a and a["keptvow_id"] and a["keptvow_verified"]]
        if any([self.keptvow.report(k, event) for k in linked]):
            self._set(b["id"], reported_to_keptvow=event)

    # -- summaries ------------------------------------------------------------------------------
    def service_summary(self, service_id):
        period = self.open_period()
        rows = self.store.all(
            "SELECT o.name AS owner, a.name AS agent, u.agent_id, SUM(u.units) AS units, SUM(u.amount) AS amount"
            " FROM usage u JOIN agents a ON a.id=u.agent_id JOIN owners o ON o.id=u.owner_id"
            " WHERE u.service_id=? AND u.period=? GROUP BY u.agent_id ORDER BY o.name, a.name", service_id, period)
        paid_bills = self.store.one("SELECT COUNT(*) AS n FROM bills WHERE service_id=? AND status='paid'", service_id)["n"]
        over = max(0, paid_bills - self.cfg.included_settlements)
        return {
            "period": period,
            "this_month": [{**r, "amount": dollars(r["amount"]), "trust_level": self.level(self.store.agent(r["agent_id"]))} for r in rows],
            "this_month_total": dollars(sum(r["amount"] for r in rows)),
            "keptvow_fee": {"paid_bills": paid_bills, "included_in_plan": self.cfg.included_settlements,
                            "billable": over, "amount": cents_str(over),
                            "note": "1¢ per paid bill above what your plan includes. Billed to you separately; never taken from your invoices."},
        }

    def owner_summary(self, owner_id):
        period = self.open_period()
        agents = self.store.all("SELECT id, name, keptvow_id, keptvow_verified, monthly_cap, active FROM agents WHERE owner_id=? ORDER BY name", owner_id)
        for a in agents:
            spent = self._spent(a["id"], period)
            a["spent_this_month"] = dollars(spent)
            a["monthly_cap"] = None if a["monthly_cap"] is None else dollars(a["monthly_cap"])
        return {"period": period, "agents": agents, "bills": self.bills(owner_id=owner_id)}


def valid_policy(p):
    p = dict(p)
    if p.get("min_trust", "unknown") not in LEVELS:
        raise ValueError(f"min_trust must be one of {LEVELS}")
    for k in ("unknown_monthly_cap", "agent_monthly_cap"):
        if p.get(k) is not None and (not isinstance(p[k], int) or p[k] < 0):
            raise ValueError(f"{k} must be a whole number of micro-dollars, or null")
    unknown = set(p) - {"min_trust", "unknown_monthly_cap", "agent_monthly_cap"}
    if unknown:
        raise ValueError(f"unknown policy fields: {sorted(unknown)}")
    return p


def valid_prices(p):
    if not isinstance(p, dict) or not p:
        raise ValueError("prices must be an object of tool name -> micro-dollars per call")
    for tool, v in p.items():
        if not isinstance(tool, str) or not tool or len(tool) > 100:
            raise ValueError("tool names must be 1-100 characters")
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100_000_000:
            raise ValueError(f"price for {tool!r} must be whole micro-dollars between 0 and 100,000,000 ($100)")
    return p
