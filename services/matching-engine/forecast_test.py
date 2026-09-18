#!/usr/bin/env python3
"""The forecast feed, the operator dashboard, and running with no liquidity.

    cargo build --release && python3 forecast_test.py

Three things are pinned here, all of them commercial rather than merely technical:

  * The feed is sellable without selling anybody out. It carries aggregates only — no agent id,
    no individual position, no balance — at every tier, including the paid one. That is a
    property of what the endpoint constructs, not a promise in a policy document.
  * The paid tier is actually differentiated: the public tier is delayed and unweighted, the
    subscriber tier is live and calibration-weighted. If the free tier were as good, nobody
    would pay for the other one.
  * The venue runs with the order book switched off. Nothing an agent needs — opening a market,
    betting, settling, being paid — requires a counterparty or a market maker's capital.
"""
import base64
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HOST, PORT = "127.0.0.1", 8097
BIN = "./target/release/ikenga-matching-engine"
OWNER_KEY = "dd" * 32
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

failures = []


def check(label, ok, extra=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if extra:
            print(f"       got: {extra}")
        failures.append(label)


def generate_keypair():
    with tempfile.NamedTemporaryFile(suffix=".pem") as f:
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", f.name],
                       check=True, capture_output=True)
        priv = subprocess.run(["openssl", "pkey", "-in", f.name, "-outform", "DER"],
                              check=True, capture_output=True).stdout
        pub = subprocess.run(["openssl", "pkey", "-in", f.name, "-pubout", "-outform", "DER"],
                             check=True, capture_output=True).stdout
    return priv[16:].hex(), pub[12:].hex()


def _pem(seed_hex):
    b64 = base64.b64encode(PKCS8_PREFIX + bytes.fromhex(seed_hex)).decode()
    return ("-----BEGIN PRIVATE KEY-----\n" + b64 + "\n-----END PRIVATE KEY-----\n").encode()


def sign(seed_hex, method, path, ts, nonce, body=b""):
    payload = method.encode() + path.encode() + ts.encode() + nonce.encode() + body
    with tempfile.NamedTemporaryFile() as k, tempfile.NamedTemporaryFile() as m, tempfile.NamedTemporaryFile() as s:
        k.write(_pem(seed_hex)); k.flush()
        m.write(payload); m.flush()
        subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-inkey", k.name, "-rawin", "-in", m.name, "-out", s.name],
            check=True, capture_output=True,
        )
        return s.read().hex()


def call(method, path, seed=None, agent_id=None, body_obj=None, owner=False, _retries=6):
    """Retries on 429. A freshly registered agent sits in the New trust band at 10 req/s, and
    this test bursts well past that during setup — which is the limiter doing its job, not a
    bug. Real integrators will meet the same ceiling on their first minute."""
    for attempt in range(_retries):
        status, body = _call_once(method, path, seed, agent_id, body_obj, owner)
        if status != 429:
            return status, body
        time.sleep(0.35)
    return status, body


def _call_once(method, path, seed=None, agent_id=None, body_obj=None, owner=False):
    raw = json.dumps(body_obj).encode() if body_obj is not None else b""
    h = {}
    if raw:
        h["Content-Type"] = "application/json"
    if owner:
        h["X-Owner-Key"] = OWNER_KEY
    if seed:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nonce = f"mkt-{time.time_ns()}"
        h.update({
            "X-Agent-ID": agent_id or "registration",
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sign(seed, method, path, ts, nonce, raw),
        })
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request(method, path, body=raw, headers=h)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except Exception:
        return resp.status, data


def register():
    seed, pub = generate_keypair()
    status, reg = call("POST", "/v1/agents", seed=seed, body_obj={"pubkey_hex": pub})
    assert status == 201, f"registration failed: {status} {reg}"
    return seed, reg["agent_id"], reg


def points(seed, agent_id):
    status, acct = call("GET", "/v1/account", seed=seed, agent_id=agent_id)
    assert status == 200 and isinstance(acct, dict) and "balances" in acct, \
        f"account lookup failed: {status} {acct}"
    return next((b["balance"] for b in acct["balances"] if b["asset"] == "PTS"), 0.0)


def open_market(question, closes_in_ms=1_000, outcomes=None, dispute_window_ms=0, resolution=None,
                asset=None, observed_offset_ms=1):
    now = int(time.time() * 1000)
    body = {
        "question": question,
        "closes_at_ms": now + closes_in_ms,
        "observed_at_ms": now + closes_in_ms + observed_offset_ms,
        "dispute_window_ms": dispute_window_ms,
        "resolution": resolution or {
            "kind": "operator_declared",
            "criteria": "as reported by the named source at the observation time",
            "source": "example.test/results",
        },
    }
    if outcomes:
        body["outcomes"] = outcomes
    if asset:
        body["asset"] = asset
    status, m = call("POST", "/v1/markets", body_obj=body, owner=True)
    assert status == 201, f"market creation failed: {status} {m}"
    return m["market_id"], m


