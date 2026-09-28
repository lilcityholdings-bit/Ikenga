#!/usr/bin/env python3
"""Auto Body Shop: a repair market for AI agents. See shop.py for the whole loop, openapi.yaml
for the contract, and README.md for why it's built this way.

    python3 server.py        # :8090, state in data/autobodyshop.db

Stdlib only (http.server + sqlite3). The optional exception is ABS_RUNNER=claude, which uses the
`anthropic` SDK.
"""
import hmac
import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import ledger
import runners
from db import ApiError, Clock, Store, sha256
from payments import Payments
from shop import Shop

MAX_BODY = 1 << 20
# Responses that carry a freshly minted secret. They are never stored for idempotent replay:
# keys are otherwise only ever kept as hashes, and a replay cache must not undo that.
NO_REPLAY_PATHS = {"/v1/keys", "/v1/agents", "/v1/accounts"}
IDEMPOTENCY_TTL = 86400
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                    "Cache-Control": "no-store"}


class RateLimiter:
    """Token buckets keyed by API key (or client IP when unauthenticated)."""

    def __init__(self):
        self.buckets, self.lock = {}, threading.Lock()

    def take(self, key, per_minute, burst=None, cost=1.0):
        """Returns 0 if allowed, else seconds to wait."""
        burst = per_minute if burst is None else burst
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (burst, now))
            tokens = min(burst, tokens + (now - last) * per_minute / 60.0)
            if tokens < cost:
                self.buckets[key] = (tokens, now)
                return math.ceil((cost - tokens) * 60.0 / per_minute)
            self.buckets[key] = (tokens - cost, now)
            if len(self.buckets) > 100_000:
                # Bound memory by dropping idle buckets (idle means refilled anyway). Clearing
                # everything would let a flood of fresh keys reset every client's limit.
                cutoff = now - 600
                self.buckets = {k: v for k, v in self.buckets.items() if v[1] > cutoff}
            return 0


def field(body, name, kind, required=True):
    if name not in body:
        if required:
            raise ApiError(422, f"Field required: {name}")
        return None
    v = body[name]
    ok = {"str": isinstance(v, str) and v != "", "bool": isinstance(v, bool),
          "num": isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
          "int": isinstance(v, int) and not isinstance(v, bool)}[kind]
    if not ok:
        raise ApiError(422, f"Invalid value for {name}")
    return v


def positive_amount(body):
    amount = field(body, "amount", "int")
    if amount <= 0 or amount > 10**15:
        raise ApiError(422, "amount must be a positive integer (atomic units)")
    return amount


