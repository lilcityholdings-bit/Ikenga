"""Money: a double-entry ledger plus spending mandates.

How bots pay without a human approving each payment
---------------------------------------------------
A human signs off once, on a *mandate*: "this account's bots may spend up to X per payment and
Y per day, for this purpose, on this agent, until this date" (the same idea as AP2's intent
mandates). After that, every automated debit (an agent posting a repair bounty for itself, a
repair bot paying the referee to score its fix) is checked against a live mandate inside the
same transaction that moves the money. A payment within the mandate goes through with nobody
asked. One outside it is refused outright, with the reason recorded as an event. It is never
queued for someone to approve, because a queue nobody watches is just a slower refusal.

Owner keys (the human) can spend without a mandate. Bot keys never can.

Accounts are strings:
    acct:<id>                 a customer or repair bot's spendable balance
    escrow:bounty:<id>        a bounty's reward, held until settlement
    escrow:warranty:<id>      the part of an award held back until the fix proves itself live
    hold:<ref>                a pre-authorised maximum for metered work (see hold/settle_hold)
    platform:fees             the operator's take and referee fees (revenue)
    pending:withdrawals       funds on their way out, awaiting payout
    world:<rail>              money outside the system (x402 deposits, grants, payouts); may go negative
"""
import secrets

from db import ApiError, canonical, sha256

DAY = 86400.0
PURPOSES = ("bounty", "eval_fee", "any")


def acct(account_id):
    return f"acct:{account_id}"


def balance(db, account):
    row = db.execute("SELECT amount FROM balances WHERE account=?", (account,)).fetchone()
    return row["amount"] if row else 0


def transfer(store, from_acct, to_acct, amount, kind, ref=None, mandate_id=None):
    if amount <= 0:
        return
    with store.tx() as db:
        if not from_acct.startswith("world:") and balance(db, from_acct) < amount:
            raise ApiError(402, f"Insufficient balance in {from_acct}: need {amount}, have {balance(db, from_acct)}")
        for account, delta in ((from_acct, -amount), (to_acct, amount)):
            db.execute("INSERT INTO balances (account, amount) VALUES (?, ?) "
                       "ON CONFLICT(account) DO UPDATE SET amount = amount + excluded.amount", (account, delta))
        db.execute("INSERT INTO transfers (from_acct, to_acct, amount, kind, ref, mandate_id, created_at)"
                   " VALUES (?,?,?,?,?,?,?)", (from_acct, to_acct, amount, kind, ref, mandate_id, store.now()))


def create_mandate(store, account_id, body):
    purpose = body.get("purpose")
    if purpose not in PURPOSES:
        raise ApiError(422, f"purpose must be one of {', '.join(PURPOSES)}")
    try:
        max_per_tx, max_per_day = int(body["max_per_tx"]), int(body["max_per_day"])
        ttl = float(body.get("ttl_s", 30 * DAY))
    except (KeyError, TypeError, ValueError):
        raise ApiError(422, "max_per_tx and max_per_day are required integers (atomic units)")
    if max_per_tx <= 0 or max_per_day < max_per_tx or ttl <= 0:
        raise ApiError(422, "Need 0 < max_per_tx <= max_per_day and ttl_s > 0")
    agent_id = body.get("agent_id")
    with store.tx() as db:
        if agent_id is not None and not db.execute(
                "SELECT 1 FROM agents WHERE agent_id=? AND account_id=?", (agent_id, account_id)).fetchone():
            raise ApiError(404, f"Agent {agent_id} not found on this account")
        mid = "mdt_" + secrets.token_hex(8)
        terms = {"id": mid, "account_id": account_id, "agent_id": agent_id, "purpose": purpose,
                 "max_per_tx": max_per_tx, "max_per_day": max_per_day, "expires_at": store.now() + ttl}
        db.execute("INSERT INTO mandates (id, account_id, agent_id, purpose, max_per_tx, max_per_day, expires_at,"
                   " terms_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (mid, account_id, agent_id, purpose, max_per_tx, max_per_day, terms["expires_at"],
                    sha256(canonical(terms)), store.now()))
    store.event("mandate_created", terms, agent_id=agent_id, account_id=account_id)
    return dict(terms, terms_hash=sha256(canonical(terms)))


