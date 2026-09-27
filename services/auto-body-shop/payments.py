"""Money in and out.

In: x402 (v2). A bot tops up its own balance by paying over HTTP, with no human and no card:
    1. POST /v1/deposits/x402 {"amount": N}              -> 402 + PAYMENT-REQUIRED header
    2. the bot signs a payment for exactly those terms (its wallet, its x402 client library)
    3. same request again with a PAYMENT-SIGNATURE header -> the shop has the facilitator verify
       and settle it on-chain, then credits the balance and returns PAYMENT-RESPONSE.
    Wire format per the x402 v2 spec: base64 JSON headers, CAIP-2 network ids, atomic amounts,
    facilitator POST /verify and /settle taking {x402Version, paymentPayload, paymentRequirements}.
    The settlement's transaction hash is a unique key, so one payment can't be credited twice.

Out: withdrawals are *requests*. Funds move to pending:withdrawals at once, and the operator
    pays them out and marks them PAID (or FAILED, which returns the funds). Paying money out of
    the system to a third party is the regulated step, so it is deliberately not automated here.

The promo/real barrier (same rule as the matching engine's IKENGA_REAL_MONEY):
    ABS_REAL_MONEY unset: operator grants create play credits; x402 deposits and withdrawals are off.
    ABS_REAL_MONEY=1:     grants are off, so every unit in the system came from a real deposit.
    Real and play money are never in the same ledger, so play credits can never be withdrawn.
    Holding third parties' funds in escrow is money transmission in most jurisdictions: read
    docs/COMPLIANCE-NOTES.md before turning it on.
"""
import base64
import json
import os
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

    def withdraw(self, principal, amount, destination):
        if principal["scope"] != "owner":
            raise ApiError(403, "Withdrawals need an owner key: bots can spend inside the shop, not cash out")
        if not self.real_money:
            raise ApiError(403, "Withdrawals are disabled: balances are play credits (ABS_REAL_MONEY is not set)")
        if not isinstance(destination, str) or not (1 <= len(destination) <= 200):
            raise ApiError(422, "destination is required")
        with self.store.tx() as db:
            wid = db.execute("INSERT INTO withdrawals (account_id, amount, destination, status, created_at)"
                             " VALUES (?,?,?,'PENDING',?)", (principal["account_id"], amount, destination,
                                                             self.store.now())).lastrowid
            ledger.transfer(self.store, ledger.acct(principal["account_id"]), "pending:withdrawals", amount,
                            "withdrawal", f"withdrawal:{wid}")
        return {"withdrawal_id": wid, "status": "PENDING"}

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
        return {"withdrawal_id": wid, "status": status}


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
