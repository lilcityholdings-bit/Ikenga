"""Money in and out.

In: x402 (v2). A bot tops up its own balance by paying over HTTP, with no human and no card:
    1. POST /v1/deposits/x402 {"amount": N}              -> 402 + PAYMENT-REQUIRED header
    2. the bot signs a payment for exactly those terms (its wallet, its x402 client library)
    3. same request again with a PAYMENT-SIGNATURE header -> the shop has the facilitator verify
       and settle it on-chain, then credits the balance and returns PAYMENT-RESPONSE.
    Wire format per the x402 v2 spec: base64 JSON headers, CAIP-2 network ids, atomic amounts,
    facilitator POST /verify and /settle taking {x402Version, paymentPayload, paymentRequirements}.
    The settlement's transaction hash is a unique key, so one payment can't be credited twice.

In: Stripe Checkout, for businesses that pay by card rather than wallet. POST /v1/deposits/stripe
    returns a Checkout URL. Stripe calls POST /v1/webhooks/stripe when it's paid; the signature is
    checked (HMAC-SHA256 over "timestamp.body", 5-minute tolerance) and the session id is a unique
    key, so replays and duplicate deliveries credit nothing. 1 cent = 10,000 units.

Out: withdrawals always go to the account's registered destination, never one named in the
    request. They pay out automatically when ALL of these hold, and otherwise wait for the operator:
      - a payout adapter is configured (ABS_PAYOUT_CMD: gets the payout as JSON, performs it with
        your provider, e.g. a Coinbase CDP wallet or Stripe Connect, and prints {"reference": ...});
      - the operator has verified the account (KYC), via POST /v1/admin/accounts/{id}/verify;
      - the destination was set at least ABS_PAYOUT_COOLING_S ago (default 24h), so a stolen key
        can't redirect funds and cash out at once;
      - the amount is within ABS_AUTO_PAYOUT_MAX and the account's rolling-24h ABS_AUTO_PAYOUT_DAILY;
      - the money has been in the account for ABS_FUNDS_AGE_S (default 7 days): funds received
        more recently can't be paid out automatically. This breaks the card-fraud chain of stolen
        card -> top up -> fund a bounty -> a sybil repairer wins -> cash out before the chargeback.
    Card disputes and refunds (charge.dispute.created, charge.refunded) freeze the paying account
    and claw back whatever of the amount is still there; the event records any shortfall.
    If the adapter fails, the payout is FAILED and the funds return. If the process dies mid-payout,
    it stays PENDING for a human to reconcile: never retried automatically, so never paid twice.

The promo/real barrier (same rule as the matching engine's IKENGA_REAL_MONEY):
    ABS_REAL_MONEY unset: operator grants create play credits; x402 deposits and withdrawals are off.
    ABS_REAL_MONEY=1:     grants are off, so every unit in the system came from a real deposit.
    Real and play money are never in the same ledger, so play credits can never be withdrawn.
    Holding third parties' funds in escrow is money transmission in most jurisdictions: read
    docs/COMPLIANCE-NOTES.md before turning it on.
"""
import base64
import hashlib
import hmac
import json
import os
import shlex
import subprocess
import time
import urllib.parse
import urllib.request

import ledger
from db import ApiError


def b64json(obj):
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def unb64json(s):
    try:
        return json.loads(base64.b64decode(s, validate=True))
    except (ValueError, TypeError):
        raise ApiError(400, "PAYMENT-SIGNATURE is not base64-encoded JSON")