class App:
    def __init__(self, shop, payments, admin_key, settings):
        self.shop, self.payments, self.admin_key, self.s = shop, payments, admin_key, settings
        self.limiter = RateLimiter()
        self.ctx = threading.local()  # per-request headers and client IP (handlers run on many threads)
        self.inflight, self.inflight_lock = set(), threading.Lock()
        self.routes = [
            ("GET", r"/", self.index, None),
            ("GET", r"/health", self.health, None),
            ("POST", r"/v1/accounts", self.signup, None),
            ("GET", r"/dashboard", self.dashboard, None),
            ("GET", r"/v1/accounts/me", lambda p, b, q: self.shop.me(p), "any"),
            ("GET", r"/v1/accounts/me/statement", self.statement, "any"),
            ("GET", r"/v1/accounts/(?P<account_id>[^/]+)/reputation", lambda p, b, q, account_id: self.shop.reputation(account_id), None),
            ("POST", r"/v1/keys", lambda p, b, q: self.shop.new_bot_key(p, b), "owner"),
            ("POST", r"/v1/keys/(?P<key_id>[^/]+)/revoke", self.revoke_key, "owner"),
            ("POST", r"/v1/mandates", lambda p, b, q: ledger.create_mandate(self.shop.store, p["account_id"], b), "owner"),
            ("POST", r"/v1/mandates/(?P<mid>[^/]+)/revoke", self.revoke_mandate, "owner"),
            ("POST", r"/v1/agents", lambda p, b, q: (201, self.shop.create_agent(p, b)), "owner"),
            ("POST", r"/v1/agents/(?P<agent_id>[^/]+)", lambda p, b, q, agent_id: self.shop.update_agent(p, agent_id, b), "owner"),
            ("GET", r"/v1/agents/(?P<agent_id>[^/]+)/events", self.events, "any"),
            ("GET", r"/v1/agents/(?P<agent_id>[^/]+)/quote", lambda p, b, q, agent_id: self.shop.quote(p, agent_id), "any"),
            ("GET", r"/v1/config/(?P<agent_id>[^/]+)", self.get_config, "any"),
            ("POST", r"/v1/telemetry", self.telemetry, "any"),
            ("POST", r"/v1/feedback", lambda p, b, q: self.shop.feedback(p, b), "any"),
            ("POST", r"/v1/candidates", self.candidates, "owner"),
            ("POST", r"/v1/approve", self.approve, "owner"),
            ("POST", r"/v1/rollback", self.rollback, "owner"),
            ("GET", r"/v1/versions/(?P<agent_id>[^/]+)", lambda p, b, q, agent_id: self.shop.versions(p, agent_id), "any"),
            ("POST", r"/v1/rollouts/(?P<agent_id>[^/]+)", lambda p, b, q, agent_id: (201, self.shop.start_rollout(p, agent_id, b)), "owner"),
            ("GET", r"/v1/bounties", lambda p, b, q: {"bounties": self.shop.list_bounties(q.get("status", "OPEN"))}, None),
            ("POST", r"/v1/bounties", self.post_bounty, "any"),
            ("GET", r"/v1/bounties/(?P<bid>\d+)", lambda p, b, q, bid: self.shop.bounty_view(p, int(bid)), "any"),
            ("POST", r"/v1/bounties/(?P<bid>\d+)/submissions", lambda p, b, q, bid: (202, self.shop.submit(p, int(bid), b)), "any"),
            ("GET", r"/v1/submissions/(?P<sid>\d+)", lambda p, b, q, sid: self.shop.submission_view(p, int(sid)), "any"),
            ("POST", r"/v1/deposits/x402", self.deposit, "any"),
            ("POST", r"/v1/deposits/stripe", self.stripe_checkout, "any"),
            ("POST", r"/v1/webhooks/stripe", self.stripe_webhook, None),
            ("POST", r"/v1/payouts/destination", lambda p, b, q: self.payments.set_destination(p, b.get("destination")), "owner"),
            ("POST", r"/v1/withdrawals", self.withdraw, "any"),
            ("POST", r"/v1/admin/accounts/(?P<account_id>[^/]+)/verify",
             lambda p, b, q, account_id: self.payments.verify_account(account_id, b.get("verified", True)), "admin"),
            ("POST", r"/v1/admin/grants", self.admin_grant, "admin"),
            ("POST", r"/v1/admin/withdrawals/(?P<wid>\d+)", self.admin_withdrawal, "admin"),
            ("GET", r"/v1/admin/ledger", self.admin_ledger, "admin"),
        ]

    # ---- plumbing -------------------------------------------------------------------------

    def handle(self, method, raw_path, headers, body, client_ip, raw=b""):
        self.ctx.raw = raw
        url = urlsplit(raw_path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        for m, pattern, fn, auth in self.routes:
            match = re.fullmatch(pattern, url.path)
            if m != method or not match:
                continue
            self.ctx.headers, self.ctx.ip = headers, client_ip
            principal = self.authenticate(headers, auth, client_ip)
            idem = headers.get("Idempotency-Key")
            if method == "POST" and principal is not None and principal.get("frozen"):
                raise ApiError(403, "Account frozen after a payment dispute; contact the operator")
            if method == "POST" and idem and principal is not None and url.path not in NO_REPLAY_PATHS:
                return self.idempotent(principal, idem, method, url.path, raw,
                                       lambda: fn(principal, body, query, **match.groupdict()))
            out = fn(principal, body, query, **match.groupdict())
            return out if isinstance(out, tuple) else (200, out)
        raise ApiError(404, "Not Found")

    def idempotent(self, principal, idem, method, path, raw, run):
        """A bot that times out and retries must not pay twice. The first response to each
        (key, Idempotency-Key) pair is stored and replayed, including errors below 500 (a
        refusal stays a refusal). Reusing the key for a different request is an error, and so
        is a retry while the first attempt is still running."""
        if len(idem) > 200:
            raise ApiError(422, "Idempotency-Key too long")
        fp = sha256(method.encode() + b" " + path.encode() + b" " + raw)
        slot = (principal["key_id"], idem)
        store = self.shop.store
        with store.tx() as db:
            row = db.execute("SELECT * FROM idempotency WHERE key_id=? AND idem_key=?", slot).fetchone()
        if row is not None:
            if row["request"] != fp:
                raise ApiError(422, "Idempotency-Key was already used for a different request")
            stored = json.loads(row["response"])
            return row["status"], stored["body"], dict(stored["headers"], **{"Idempotent-Replayed": "true"})
        with self.inflight_lock:
            if slot in self.inflight:
                raise ApiError(409, "A request with this Idempotency-Key is still in progress")
            self.inflight.add(slot)
        try:
            try:
                out = run()
                out = out if isinstance(out, tuple) else (200, out)
                status, payload, extra = out[0], out[1], (out[2] if len(out) > 2 else {})
            except ApiError as e:
                status, payload, extra = e.status, e.body or {"detail": e.detail}, e.headers
            if status < 500:
                with store.tx() as db:
                    db.execute("DELETE FROM idempotency WHERE created_at<?", (store.now() - IDEMPOTENCY_TTL,))
                    db.execute("INSERT INTO idempotency (key_id, idem_key, request, status, response, created_at)"
                               " VALUES (?,?,?,?,?,?)", (*slot, fp, status, json.dumps({"body": payload, "headers": extra}),
                                                         store.now()))
            return status, payload, extra
        finally:
            with self.inflight_lock:
                self.inflight.discard(slot)

    def authenticate(self, headers, auth, client_ip):
        if auth == "admin":
            given = headers.get("X-Admin-Key") or ""
            if self.admin_key is None or not hmac.compare_digest(given.encode(), self.admin_key.encode()):
                raise ApiError(401, "Missing or invalid X-Admin-Key")
            return None
        header = headers.get("Authorization") or ""
        principal = self.shop.resolve_key(header[7:]) if header.startswith("Bearer ") else None
        if header and principal is None:
            raise ApiError(401, "Invalid or revoked API key")
        if auth is not None and principal is None:
            raise ApiError(401, "Authorization: Bearer <api key> required")
        if auth == "owner" and principal["scope"] != "owner":
            raise ApiError(403, "This action needs an owner key")
        wait = self.limiter.take(principal["key_id"] if principal else "ip:" + client_ip, self.s["rate_per_min"])
        if wait:
            raise ApiError(429, "Rate limit exceeded", headers={"Retry-After": str(wait)})
        return principal

    # ---- routes ---------------------------------------------------------------------------

    def index(self, p, b, q):
        return {"service": "Auto Body Shop", "spec": "openapi.yaml",
                "what": "A repair market for AI agents: verified failures become bounties, repair bots compete on "
                        "hidden tests, and payment and the fix are both escrowed until the fix is proven.",
                "real_money": self.payments.real_money, "x402": self.payments.x402_ready(),
                "referee": self.s["runner_name"], "take_bps": self.s["take_bps"], "compute_margin_bps": self.s["margin_bps"],
                "max_cost_per_run": self.s["max_cost_per_run"],
                "rails": {"x402": self.payments.x402_ready(),
                          "stripe": bool(self.payments.real_money and self.payments.stripe_key),
                          "auto_payouts": bool(self.payments.real_money and self.payments.payout_cmd)},
                "dashboard": "/dashboard"}

    def health(self, p, b, q):
        with self.shop.store.tx() as db:
            imbalance = ledger.invariant(db)
        if imbalance != 0:
            raise ApiError(503, f"Ledger invariant broken: balances sum to {imbalance}")
        return {"status": "ok", "ledger_balanced": True, "referee": self.s["runner_name"],
                "real_money": self.payments.real_money}

    def signup(self, p, b, q):
        # Free accounts are what let repair bots join with no human, so the per-IP limit here is
        # much tighter than for authenticated calls.
        wait = self.limiter.take("signup:" + self.ctx.ip, self.s["signups_per_hour"] / 60.0,
                                 burst=self.s["signups_per_hour"])
        if wait:
            raise ApiError(429, "Too many signups from this address", headers={"Retry-After": str(wait)})
        return 201, self.shop.create_account(b.get("name"))

    def revoke_key(self, p, b, q, key_id):
        self.shop.revoke_key(p, key_id)
        return {"status": "REVOKED", "key_id": key_id}

    def revoke_mandate(self, p, b, q, mid):
        self.shop.revoke_mandate(p, mid)
        return {"status": "REVOKED", "mandate_id": mid}

    def events(self, p, b, q, agent_id):
        return {"events": self.shop.events(p, agent_id, int(q.get("after", 0)))}

    def get_config(self, p, b, q, agent_id):
        out = self.shop.get_config(p, agent_id, q.get("session"))
        etag = '"%s"' % out["fingerprint"]
        extra = {"ETag": etag, "Cache-Control": f"max-age={self.s['config_max_age']}"}
        if self.ctx.headers.get("If-None-Match") == etag:
            return 304, None, extra
        return 200, out, extra

    def telemetry(self, p, b, q):
        field(b, "agent_id", "str")
        latency = field(b, "latency_ms", "num")
        if latency < 0:
            raise ApiError(422, "Invalid value for latency_ms")
        field(b, "success", "bool")
        field(b, "version", "str", required=False)
        return self.shop.ingest(p, b)

    def candidates(self, p, b, q):
        version = self.shop.stage(p, b)
        return 201, {"status": "STAGED", "version": version}

    def approve(self, p, b, q):
        agent_id, version = field(b, "agent_id", "str"), field(b, "version", "str")
        self.shop.approve(p, agent_id, version)
        return {"status": "SUCCESS", "message": f"Version {version} promoted to production."}

    def rollback(self, p, b, q):
        agent_id, version = field(b, "agent_id", "str"), field(b, "version", "str")
        restored = self.shop.rollback(p, agent_id, version)
        return {"status": "ROLLED_BACK", "message": f"Agent restored to last stable configuration ({restored}).",
                "restored_version": restored}

    def post_bounty(self, p, b, q):
        return 201, self.shop.open_bounty(p, field(b, "agent_id", "str"), b)

    def deposit(self, p, b, q):
        out, headers = self.payments.x402_deposit(p, positive_amount(b), self.ctx.headers.get("PAYMENT-SIGNATURE"))
        return 200, out, headers

    def statement(self, p, b, q):
        with self.shop.store.tx() as db:
            return {"account_id": p["account_id"], "balance": ledger.balance(db, ledger.acct(p["account_id"])),
                    "entries": ledger.statement(db, p["account_id"], int(q.get("after", 0)))}

    def stripe_checkout(self, p, b, q):
        return 201, self.payments.stripe_checkout(p, field(b, "amount_cents", "int"))

    def stripe_webhook(self, p, b, q):
        return self.payments.stripe_webhook(self.ctx.raw, self.ctx.headers.get("Stripe-Signature"))

    def withdraw(self, p, b, q):
        out = self.payments.withdraw(p, positive_amount(b))
        return (200 if out["status"] == "PAID" else 202), out

    def dashboard(self, p, b, q):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), "rb") as f:
            return 200, None, {"Content-Type": "text/html; charset=utf-8", "_raw": f.read(),
                               "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                           "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"}

    def admin_grant(self, p, b, q):
        return self.payments.grant(field(b, "account_id", "str"), positive_amount(b))

    def admin_withdrawal(self, p, b, q, wid):
        return self.payments.finish_withdrawal(int(wid), b.get("status"), b.get("reference"))

    def admin_ledger(self, p, b, q):
        with self.shop.store.tx() as db:
            rows = db.execute("SELECT account, amount FROM balances ORDER BY account").fetchall()
            return {"balances": {r["account"]: r["amount"] for r in rows}, "sum": ledger.invariant(db)}


def make_handler(app, trust_proxy=False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AutoBodyShop/2.0"

        def _dispatch(self, method):
            extra = {}
            raw = b""
            try:
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ApiError(413, "Request body too large")
                    raw = self.rfile.read(length) if length else b""
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        raise ApiError(400, "Body is not valid JSON")
                    if not isinstance(body, dict):
                        raise ApiError(422, "Body must be a JSON object")
                ip = self.client_address[0]
                if trust_proxy and self.headers.get("X-Forwarded-For"):
                    # The rightmost entry is the one the trusted proxy appended; anything to its left
                    # was sent by the client and can say anything.
                    ip = self.headers["X-Forwarded-For"].split(",")[-1].strip()
                out = app.handle(method, self.path, self.headers, body, ip, raw if method == "POST" else b"")
                status, payload = out[0], out[1]
                if len(out) > 2:
                    extra = out[2]
            except ApiError as e:
                status, payload, extra = e.status, e.body or {"detail": e.detail}, e.headers
            except Exception as e:
                print(f"[error] {method} {self.path}: {e!r}", file=sys.stderr, flush=True)
                status, payload = 500, {"detail": "Internal Server Error"}
            extra = dict(extra)
            data = extra.pop("_raw", None)
            if data is None:
                data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if payload is not None and "Content-Type" not in extra:
                self.send_header("Content-Type", "application/json")
            for k, v in dict(SECURITY_HEADERS, **extra).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            if os.environ.get("ABS_ACCESS_LOG") == "1":
                super().log_message(fmt, *args)

    return Handler


def settings_from_env(env):
    return {
        # Referee costs are metered: each model call's real cost, plus margin. max_cost_per_run caps what
        # one call can be charged and sizes the hold; runners that can't meter are charged a flat rate.
        "max_cost_per_run": int(env.get("ABS_MAX_COST_PER_RUN", "20000")),          # $0.02
        "unmetered_cost_per_run": int(env.get("ABS_UNMETERED_COST_PER_RUN", "2000")),  # $0.002
        "margin_bps": int(env.get("ABS_COMPUTE_MARGIN_BPS", "2000")),                # 20% on compute
        "take_bps": int(env.get("ABS_TAKE_BPS", "1000")),              # platform share of each award
        "trials": int(env.get("ABS_EVAL_TRIALS", "1")),
        "visible_fraction": float(env.get("ABS_VISIBLE_FRACTION", "0.5")),
        "max_failures": int(env.get("ABS_MAX_FAILURES", "100")),
        "max_guards": int(env.get("ABS_MAX_GUARDS", "50")),
        "warranty_sample": int(env.get("ABS_WARRANTY_SAMPLE", "20")),
        "guard_every": int(env.get("ABS_GUARD_EVERY", "10")),
        "max_field_bytes": int(env.get("ABS_MAX_FIELD_BYTES", "65536")),
        "rate_per_min": float(env.get("ABS_RATE_PER_MIN", "1200")),
        "signups_per_hour": float(env.get("ABS_SIGNUPS_PER_HOUR", "20")),
        "config_max_age": int(env.get("ABS_CONFIG_MAX_AGE", "30")),
        "runner_name": env.get("ABS_RUNNER", "none"),
    }


def build(db_path, env=None, runner=None, clock=None):
    env = dict(os.environ if env is None else env)
    settings = settings_from_env(env)
    if not (0 < settings["visible_fraction"] < 1) or settings["trials"] < 1 or settings["trials"] % 2 == 0:
        raise SystemExit("FATAL: need 0 < ABS_VISIBLE_FRACTION < 1 and an odd ABS_EVAL_TRIALS >= 1")
    store = Store(db_path, clock or Clock())
    shop = Shop(store, runner, settings)
    return App(shop, Payments(store, env), env.get("ABS_ADMIN_KEY") or None, settings)


def main():
    env = os.environ
    if env.get("ABS_ENV") == "production":
        if not env.get("ABS_ADMIN_KEY"):
            sys.exit("FATAL: ABS_ENV=production requires ABS_ADMIN_KEY")
    app = build(env.get("ABS_DB", "data/autobodyshop.db"), runner=runners.from_env())
    tick = float(env.get("ABS_TICK_S", "5"))

    def ticker():
        while True:
            time.sleep(tick)
            try:
                app.shop.tick()
            except Exception as e:
                print(f"[tick] {e!r}", file=sys.stderr, flush=True)

    threading.Thread(target=ticker, daemon=True, name="ticker").start()
    port = int(env.get("ABS_PORT", "8090"))
    server = ThreadingHTTPServer((env.get("ABS_HOST", "0.0.0.0"), port),
                                 make_handler(app, env.get("ABS_TRUST_PROXY") == "1"))
    print(f"Auto Body Shop on :{port} (referee: {app.s['runner_name']}, real money: {app.payments.real_money})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