def settle_now(market_id, outcome=None, void=False, evidence="observed at the source"):
    """Propose then finalize. Markets in this test use a zero dispute window unless stated."""
    body = {"evidence": evidence}
    if void:
        body["void"] = True
    else:
        body["outcome"] = outcome
    st, prop = call("POST", f"/v1/markets/{market_id}/propose", body_obj=body, owner=True)
    assert st == 200, f"propose failed: {st} {prop}"
    return call("POST", f"/v1/markets/{market_id}/finalize", body_obj={}, owner=True)


def wait_for_observation(market):
    """Sleep until the market's committed observation time has passed."""
    remaining = market["observed_at_ms"] / 1000 - time.time()
    if remaining > 0:
        time.sleep(remaining + 0.05)


def start_server():
    env = {
        **os.environ,
        "IKENGA_BIND": f"{HOST}:{PORT}",
        "IKENGA_OWNER_KEY": OWNER_KEY,
        "IKENGA_WAL_PATH": WAL,
        "IKENGA_REGISTRATIONS_PER_HOUR": "500",
    }
    proc = subprocess.Popen(BIN, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if "listening on" in line:
            break
    return proc, lines


def stop(proc):
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)




def raw_get(path, headers=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


WAL = "/tmp/ikenga-forecast-test.wal"
if os.path.exists(WAL):
    os.remove(WAL)
# The track record is deliberately scored on money-backed markets only, so this suite needs a
# deployment where a redeemable asset exists. See forecast::track_record.
os.environ["IKENGA_REAL_MONEY"] = "1"


def deposit(agent_id, amount):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("POST", f"/v1/deposits/{agent_id}/USDC", body=str(amount).encode(),
                 headers={"X-Owner-Key": OWNER_KEY})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp.status

proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to start:\n" + "\n".join(lines))
    sys.exit(1)

# ---------------------------------------------------------------------------------------------
print("=== 1. the venue runs with no order book and no liquidity ===")
status, body = call("POST", "/v1/orders", body_obj={"symbol": "BTC-USD", "side": "buy", "qty": 1})
check("the order book is off by default", status == 404, (status, body))
check("and it says to use the markets instead",
      isinstance(body, dict) and body.get("code") == "ORDERBOOK_DISABLED", body)
status, _ = call("GET", "/v1/quote?symbol=BTC-USD")
check("quoting a book that isn't there is refused too", status == 404, status)

seed_a, agent_a, _ = register()
seed_b, agent_b, _ = register()
seed_c, agent_c, _ = register()

mkt, detail = open_market("Will the referenced index close above its opening level?",
                          closes_in_ms=1_200)
status, _ = call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_a, agent_id=agent_a,
                 body_obj={"outcome": 0, "amount": 300})
check("a bet is placed with no market maker on the other side", status == 200, status)
call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_b, agent_id=agent_b,
     body_obj={"outcome": 1, "amount": 100})
call("POST", f"/v1/markets/{mkt}/stakes", seed=seed_c, agent_id=agent_c,
     body_obj={"outcome": 1, "amount": 100})

# ---------------------------------------------------------------------------------------------
print()
print("=== 2. the feed sells the signal, not the participants ===")
status, feed = call("GET", "/v1/feed")
check("the feed is readable without an account", status == 200, status)
check("it names its tier", feed.get("tier") == "public", feed.get("tier"))
check("the public tier is delayed", feed.get("delay_ms") == 900_000, feed.get("delay_ms"))

blob = json.dumps(feed)
check("no agent id appears anywhere in the feed",
      agent_a not in blob and agent_b not in blob and agent_c not in blob, blob[:400])
for leak in ("agent", "stake_id", "address", "balance", "pubkey"):
    check(f"the feed exposes no '{leak}' field", f'"{leak}' not in blob)

status, sub = call("POST", "/v1/feed/subscribers", body_obj={"label": "quantfund.test"}, owner=True)
check("the owner can issue a subscriber key", status == 201, (status, sub))
key = sub.get("feed_key", "")
check("the key is namespaced", key.startswith("feed_"), key)
check("it says which header to send it in", sub.get("header") == "X-Feed-Key", sub)

status, body = call("POST", "/v1/feed/subscribers", body_obj={"label": "nope"})
check("issuing keys is owner-only", status == 403, (status, body))