def authorize(store, principal, amount, purpose, agent_id=None):
    """Returns the mandate id covering this debit (None for an owner key), or raises 403 with
    the precise reason. Must be called inside the transaction that moves the money, so two
    concurrent payments can't both squeeze under the same daily cap."""
    if principal["scope"] == "owner":
        return None
    if principal.get("agent_id") and agent_id and principal["agent_id"] != agent_id:
        raise ApiError(403, "This key is bound to a different agent")
    with store.tx() as db:
        now = store.now()
        rows = db.execute(
            "SELECT * FROM mandates WHERE account_id=? AND revoked=0 AND expires_at>? AND purpose IN (?, 'any')"
            " AND (agent_id IS NULL OR agent_id=?) ORDER BY max_per_tx DESC",
            (principal["account_id"], now, purpose, agent_id)).fetchall()
        if not rows:
            raise ApiError(403, f"No active mandate covers '{purpose}' payments for this key")
        reasons = []
        for m in rows:
            if amount > m["max_per_tx"]:
                reasons.append(f"{m['id']}: {amount} exceeds max_per_tx {m['max_per_tx']}")
                continue
            # Net spend: money back to the account under the same mandate (the unused part of a
            # hold) doesn't count against the cap.
            spent = db.execute("SELECT COALESCE(SUM(CASE WHEN to_acct=? THEN -amount ELSE amount END), 0) FROM transfers"
                               " WHERE mandate_id=? AND created_at>?", (acct(m["account_id"]), m["id"], now - DAY)).fetchone()[0]
            if spent + amount > m["max_per_day"]:
                reasons.append(f"{m['id']}: would bring 24h spend to {spent + amount}, over max_per_day {m['max_per_day']}")
                continue
            return m["id"]
        raise ApiError(403, "Mandate limits refuse this payment: " + "; ".join(reasons))


def spend(store, principal, amount, purpose, to_acct, kind, ref, agent_id=None):
    with store.tx():
        mandate_id = authorize(store, principal, amount, purpose, agent_id)
        transfer(store, acct(principal["account_id"]), to_acct, amount, kind, ref, mandate_id)
        return mandate_id


def hold(store, principal, amount, purpose, ref, agent_id=None):
    """Authorise-then-capture, like a card pre-authorisation or x402's "upto" scheme: reserve the
    most this work can cost (checked against the mandate), then capture what it actually cost
    and release the rest. The payer never pays for an estimate."""
    with store.tx():
        mandate_id = authorize(store, principal, amount, purpose, agent_id)
        transfer(store, acct(principal["account_id"]), f"hold:{ref}", amount, "hold", ref, mandate_id)
        return mandate_id


def settle_hold(store, ref, account_id, capture, to_acct, kind):
    """Captures up to the held amount (a cost overrun is the operator's problem, never the
    payer's) and releases the remainder. The release carries the hold's mandate, so the unused
    part stops counting against that mandate's daily cap. Returns what was captured."""
    with store.tx() as db:
        held = balance(db, f"hold:{ref}")
        row = db.execute("SELECT mandate_id FROM transfers WHERE to_acct=? AND kind='hold' ORDER BY id DESC LIMIT 1",
                         (f"hold:{ref}",)).fetchone()
        mandate_id = row["mandate_id"] if row else None
        captured = max(0, min(capture, held))
        transfer(store, f"hold:{ref}", to_acct, captured, kind, ref)
        transfer(store, f"hold:{ref}", acct(account_id), held - captured, "hold_release", ref, mandate_id)
        return captured


def statement(db, account_id, after=0, limit=500):
    a = acct(account_id)
    rows = db.execute("SELECT id, from_acct, to_acct, amount, kind, ref, mandate_id, created_at FROM transfers"
                      " WHERE (from_acct=? OR to_acct=?) AND id>? ORDER BY id LIMIT ?", (a, a, after, limit)).fetchall()
    return [{"id": r["id"], "amount": r["amount"] if r["to_acct"] == a else -r["amount"],
             "counterparty": r["from_acct"] if r["to_acct"] == a else r["to_acct"], "kind": r["kind"],
             "ref": r["ref"], "mandate_id": r["mandate_id"], "at": r["created_at"]} for r in rows]


def invariant(db):
    """Sum of all balances. Must always be exactly zero."""
    return db.execute("SELECT COALESCE(SUM(amount), 0) FROM balances").fetchone()[0]