class Payments:
    def __init__(self, store, env=None):
        env = os.environ if env is None else env
        self.store = store
        self.real_money = env.get("ABS_REAL_MONEY") == "1"
        self.facilitator = (env.get("ABS_X402_FACILITATOR_URL") or "").rstrip("/")
        self.network = env.get("ABS_X402_NETWORK", "eip155:8453")                      # Base mainnet
        self.asset = env.get("ABS_X402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")  # USDC on Base
        self.pay_to = env.get("ABS_X402_PAY_TO")
        self.extra = json.loads(env.get("ABS_X402_EXTRA", '{"name":"USD Coin","version":"2"}'))
        self.public_url = env.get("ABS_PUBLIC_URL", "http://localhost:8090").rstrip("/")
        self.post = _post_json
        self.stripe_key = env.get("ABS_STRIPE_SECRET_KEY")
        self.stripe_webhook_secret = env.get("ABS_STRIPE_WEBHOOK_SECRET")
        self.stripe_api = env.get("ABS_STRIPE_API", "https://api.stripe.com")
        self.stripe_post = _post_form
        self.payout_cmd = env.get("ABS_PAYOUT_CMD")
        self.payout_asset = env.get("ABS_PAYOUT_ASSET", "USDC")
        self.auto_max = int(env.get("ABS_AUTO_PAYOUT_MAX", "0"))
        self.auto_daily = int(env.get("ABS_AUTO_PAYOUT_DAILY", "0"))
        self.cooling = float(env.get("ABS_PAYOUT_COOLING_S", "86400"))
        self.funds_age = float(env.get("ABS_FUNDS_AGE_S", str(7 * 86400)))
        self.run_payout = self._run_payout_cmd

    def x402_ready(self):
        return self.real_money and bool(self.facilitator and self.pay_to)

    def requirements(self, amount):
        return {"scheme": "exact", "network": self.network, "amount": str(amount), "asset": self.asset,
                "payTo": self.pay_to, "maxTimeoutSeconds": 120, "extra": self.extra}

    def x402_deposit(self, principal, amount, signature_header):
        if not self.real_money:
            raise ApiError(403, "Real-money deposits are disabled (ABS_REAL_MONEY is not set)")
        if not self.x402_ready():
            raise ApiError(503, "x402 is not configured (ABS_X402_FACILITATOR_URL, ABS_X402_PAY_TO)")
        req = self.requirements(amount)
        resource = {"url": f"{self.public_url}/v1/deposits/x402", "description": f"Deposit {amount} units",
                    "mimeType": "application/json"}
        if not signature_header:
            body = {"x402Version": 2, "error": "PAYMENT-SIGNATURE header is required", "resource": resource,
                    "accepts": [req], "extensions": {}}
            raise ApiError(402, body["error"], headers={"PAYMENT-REQUIRED": b64json(body)}, body=body)
        payload = unb64json(signature_header)
        accepted = payload.get("accepted") if isinstance(payload, dict) else None
        if accepted != req:
            # The client must have signed for exactly these terms. A payment built for a
            # different amount, asset or recipient is refused before anything is sent to the chain.
            raise ApiError(402, "Signed payment does not match the payment requirements",
                           headers={"PAYMENT-REQUIRED": b64json({"x402Version": 2, "resource": resource,
                                                                  "accepts": [req], "extensions": {},
                                                                  "error": "requirements mismatch"})})
        msg = {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": req}
        verify = self.post(self.facilitator + "/verify", msg)
        if not verify.get("isValid"):
            raise ApiError(402, f"Payment rejected by facilitator: {verify.get('invalidReason', 'invalid')}")
        settle = self.post(self.facilitator + "/settle", msg)
        if not settle.get("success") or not settle.get("transaction"):
            raise ApiError(402, f"Payment settlement failed: {settle.get('errorReason', 'unknown')}")
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM deposits WHERE reference=?", (settle["transaction"],)).fetchone():
                raise ApiError(409, "This payment was already credited")
            db.execute("INSERT INTO deposits (account_id, amount, rail, reference, payer, created_at) VALUES (?,?,?,?,?,?)",
                       (principal["account_id"], amount, "x402", settle["transaction"], settle.get("payer"), self.store.now()))
            ledger.transfer(self.store, "world:x402", ledger.acct(principal["account_id"]), amount, "deposit",
                            settle["transaction"])
            bal = ledger.balance(db, ledger.acct(principal["account_id"]))
        return {"status": "CREDITED", "amount": amount, "balance": bal, "transaction": settle["transaction"],
                "network": settle.get("network")}, {"PAYMENT-RESPONSE": b64json(settle)}

    def grant(self, account_id, amount):
        if self.real_money:
            raise ApiError(403, "Grants are disabled in real-money mode: every unit must come from a deposit")
        with self.store.tx() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ApiError(404, "Account not found")
            ledger.transfer(self.store, "world:grants", ledger.acct(account_id), amount, "grant")
            return {"account_id": account_id, "balance": ledger.balance(db, ledger.acct(account_id))}

    # ---- Stripe ---------------------------------------------------------------------------

    def stripe_checkout(self, principal, amount_cents):
        if not self.real_money:
            raise ApiError(403, "Real-money deposits are disabled (ABS_REAL_MONEY is not set)")
        if not (self.stripe_key and self.stripe_webhook_secret):
            raise ApiError(503, "Stripe is not configured (ABS_STRIPE_SECRET_KEY, ABS_STRIPE_WEBHOOK_SECRET)")
        if not (50 <= amount_cents <= 10_000_000):
            raise ApiError(422, "amount_cents must be between 50 and 10,000,000")
        aid = principal["account_id"]
        form = {"mode": "payment", "client_reference_id": aid, "metadata[account_id]": aid,
                "success_url": f"{self.public_url}/dashboard?deposit=ok", "cancel_url": f"{self.public_url}/dashboard",
                "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": "usd",
                "line_items[0][price_data][unit_amount]": str(amount_cents),
                "line_items[0][price_data][product_data][name]": "Auto Body Shop balance"}
        session = self.stripe_post(self.stripe_api + "/v1/checkout/sessions", form, self.stripe_key)
        if "id" not in session or "url" not in session:
            raise ApiError(502, f"Stripe error: {session.get('error', {}).get('message', 'unknown')}")
        return {"session_id": session["id"], "url": session["url"], "amount_cents": amount_cents}

    def stripe_webhook(self, raw, signature_header):
        if not (self.real_money and self.stripe_webhook_secret):
            raise ApiError(503, "Stripe webhooks are not enabled (ABS_REAL_MONEY and ABS_STRIPE_WEBHOOK_SECRET)")
        parts = dict(p.split("=", 1) for p in (signature_header or "").split(",") if "=" in p)
        sigs = [v for k, v in (p.split("=", 1) for p in (signature_header or "").split(",") if "=" in p) if k == "v1"]
        try:
            ts = int(parts.get("t", ""))
        except ValueError:
            raise ApiError(400, "Missing Stripe-Signature timestamp")
        expected = hmac.new(self.stripe_webhook_secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, s) for s in sigs):
            raise ApiError(400, "Bad Stripe signature")
        if abs(time.time() - ts) > 300:
            raise ApiError(400, "Stripe signature too old")
        event = json.loads(raw)
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM webhook_events WHERE id=?", (event.get("id"),)).fetchone():
                return {"received": True, "credited": False, "duplicate": True}
            db.execute("INSERT INTO webhook_events (id, created_at) VALUES (?,?)", (event.get("id"), self.store.now()))
            if event.get("type") in ("charge.dispute.created", "charge.refunded"):
                return self._stripe_reversal(db, event)
        if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            return {"received": True, "credited": False}
        obj = event["data"]["object"]
        if obj.get("payment_status") != "paid" or obj.get("currency") != "usd":
            return {"received": True, "credited": False}
        account_id = obj.get("client_reference_id")
        units = int(obj["amount_total"]) * 10_000
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM deposits WHERE reference=?", (obj["id"],)).fetchone():
                return {"received": True, "credited": False, "duplicate": True}
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ApiError(422, "Unknown account on checkout session")
            db.execute("INSERT INTO deposits (account_id, amount, rail, reference, payment_ref, payer, created_at)"
                       " VALUES (?,?,?,?,?,?,?)", (account_id, units, "stripe", obj["id"], obj.get("payment_intent"),
                                                   obj.get("customer"), self.store.now()))
            ledger.transfer(self.store, "world:stripe", ledger.acct(account_id), units, "deposit", obj["id"])
        self.store.event("deposit", {"rail": "stripe", "amount": units, "reference": obj["id"]}, account_id=account_id)
        return {"received": True, "credited": True}

    def _stripe_reversal(self, db, event):
        """Money we credited is being taken back by the card network. Freeze the account (it can
        read, but not spend or withdraw, until the operator looks) and recover what's still there."""
        obj = event["data"]["object"]
        dep = db.execute("SELECT * FROM deposits WHERE rail='stripe' AND payment_ref=?", (obj.get("payment_intent"),)).fetchone()
        if dep is None:
            return {"received": True, "reversed": False}
        cents = obj.get("amount_refunded") if event["type"] == "charge.refunded" else obj.get("amount")
        units = min(int(cents or 0) * 10_000, dep["amount"])
        a = ledger.acct(dep["account_id"])
        recovered = min(units, ledger.balance(db, a))
        ledger.transfer(self.store, a, "world:stripe", recovered, "card_reversal", obj.get("id"))
        db.execute("UPDATE accounts SET frozen=1 WHERE id=?", (dep["account_id"],))
        self.store.event("card_reversal", {"type": event["type"], "amount": units, "recovered": recovered,
                                           "shortfall": units - recovered, "deposit": dep["reference"]},
                         account_id=dep["account_id"])
        return {"received": True, "reversed": True, "recovered": recovered, "shortfall": units - recovered}

    # ---- payouts ---------------------------------------------------------------------------

    def set_destination(self, principal, destination):
        if principal["scope"] != "owner":
            raise ApiError(403, "This action needs an owner key")
        if not isinstance(destination, str) or not (1 <= len(destination) <= 200):
            raise ApiError(422, "destination is required")
        with self.store.tx() as db:
            db.execute("INSERT INTO payout_profiles (account_id, destination, destination_set_at) VALUES (?,?,?)"
                       " ON CONFLICT(account_id) DO UPDATE SET destination=excluded.destination,"
                       " destination_set_at=excluded.destination_set_at",
                       (principal["account_id"], destination, self.store.now()))
        self.store.event("payout_destination_set", {"destination": destination}, account_id=principal["account_id"])
        return {"destination": destination, "auto_payouts_from": self.store.now() + self.cooling}

    def verify_account(self, account_id, verified):
        with self.store.tx() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise ApiError(404, "Account not found")
            db.execute("INSERT INTO payout_profiles (account_id, verified) VALUES (?,?)"
                       " ON CONFLICT(account_id) DO UPDATE SET verified=excluded.verified", (account_id, int(bool(verified))))
        return {"account_id": account_id, "verified": bool(verified)}

    def withdraw(self, principal, amount):
        if principal["scope"] != "owner":
            raise ApiError(403, "Withdrawals need an owner key: bots can spend inside the shop, not cash out")
        if not self.real_money:
            raise ApiError(403, "Withdrawals are disabled: balances are play credits (ABS_REAL_MONEY is not set)")
        aid = principal["account_id"]
        with self.store.tx() as db:
            prof = db.execute("SELECT * FROM payout_profiles WHERE account_id=?", (aid,)).fetchone()
            if prof is None or not prof["destination"]:
                raise ApiError(409, "Register a payout destination first (POST /v1/payouts/destination)")
            wid = db.execute("INSERT INTO withdrawals (account_id, amount, destination, status, created_at)"
                             " VALUES (?,?,?,'PENDING',?)", (aid, amount, prof["destination"], self.store.now())).lastrowid
            ledger.transfer(self.store, ledger.acct(aid), "pending:withdrawals", amount, "withdrawal", f"withdrawal:{wid}")
            auto, why = self._auto_ok(db, prof, amount, wid)
        if not auto:
            return {"withdrawal_id": wid, "status": "PENDING", "destination": prof["destination"], "review": why}
        # Runs outside the database lock: the provider call can be slow.
        try:
            reference = self.run_payout({"withdrawal_id": wid, "amount": amount, "destination": prof["destination"],
                                         "asset": self.payout_asset})
            return dict(self.finish_withdrawal(wid, "PAID", reference), destination=prof["destination"], automatic=True)
        except Exception as e:
            out = self.finish_withdrawal(wid, "FAILED", repr(e)[:200])
            return dict(out, error=f"payout failed, funds returned: {e}"[:300])

    def _auto_ok(self, db, prof, amount, wid):
        if not self.payout_cmd:
            return False, "no payout adapter configured"
        if not prof["verified"]:
            return False, "account not verified by the operator"
        if self.store.now() - (prof["destination_set_at"] or 0) < self.cooling:
            return False, "payout destination changed recently (cooling period)"
        if amount > self.auto_max:
            return False, f"over the automatic payout limit ({self.auto_max})"
        paid = db.execute("SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE account_id=? AND status IN ('PAID','PENDING')"
                          " AND created_at>? AND id<>?", (prof["account_id"], self.store.now() - 86400, wid)).fetchone()[0]
        if paid + amount > self.auto_daily:
            return False, f"over the automatic daily payout limit ({self.auto_daily})"
        a = ledger.acct(prof["account_id"])
        # Everything that arrived recently (deposits, awards, warranty releases), except money that
        # was only ever this account's own coming back to it (released holds, failed payouts).
        recent = db.execute("SELECT COALESCE(SUM(amount), 0) FROM transfers WHERE to_acct=? AND created_at>?"
                            " AND kind NOT IN ('hold_release','withdrawal_failed','bounty_refund','warranty_refund',"
                            " 'referee_fee_refund')",
                            (a, self.store.now() - self.funds_age)).fetchone()[0]
        settled = ledger.balance(db, a) + amount - recent  # balance before this withdrawal, minus young money
        if amount > settled:
            return False, "includes funds received too recently for an automatic payout"
        return True, None

    def _run_payout_cmd(self, payout):
        p = subprocess.run(shlex.split(self.payout_cmd), input=json.dumps(payout).encode(), capture_output=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="replace")[-200:] or f"exit {p.returncode}")
        return json.loads(p.stdout)["reference"]

    def finish_withdrawal(self, wid, status, reference):
        if status not in ("PAID", "FAILED"):
            raise ApiError(422, "status must be PAID or FAILED")
        with self.store.tx() as db:
            w = db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
            if w is None or w["status"] != "PENDING":
                raise ApiError(404, "No pending withdrawal with that id")
            to = "world:payouts" if status == "PAID" else ledger.acct(w["account_id"])
            ledger.transfer(self.store, "pending:withdrawals", to, w["amount"], "withdrawal_" + status.lower(),
                            f"withdrawal:{wid}")
            db.execute("UPDATE withdrawals SET status=?, reference=?, updated_at=? WHERE id=?",
                       (status, reference, self.store.now(), wid))
        self.store.event("withdrawal_" + status.lower(), {"withdrawal_id": wid, "amount": w["amount"], "reference": reference},
                         account_id=w["account_id"])
        return {"withdrawal_id": wid, "status": status, "reference": reference}


def _post_form(url, form, key):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            raise ApiError(502, f"Stripe error: HTTP {e.code}")
    except (OSError, ValueError) as e:
        raise ApiError(502, f"Stripe unreachable: {e}")


def _post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            raise ApiError(502, f"Facilitator error: HTTP {e.code}")
    except (OSError, ValueError) as e:
        raise ApiError(502, f"Facilitator unreachable: {e}")