status, raw = raw_get("/v1/feed", {"X-Feed-Key": key})
paid = json.loads(raw)
check("a subscriber key upgrades the tier", paid.get("tier") == "subscriber", paid.get("tier"))
check("and removes the delay", paid.get("delay_ms") == 0, paid.get("delay_ms"))
check("the live tier actually sees the open market",
      any(m["market_id"] == mkt for m in paid.get("markets", [])),
      [m["market_id"] for m in paid.get("markets", [])])

pub_market = next((m for m in feed.get("markets", []) if m["market_id"] == mkt), None)
check("the delay withholds the numbers, not just the row — money placed inside the delay "
      "window is invisible to the free tier",
      pub_market is None or pub_market["total_pool"] == 0.0,
      pub_market.get("total_pool") if pub_market else "row withheld")
paid_market = next((m for m in paid.get("markets", []) if m["market_id"] == mkt), None)
check("the paid tier carries the calibration-weighted consensus",
      paid_market is not None and "weighted_consensus" in paid_market["outcomes"][0],
      paid_market["outcomes"][0] if paid_market else None)
check("the free tier does not",
      pub_market is None or "weighted_consensus" not in pub_market["outcomes"][0],
      pub_market["outcomes"][0] if pub_market else "market not in public feed at all")
if paid_market:
    consensus = [o["consensus"] for o in paid_market["outcomes"]]
    check("consensus is the pool split, not a guess",
          abs(consensus[0] - 0.6) < 1e-6 and abs(consensus[1] - 0.4) < 1e-6, consensus)
    check("the pool size is published", abs(paid_market["total_pool"] - 500.0) < 1e-9,
          paid_market.get("total_pool"))
    check("participant count is a count, not a list",
          paid_market["participants"] == 3, paid_market.get("participants"))

status, body = call("POST", "/v1/feed/subscribers", body_obj={"revoke": key}, owner=True)
check("a key can be revoked", status == 200 and body.get("revoked") is True, (status, body))
status, raw = raw_get("/v1/feed", {"X-Feed-Key": key})
check("and the revoked key falls back to the free tier",
      json.loads(raw).get("tier") == "public", raw[:200])

# ---------------------------------------------------------------------------------------------
print()
print("=== 3. the dashboard ===")
status, raw = raw_get("/dashboard")
check("the console page is served", status == 200, status)
check("it is a self-contained page", b"<!doctype html>" in raw.lower(), raw[:80])
check("it pulls in nothing from another origin",
      b"http://" not in raw and b"https://" not in raw, "external reference in page")
check("the page itself carries no data", b"X-Owner-Key" in raw and b"/v1/dashboard" in raw)

status, body = call("GET", "/v1/dashboard")
check("dashboard data needs the owner key", status == 403, (status, body))

status, dash = call("GET", "/v1/dashboard", owner=True)
check("the owner can read it", status == 200, (status, dash))
check("it reports the settlement asset", dash.get("settlement_asset") == "USDC",
      dash.get("settlement_asset"))
check("it reports the rake", dash["revenue"]["rake_bps"] == 100.0, dash["revenue"])
check("it counts the open market", dash["markets"]["open"] >= 1, dash["markets"])
check("it counts the money staked", abs(dash["markets"]["total_staked"] - 500.0) < 1e-9,
      dash["markets"])
check("it counts distinct participants", dash["markets"]["unique_participants"] == 3,
      dash["markets"])
check("it shows reserves fully backed", dash["reserves"]["fully_backed"] is True,
      dash["reserves"])
check("it shows the order book is off", dash["orderbook_enabled"] is False, dash)
check("nothing needs attention yet", dash["needs_attention"] == [], dash["needs_attention"])

wait_for_observation(detail)
status, dash = call("GET", "/v1/dashboard", owner=True)
needs = {n["market_id"]: n for n in dash["needs_attention"]}
check("a closed market is surfaced as needing action", mkt in needs, dash["needs_attention"])
check("with a reason a human can act on",
      "resolve" in needs.get(mkt, {}).get("reason", ""), needs.get(mkt))

st, out = settle_now(mkt, outcome=0)
check("settling works with no counterparty", st == 200, (st, out))
# Losing pool 200, gross rake 1% = 2. A quarter of that is handed back to early stakers, so the
# house books 1.5. Both numbers are published, and they must reconcile against each other.
check("the rake came only from the losing pool",
      abs(out["gross_rake"] - 2.0) < 1e-9 and abs(out["rake"] - 1.5) < 1e-9, out)
check("and the published rake, rebate and gross rake reconcile",
      abs(out["gross_rake"] - out["rake"] - out["early_liquidity_rebate"]) < 1e-9, out)
check("payouts plus what the house kept equal the pool exactly",
      abs(sum(p["payout"] for p in out["payouts"]) + out["rake"] - out["total_pool"]) < 1e-6,
      (sum(p["payout"] for p in out["payouts"]), out["rake"], out["total_pool"]))

status, dash = call("GET", "/v1/dashboard", owner=True)
earned = {r["asset"]: r["amount"] for r in dash["revenue"]["by_asset"]}
check("the dashboard shows the revenue the house actually kept",
      abs(earned.get("PTS", 0) - 1.5) < 1e-9, earned)
check("and the market is off the attention list",
      mkt not in {n["market_id"] for n in dash["needs_attention"]}, dash["needs_attention"])
check("a points market is kept out of the scored record",
      dash["track_record"]["resolved_markets"] == 0
      and dash["track_record"]["promotional_excluded"] >= 1,
      dash["track_record"])
check("the subscriber count survives a revoke", dash["feed"]["subscribers"] == 0, dash["feed"])

# Now the same thing with money behind it, which is what the feed actually sells.
for a in (agent_a, agent_b, agent_c):
    deposit(a, 1000)
paid_mkt, paid_detail = open_market("Will the money-backed reference settle above its open?",
                                    closes_in_ms=900, asset="USDC")
call("POST", f"/v1/markets/{paid_mkt}/stakes", seed=seed_a, agent_id=agent_a,
     body_obj={"outcome": 0, "amount": 300})
call("POST", f"/v1/markets/{paid_mkt}/stakes", seed=seed_b, agent_id=agent_b,
     body_obj={"outcome": 1, "amount": 200})
call("POST", f"/v1/markets/{paid_mkt}/stakes", seed=seed_c, agent_id=agent_c,
     body_obj={"outcome": 1, "amount": 100})
wait_for_observation(paid_detail)
settle_now(paid_mkt, outcome=0)

status, dash = call("GET", "/v1/dashboard", owner=True)
check("a money-backed market IS scored",
      dash["track_record"]["resolved_markets"] >= 1, dash["track_record"])
check("a Brier score is published", dash["track_record"]["mean_brier"] is not None,
      dash["track_record"])
earned = {r["asset"]: r["amount"] for r in dash["revenue"]["by_asset"]}
check("and the rake was collected in USDC, net of the early-liquidity rebate",
      abs(earned.get("USDC", 0) - 2.25) < 1e-9, earned)

time.sleep(6)  # the feed is cached; wait it out rather than reading a stale body
status, feed = call("GET", "/v1/feed")
check("the free feed publishes the track record too",
      feed["track_record"]["resolved_markets"] >= 1, feed["track_record"])
check("and states its own privacy property", "agent identities" in feed.get("privacy", ""),
      feed.get("privacy"))

# ---------------------------------------------------------------------------------------------
print()
print("=== 4. the order book comes back when it is asked for ===")
stop(proc)
os.environ["IKENGA_ENABLE_ORDERBOOK"] = "1"
proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to restart:\n" + "\n".join(lines))
    sys.exit(1)
status, _ = call("GET", "/v1/quote?symbol=BTC-USD")
check("quoting works when the book is enabled", status == 200, status)

# Subscriber keys are billing state. A restart that forgets them silently demotes a paying
# customer to the delayed free tier with no error anywhere.
status, sub2 = call("POST", "/v1/feed/subscribers", body_obj={"label": "persist.test"},
                    owner=True)
key2 = sub2["feed_key"]
stop(proc)
proc, lines = start_server()
if not any("listening on" in l for l in lines):
    print("server failed to restart:\n" + "\n".join(lines))
    sys.exit(1)
status, raw = raw_get("/v1/feed", {"X-Feed-Key": key2})
check("a paid subscriber key survives a restart",
      json.loads(raw).get("tier") == "subscriber", raw[:160])
status, body = call("POST", "/v1/feed/subscribers", body_obj={"revoke": key2}, owner=True)
check("and a revocation survives one too", status == 200 and body.get("revoked") is True,
      (status, body))
stop(proc)
proc, lines = start_server()
status, raw = raw_get("/v1/feed", {"X-Feed-Key": key2})
check("a revoked key does not come back from the dead",
      json.loads(raw).get("tier") == "public", raw[:160])
status, dash = call("GET", "/v1/dashboard", owner=True)
check("and the dashboard says so", dash["orderbook_enabled"] is True, dash)
check("the markets survived the restart", dash["markets"]["total"] >= 1, dash["markets"])
check("so did the revenue",
      abs({r["asset"]: r["amount"] for r in dash["revenue"]["by_asset"]}.get("PTS", 0) - 1.5) < 1e-9,
      dash["revenue"])

stop(proc)
if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL FORECAST + DASHBOARD CHECKS PASSED")
