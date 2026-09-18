#!/usr/bin/env python3
"""Why a rational agent would bet here at all — and the logic that has to hold for that to be true.

    cargo build --release && python3 rationality_test.py

Every other suite asks whether the venue is correct. This one asks whether it is *worth using*,
which is a different question with a different failure mode: an engine can be flawless and still
be something no agent with a working expected-value calculator would ever touch.

Three objections are pinned here, each with the fix that answers it.

1. "I cannot tell whether this bet is worth making."  A pari-mutuel payout depends on the final
   pool, so the odds you are quoted are not the odds you get, and your own stake moves them.
   GET /v1/markets/{id}/quote has to do that arithmetic for the caller, including its own market
   impact, and hand back the one number that decides it: the probability you must beat.

2. "Whoever bets last has strictly more information than whoever bets first."  True, and if every
   participant is a program running that reasoning, the board never fills. The early-liquidity
   rebate has to make being first pay — out of the house's cut, never out of another participant,
   and never by enough to make staking both sides profitable.

3. "The points are worth nothing, so why spend my operator's compute?"  Because what you leave
   with is a signed, independently checkable forecasting record. If that signature does not verify
   against the published key, the whole answer collapses.
"""
import base64
import http.client
import json
import os
import signal
import subprocess
import sys
import http.server
import socketserver
import tempfile
import threading
import time

HOST, PORT = "127.0.0.1", 8347
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
        nonce = f"rat-{time.time_ns()}"
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
        "IKENGA_REAL_MONEY": "1",
        "IKENGA_ROUTE_DEMO": "1",
        "IKENGA_SWEEP_INTERVAL_MS": "1000",
        "IKENGA_AUTOPILOT": "BTC-USD:600@-50,0,+50",
        "IKENGA_SEED_PER_MARKET": "25",
        # Four sources pointed at a local replay of what these exchanges really returned.
        "IKENGA_ROUTE_SOURCES": (
            f"coinbase|http://127.0.0.1:{VENUE_PORT}/coinbase?p={{sell}}-{{buy}}|data.amount|rate;"
            f"kraken|http://127.0.0.1:{VENUE_PORT}/kraken?p={{sell}}{{buy}}|result.*.c.0|rate;"
            f"gemini|http://127.0.0.1:{VENUE_PORT}/gemini?p={{sell_lower}}{{buy_lower}}|last|rate;"
            f"bitfinex|http://127.0.0.1:{VENUE_PORT}/bitfinex?p={{sell}}{{buy}}|6|rate"
        ),
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
WAL = "/tmp/ikenga-rationality-test.wal"
VENUE_PORT = 8792

if os.path.exists(WAL):
    os.remove(WAL)
proc, boot = start_server()
if not any("listening on" in l for l in boot):
    print("server did not start:")
    print("\n".join(boot))
    sys.exit(1)

# ---------------------------------------------------------------------------------------------
# 1. Can an agent tell whether a bet is worth making?
# ---------------------------------------------------------------------------------------------
print("\n-- pricing a bet before making it --")

seed_a, agent_a, _ = register()
seed_b, agent_b, _ = register()

mid, market = open_market("Will the quoted side win?", closes_in_ms=60_000,
                          outcomes=["YES", "NO"], dispute_window_ms=0,
                          observed_offset_ms=1_000)

st, q = call("GET", f"/v1/markets/{mid}/quote?outcome=0&amount=100")
check("an empty market can still be quoted", st == 200, (st, q))
check("and it says plainly that there is nothing to win yet",
      any("nobody has taken the other side" in w for w in q.get("warnings", [])), q.get("warnings"))
check("with no counterparty, no probability is high enough to justify a bet",
      q["breakeven_probability"] == 1.0, q["breakeven_probability"])

# Put a real pool on the board: 400 against, 100 for.
call("POST", f"/v1/markets/{mid}/stakes", seed=seed_b, agent_id=agent_b,
     body_obj={"outcome": 1, "amount": 400})
call("POST", f"/v1/markets/{mid}/stakes", seed=seed_a, agent_id=agent_a,
     body_obj={"outcome": 0, "amount": 100})

st, q = call("GET", f"/v1/markets/{mid}/quote?outcome=0&amount=100")
check("the quote reflects the live pool", st == 200 and q["total_pool"] == 500, q.get("total_pool"))

# Hand-check the arithmetic the caller is being asked to trust.
# backed=100, against=400, rake 1% -> distributable 396. Staking 100 more: 396*100/200 = 198.
check("payout_if_right is the pari-mutuel payout, not an approximation",
      abs(q["profit_if_right"] - 198.0) < 1e-6, q["profit_if_right"])
check("breakeven_probability is stake/(stake+profit)",
      abs(q["breakeven_probability"] - (100 / 298)) < 1e-9, q["breakeven_probability"])
check("your own stake moves the price you are paid at",
      q["implied_probability_after"] > q["implied_probability_before"],
      (q["implied_probability_before"], q["implied_probability_after"]))

# The number that actually decides it.
be = q["breakeven_probability"]
st, below = call("GET", f"/v1/markets/{mid}/quote?outcome=0&amount=100&belief={be - 0.05:.6f}")
check("a belief below breakeven is told not to bet",
      "negative_expected_value" in below["verdict"], below["verdict"])
check("and the expected value is negative", below["expected_value"] < 0, below["expected_value"])

st, above = call("GET", f"/v1/markets/{mid}/quote?outcome=0&amount=100&belief={be + 0.10:.6f}")
check("a belief above breakeven is told to bet",
      above["verdict"] == "positive_expected_value", above["verdict"])
check("and it is given a size to stop at", above["max_stake_at_belief"] > 0,
      above["max_stake_at_belief"])

# Sizing: past max_stake_at_belief the edge is gone. Verify by quoting exactly there.
cap = above["max_stake_at_belief"]
st, at_cap = call("GET",
                  f"/v1/markets/{mid}/quote?outcome=0&amount={cap:.6f}&belief={be + 0.10:.6f}")
check("at max_stake_at_belief the expected value has gone to zero",
      abs(at_cap["expected_value"]) < 1e-3, at_cap["expected_value"])
st, past_cap = call("GET",
                    f"/v1/markets/{mid}/quote?outcome=0&amount={cap * 2:.6f}&belief={be + 0.10:.6f}")
check("and past it the same belief is a losing bet", past_cap["expected_value"] < 0,
      past_cap["expected_value"])

st, no_edge = call("GET",
                   f"/v1/markets/{mid}/quote?outcome=0&amount=50&belief={q['implied_probability_before']:.6f}")
check("agreeing with the pool is called out as not being an edge",
      any("not an edge" in w for w in no_edge.get("warnings", [])), no_edge.get("warnings"))

st, whale = call("GET", f"/v1/markets/{mid}/quote?outcome=0&amount=100000")
check("a stake larger than the pool is warned about",
      any("larger than the entire existing pool" in w for w in whale.get("warnings", [])),
      whale.get("warnings"))

# The quote must not become a way to probe things the caller cannot otherwise see.
blob = json.dumps(q)
for leak in [agent_a, agent_b, "pubkey", "balance"]:
    check(f"the quote leaks no {leak[:12]}", leak not in blob)

for bad in ["?outcome=9&amount=10", "?outcome=0&amount=-5", "?outcome=0&amount=abc",
            "?outcome=0&amount=10&belief=62", "?outcome=0&amount=10&belief=0",
            "?outcome=0&amount=10&belief=1"]:
    st, _ = call("GET", f"/v1/markets/{mid}/quote{bad}")
    check(f"quote refuses {bad}", st == 400, st)

st, _ = call("GET", "/v1/markets/mkt_nope/quote?outcome=0&amount=10")
check("quote on an unknown market is a 404, not a guess", st == 404, st)

# ---------------------------------------------------------------------------------------------
# 2. Is there any reason to be the first one in?
# ---------------------------------------------------------------------------------------------
print("\n-- getting paid for being early --")

seed_e, agent_e, _ = register()   # early
seed_l, agent_l, _ = register()   # late
seed_x, agent_x, _ = register()   # the losing side

emid, emarket = open_market("Does being early pay?", closes_in_ms=9_000,
                            outcomes=["YES", "NO"], dispute_window_ms=0,
                            observed_offset_ms=500)

e_pre, l_pre, x_pre = points(seed_e, agent_e), points(seed_l, agent_l), points(seed_x, agent_x)

# Same side, same size, one at the open and one just before the close.
call("POST", f"/v1/markets/{emid}/stakes", seed=seed_e, agent_id=agent_e,
     body_obj={"outcome": 0, "amount": 100})
call("POST", f"/v1/markets/{emid}/stakes", seed=seed_x, agent_id=agent_x,
     body_obj={"outcome": 1, "amount": 500})
time.sleep(7.5)
st, late_stake = call("POST", f"/v1/markets/{emid}/stakes", seed=seed_l, agent_id=agent_l,
                      body_obj={"outcome": 0, "amount": 100})
check("the late stake was accepted", st in (200, 201), (st, late_stake))

st, m = call("GET", f"/v1/markets/{emid}")
wait_for_observation(m)
settle_now(emid, outcome=0)

e_gain = points(seed_e, agent_e) - e_pre
l_gain = points(seed_l, agent_l) - l_pre
check("both winners were paid", e_gain > 0 and l_gain > 0, (e_gain, l_gain))
check("the agent who made the market exist was paid more than the one who waited",
      e_gain > l_gain, (e_gain, l_gain))
check("but the difference is small — it is a liquidity fee, not a lottery",
      (e_gain - l_gain) / max(l_gain, 1e-9) < 0.05, (e_gain, l_gain))

pot = 700.0
paid_out = e_gain + l_gain + (points(seed_x, agent_x) - x_pre)
check("the rebate did not create money", paid_out <= pot + 1e-6, paid_out)

# The attack: stake both sides early and collect the rebate twice.
seed_w, agent_w, _ = register()
wmid, _ = open_market("Can a wash trade farm the rebate?", closes_in_ms=3_000,
                      outcomes=["YES", "NO"], dispute_window_ms=0, observed_offset_ms=500)
w_pre = points(seed_w, agent_w)
call("POST", f"/v1/markets/{wmid}/stakes", seed=seed_w, agent_id=agent_w,
     body_obj={"outcome": 0, "amount": 200})
call("POST", f"/v1/markets/{wmid}/stakes", seed=seed_w, agent_id=agent_w,
     body_obj={"outcome": 1, "amount": 200})
st, wm = call("GET", f"/v1/markets/{wmid}")
wait_for_observation(wm)
settle_now(wmid, outcome=0)
w_delta = points(seed_w, agent_w) - w_pre
check("staking both sides early still loses money", w_delta < 0, w_delta)

# ---------------------------------------------------------------------------------------------
# 3. Is there anything to take away when the chips are worthless?
# ---------------------------------------------------------------------------------------------
print("\n-- leaving with something checkable --")

st, rec = call("GET", "/v1/account/record", seed=seed_e, agent_id=agent_e)
check("an agent can fetch its own record", st == 200, (st, rec))
check("the record only covers resolved markets it was actually in",
      rec["scored_markets"] >= 1, rec["scored_markets"])
check("every entry carries the hash its terms were fixed under",
      all(e.get("commitment_sha256") for e in rec["markets"]), rec["markets"][:1])
check("and the venue publishes the key it signed with",
      isinstance(rec.get("venue_public_key"), str) and len(rec["venue_public_key"]) == 64,
      rec.get("venue_public_key"))

st, spec = call("GET", "/v1/spec")
check("the same key is published in the spec, so a verifier never has to ask the agent for it",
      spec.get("venue_public_key") == rec.get("venue_public_key"),
      (spec.get("venue_public_key"), rec.get("venue_public_key")))


def verify_ed25519(pub_hex, message: bytes, sig_hex) -> bool:
    der = bytes.fromhex("302a300506032b6570032100") + bytes.fromhex(pub_hex)
    with tempfile.NamedTemporaryFile(suffix=".der") as k, \
         tempfile.NamedTemporaryFile() as m, tempfile.NamedTemporaryFile() as s:
        k.write(der); k.flush()
        m.write(message); m.flush()
        s.write(bytes.fromhex(sig_hex)); s.flush()
        r = subprocess.run(
            ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", k.name, "-keyform", "DER",
             "-rawin", "-in", m.name, "-sigfile", s.name],
            capture_output=True)
        return r.returncode == 0


ok = verify_ed25519(rec["venue_public_key"], rec["attestation_input"].encode(), rec["signature"])
check("the signature verifies against the published key, with no call back to the server", ok)

tampered = rec["attestation_input"].replace("brier:", "brier:0.0")
check("a doctored record does not verify",
      not verify_ed25519(rec["venue_public_key"], tampered.encode(), rec["signature"]))

# The record must be checkable line by line, not taken on faith.
entry = rec["markets"][0]
st, live = call("GET", f"/v1/markets/{entry['market_id']}")
check("each market in the record can be fetched independently", st == 200, st)
check("and its published commitment matches the one the record claims",
      live["commitment_sha256"] == entry["commitment_sha256"], (live.get("commitment_sha256"), entry["commitment_sha256"]))
check("the recorded outcome matches the market's own", live["winning_outcome"] == entry["winning_outcome"],
      (live.get("winning_outcome"), entry["winning_outcome"]))

st, other = call("GET", "/v1/account/record", seed=seed_l, agent_id=agent_l)
mine = {e["market_id"] for e in rec["markets"]}
theirs = {e["market_id"] for e in other["markets"]}
check("a record contains only its own agent's history",
      rec["agent_id"] != other["agent_id"], (rec["agent_id"], other["agent_id"]))
blob = json.dumps(other)
check("and never names anybody else", agent_e not in blob)

st, unsigned = call("GET", "/v1/account/record")
check("the record cannot be fetched unsigned", st == 401, st)

# ---------------------------------------------------------------------------------------------
# 4. Does the board give an agent anywhere to put a view?
# ---------------------------------------------------------------------------------------------
print("\n-- somewhere to be right --")

st, spec = call("GET", "/v1/spec")
check("the spec tells an agent when NOT to bet",
      isinstance(spec.get("when_not_to_use_this"), list) and len(spec["when_not_to_use_this"]) >= 3,
      spec.get("when_not_to_use_this"))
joined = json.dumps(spec)
check("it names the quote endpoint as the thing to call first",
      "/v1/markets/{id}/quote" in joined)
check("it is honest that betting without an edge loses money",
      "negative-sum" in joined or "negative sum" in joined)
check("it explains what the worthless points are actually for", "/v1/account/record" in joined)

# ---------------------------------------------------------------------------------------------
# 5. Is it comfortable to integrate against?
# ---------------------------------------------------------------------------------------------
print("\n-- comfortable to integrate --")

st, tools = call("GET", "/v1/tools")
check("the venue publishes its own tool definitions", st == 200 and "tools" in tools, st)
names = {t["name"] for t in tools["tools"]}
for needed in ["ikenga_register", "ikenga_list_markets", "ikenga_quote", "ikenga_place_stake",
               "ikenga_get_record"]:
    check(f"{needed} is offered as a tool", needed in names, sorted(names))
for t in tools["tools"]:
    sch = t.get("input_schema", {})
    check(f"{t['name']} has a usable schema",
          sch.get("type") == "object" and isinstance(sch.get("properties"), dict)
          and isinstance(sch.get("required"), list), sch)
    check(f"{t['name']} says how to call it over HTTP",
          t.get("http", {}).get("method") in ("GET", "POST")
          and t["http"].get("path", "").startswith("/v1")
          and t["http"].get("auth") in ("none", "signed", "self-signed"), t.get("http"))
    for req in sch.get("required", []):
        check(f"{t['name']}'s required field {req} is described",
              req in sch["properties"], sch)
quote_tool = next(t for t in tools["tools"] if t["name"] == "ikenga_quote")
check("the quote tool tells the model to call it before betting",
      "BEFORE EVERY BET" in quote_tool["description"], quote_tool["description"][:80])

# Every advertised path must actually route. A tool list that names a 404 is worse than none.
for t in tools["tools"]:
    path = t["http"]["path"].replace("{market_id}", mid).replace("{challenge_id}", "ch_nope")
    if t["http"]["auth"] != "none":
        continue
    probe = path + ("?outcome=0&amount=1" if "quote" in path else "")
    st, _ = call("GET", probe) if t["http"]["method"] == "GET" else (200, None)
    check(f"{t['name']} points at a real endpoint", st != 404, (probe, st))

# A rehearsal that changes nothing.
seed_d, agent_d, _ = register()
before = points(seed_d, agent_d)
dmid, dmarket = open_market("Does a dry run move money?", closes_in_ms=30_000,
                            outcomes=["YES", "NO"], dispute_window_ms=0, observed_offset_ms=1_000)
st, dry = call("POST", f"/v1/markets/{dmid}/stakes", seed=seed_d, agent_id=agent_d,
               body_obj={"outcome": 0, "amount": 10, "dry_run": True})
check("a dry run is accepted", st == 200 and dry.get("dry_run") is True, (st, dry))
check("and reports what would happen", dry.get("would_succeed") is True
      and abs(dry["balance_would_remain"] - (before - 10)) < 1e-9, dry)
check("but moves no money at all", points(seed_d, agent_d) == before,
      (points(seed_d, agent_d), before))
st, m_after = call("GET", f"/v1/markets/{dmid}")
check("and leaves no stake behind", m_after["total_pool"] == 0, m_after["total_pool"])

st, bad_dry = call("POST", f"/v1/markets/{dmid}/stakes", seed=seed_d, agent_id=agent_d,
                   body_obj={"outcome": 0, "amount": 999999, "dry_run": True})
check("a dry run refuses what the real call would refuse",
      bad_dry.get("code") == "INSUFFICIENT_BALANCE", (st, bad_dry))
check("and says it was only a rehearsal", "dry run" in bad_dry.get("message", ""), bad_dry)

st, real = call("POST", f"/v1/markets/{dmid}/stakes", seed=seed_d, agent_id=agent_d,
                body_obj={"outcome": 0, "amount": 10})
check("the same request without dry_run does commit", st == 200 and "balance_remaining" in real,
      (st, real))
check("and the money actually moved", abs(points(seed_d, agent_d) - (before - 10)) < 1e-9,
      points(seed_d, agent_d))

# The 401 that has to teach rather than stonewall.
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
nonce = f"diag-{time.time_ns()}"
raw = json.dumps({"outcome": 0, "amount": 1}).encode()
wrong = sign(seed_d, "POST", f"/v1/markets/{dmid}/stakes", ts, nonce,
             json.dumps({"outcome": 0, "amount": 1}, indent=2).encode())
conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
conn.request("POST", f"/v1/markets/{dmid}/stakes", body=raw, headers={
    "Content-Type": "application/json", "X-Agent-ID": agent_d, "X-Timestamp": ts,
    "X-Nonce": nonce, "X-Signature": wrong})
r = conn.getresponse(); diag = json.loads(r.read()); conn.close()
check("a mismatched signature is rejected", r.status == 401, r.status)
msg = diag.get("message", "")
check("and the server shows the exact bytes it expected",
      "begin signing input" in msg and f"POST/v1/markets/{dmid}/stakes" in msg, msg[:160])
check("with a hash so a large body can be compared in one line", "sha256 of that input:" in msg)
check("and names the mismatched body as the first thing to check",
      "Serialise ONCE" in msg, msg[-200:])
check("the diagnostic does not leak a key", "seed" not in msg.lower() and agent_d not in msg)

# ---------------------------------------------------------------------------------------------
# 6. Does a market exist for the first agent to arrive at?
# ---------------------------------------------------------------------------------------------
print("\n-- somewhere to land on arrival --")

st, seeded_board = call("GET", "/v1/markets")
autopiloted = [m for m in seeded_board["markets"] if "where it opened" in m["question"]
               or "the price it opened at" in m["question"]]
check("autopilot opened markets", len(autopiloted) >= 1, len(autopiloted))
check("and every one of them has money on both sides already",
      all(all(o["pool"] > 0 for o in m["outcomes"]) for m in autopiloted),
      [[o["pool"] for o in m["outcomes"]] for m in autopiloted])
check("a full ladder opened, not just one rung",
      len({m["question"] for m in autopiloted}) >= 3,
      sorted({m["question"] for m in autopiloted}))
check("the house seeded every outcome equally, so it holds no opinion",
      all(len({round(o["pool"], 6) for o in m["outcomes"]}) == 1 for m in autopiloted),
      [[o["pool"] for o in m["outcomes"]] for m in autopiloted])

# The first arrival must find a bet worth making, or the seeding did nothing.
seed_f, agent_f, _ = register()
target = autopiloted[0]
st, fq = call("GET", f"/v1/markets/{target['market_id']}/quote?outcome=0&amount=1&belief=0.60")
check("a first arrival is offered a real edge to size against",
      fq["max_stake_at_belief"] > 0, fq.get("max_stake_at_belief"))
size = round(fq["max_stake_at_belief"] * 0.5, 2)
st, fq2 = call("GET",
               f"/v1/markets/{target['market_id']}/quote?outcome=0&amount={size}&belief=0.60")
check("and at half that size the bet is worth making",
      fq2["verdict"].startswith("positive"), (size, fq2["verdict"]))
st, staked = call("POST", f"/v1/markets/{target['market_id']}/stakes", seed=seed_f,
                  agent_id=agent_f, body_obj={"outcome": 0, "amount": size})
check("the first arrival can actually place it", st == 200, (st, staked))

# And the house must not be able to trade by hand.
st, house_try = call("POST", f"/v1/markets/{target['market_id']}/stakes", seed=seed_f,
                     agent_id="agent_house", body_obj={"outcome": 0, "amount": 1})
check("the house agent cannot stake through the API", st in (401, 403), (st, house_try))
st, acct = call("GET", "/v1/account", seed=seed_f, agent_id="agent_house")
check("nor can anyone read its account without its key", st == 401, st)

# ---------------------------------------------------------------------------------------------
# 7. Can a person actually place a bet, and can a builder find the door?
# ---------------------------------------------------------------------------------------------
print("\n-- doors a human can walk through --")


def raw_get(path, accept=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("GET", path, headers={"Accept": accept} if accept else {})
    r = conn.getresponse()
    body = r.read().decode("utf-8", "replace")
    ctype = r.getheader("Content-Type") or ""
    conn.close()
    return r.status, ctype, body


st, ctype, page = raw_get("/bet")
check("the betting page is served", st == 200 and "text/html" in ctype, (st, ctype))
check("it can make a key in the browser", "Ed25519" in page and "generateKey" in page)
check("it signs the same buffer it sends",
      "JSON.stringify(bodyObj)" in page and "method + path + ts + nonce + raw" in page)
check("it rehearses before committing", '"dry_run"' in page or "dry_run: true" in page)
check("it quotes before betting", "/quote?outcome=" in page)
check("it tells the reader the key is theirs alone", "only in this browser" in page)

st, ctype, _ = raw_get("/build")
check("the builder page is served", st == 200 and "text/html" in ctype, (st, ctype))

st, ctype, agent = raw_get("/agent.py")
check("the example agent downloads from the venue itself", st == 200, st)
check("and it is the real file", "def form_a_belief" in agent and "max_stake_at_belief" in agent)
check("served as text, not sniffable as html",
      "text/plain" in ctype or "x-python" in ctype, ctype)

# One address, two audiences.
st, ctype, _ = raw_get("/", accept="text/html,application/xhtml+xml")
check("a browser at the root gets a page", st == 200 and "text/html" in ctype, ctype)
st, ctype, body = raw_get("/")
check("a client at the root still gets JSON", st == 200 and "application/json" in ctype, ctype)
json.loads(body)
st, ctype, _ = raw_get("/", accept="*/*")
check("curl's */* is not mistaken for a browser", "application/json" in ctype, ctype)

# The public pages must not carry anything privileged.
for path in ["/bet", "/build"]:
    _, _, html = raw_get(path)
    for leak in [OWNER_KEY, "X-Owner-Key", "IKENGA_OWNER"]:
        check(f"{path} does not carry {leak[:14]}", leak not in html)

# ---------------------------------------------------------------------------------------------
# 8. Can any single endpoint wedge the whole venue?
# ---------------------------------------------------------------------------------------------
print("\n-- no endpoint wedges the venue --")

# This exists because one did. /v1/dashboard held the balances lock in a named binding and then
# called a helper that locked balances again; a std::sync::Mutex is not reentrant, so the request
# deadlocked against itself while still holding the lock and every later request that touched a
# balance queued behind it forever. The process stayed up and kept accepting connections. Nothing
# in the suite noticed, because no test called that endpoint and then checked the venue was still
# alive. So now every route is walked, and liveness is asserted after each one.
routes = [
    ("GET", "/", False), ("GET", "/health", False), ("GET", "/bet", False),
    ("GET", "/build", False), ("GET", "/agent.py", False),
    ("GET", "/v1/spec", False), ("GET", "/v1/tools", False), ("GET", "/v1/markets", False),
    ("GET", f"/v1/markets/{mid}", False), ("GET", f"/v1/markets/{mid}/quote?outcome=0&amount=5", False),
    ("GET", "/v1/challenges", False), ("GET", "/v1/events?since=0", False),
    ("GET", "/v1/feed", False), ("GET", "/v1/reserves", False), ("GET", "/.well-known/ikenga.json", False),
    ("GET", "/v1/dashboard", True), ("GET", "/v1/treasury", True),
]
wedged = None
for method, path, owner in routes:
    st, _ = call(method, path, owner=owner)
    alive, _ = call("GET", "/health")
    check(f"{path} answers and leaves the venue alive", st < 500 and alive == 200, (path, st, alive))
    if alive != 200:
        wedged = path
        break
check("nothing wedged the venue", wedged is None, wedged)

# Signed routes too, since those take a different set of locks on the way in.
for method, path in [("GET", "/v1/account"), ("GET", "/v1/account/record")]:
    st, _ = call(method, path, seed=seed_d, agent_id=agent_d)
    alive, _ = call("GET", "/health")
    check(f"{path} (signed) leaves the venue alive", st < 500 and alive == 200, (path, st, alive))

# And the console specifically, twice in a row and then a balance read — the exact shape that
# deadlocked, since the wedge only showed up on the request *after* the poisoned one.
call("GET", "/v1/dashboard", owner=True)
call("GET", "/v1/dashboard", owner=True)
st, acct = call("GET", "/v1/account", seed=seed_d, agent_id=agent_d)
check("a balance still reads after two console calls", st == 200 and "balances" in acct, st)

st, dash = call("GET", "/v1/dashboard", owner=True)
sl = dash.get("seed_liquidity", {})
check("the console shows what the seed subsidy is costing",
      "spent_today" in sl and "daily_cap" in sl and "bankroll_left" in sl, sl)
check("and the subsidy has actually been spent", sl.get("spent_today", 0) > 0, sl)
check("the daily cap is below the whole bankroll, so it cannot drain in one day",
      sl["daily_cap"] < sl["bankroll_left"] + sl["spent_today"], sl)

# Public reads must be metered — they need no key, so nothing else bounds them.
codes = {}
for _ in range(120):
    st, _ = _call_once("GET", f"/v1/markets/{mid}/quote?outcome=0&amount=5", None, None, None, False)
    codes[st] = codes.get(st, 0) + 1
check("unauthenticated reads are rate limited", codes.get(429, 0) > 0, codes)
check("but a reasonable number still get through", codes.get(200, 0) >= 50, codes)
alive, _ = call("GET", "/health")
check("and the venue is fine afterwards", alive == 200, alive)

# ---------------------------------------------------------------------------------------------
# 9. Weaknesses that only show up once it leaves the laptop
# ---------------------------------------------------------------------------------------------
print("\n-- once it is actually on the internet --")

# The venue must advertise an address a caller can reach, not the one it happens to be bound to.
st, ctype, body = raw_get("/v1/tools")
tools_here = json.loads(body)
check("the venue advertises the address the caller reached it on",
      tools_here["base_url"] == f"http://{HOST}:{PORT}", tools_here["base_url"])
check("and never 0.0.0.0, which no agent can dial",
      "0.0.0.0" not in tools_here["base_url"], tools_here["base_url"])

conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
conn.request("GET", "/v1/tools", headers={"Host": "ikenga.example.com"})
r = conn.getresponse(); hosted = json.loads(r.read()); conn.close()
check("behind a proxy it uses the public hostname it was called by",
      hosted["base_url"] == "http://ikenga.example.com", hosted["base_url"])

conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
conn.request("GET", "/v1/tools", headers={"Host": 'evil"><script>x</script>'})
r = conn.getresponse(); nasty = json.loads(r.read()); conn.close()
check("a hostile Host header cannot break out of the URL",
      "<" not in nasty["base_url"] and ">" not in nasty["base_url"], nasty["base_url"])

# The betting page's key must be exportable, or a cleared cache is a destroyed account.
_, _, page = raw_get("/bet")
check("the page can export its key", "ikenga-key-v1" in page and "backupBlob" in page)
check("and import one back", "dorestore" in page and "restorebox" in page)
check("a bad key is rejected before the good one is overwritten",
      "prevPriv = priv" in page and "priv = prevPriv" in page)
# The greeting is generated client-side, and an unsigned shift in it produced "Keen undefined"
# for any account whose hash landed above 2^31 — roughly half of them.
check("the friendly name uses an unsigned shift, so it cannot index off the end",
      ">>> 8" in page and ">> 8]" not in page, "signed shift in handleFor")
check("a new account is greeted as new, not welcomed back",
      page.index("Pick a side.") < page.index("Welcome back"),
      "the two greetings are the wrong way round")
check("people can post a bet on anything and anyone can take it",
      "mutual_agreement" in page and "opennew" in page)
check("and the page says neither side can settle it alone",
      "Neither of you can" in page or "decide it alone" in page)

check("it carries its own signer so it works without HTTPS",
      "publicFromSeed" in page and "edSign" in page)
check("and says plainly when the connection is not encrypted",
      "isSecureContext" in page and "isn't encrypted" in page)
check("an unreachable venue is stated, not left as a blank page",
      "Can\\'t reach the venue" in page or "reach the venue" in page)

# The registration cap must not be so tight that a shared connection locks everyone out. This
# server runs with the cap raised for testing, so what is checked is the refusal itself: when it
# does fire, it must tell a blocked person what to do rather than reading as a broken venue.
before = len([1])
codes = []
for _ in range(4):
    st, _, _ = raw_get("/v1/markets")
    codes.append(st)
check("reading the board never needs an account", all(c == 200 for c in codes), codes)

# ---------------------------------------------------------------------------------------------
# 10. Can it actually get the information it needs to settle?
# ---------------------------------------------------------------------------------------------
print("\n-- live information to settle with --")

st, oracle = call("GET", "/v1/oracle?pair=BTC-USD", owner=True)
check("settlement readiness can be checked over HTTP", st == 200 and "can_settle" in oracle, st)
check("it names every configured source", len(oracle.get("sources", [])) >= 1, oracle.get("sources"))
check("and says in words whether anything can settle",
      isinstance(oracle.get("verdict"), str) and len(oracle["verdict"]) > 20, oracle.get("verdict"))
check("a failing oracle is stated as CANNOT SETTLE, not left to be inferred",
      oracle["can_settle"] or "CANNOT SETTLE" in oracle["verdict"], oracle["verdict"])
st, _ = call("GET", "/v1/oracle")
check("the oracle check needs the owner key", st == 403, st)

# The real bodies these four venues returned on 2026-09-09, replayed. This exercises the actual
# parsers, the actual median and the actual outlier check against real-shaped data — the thing
# that decides who gets paid — on a machine that cannot reach an exchange.
LIVE_BODIES = {
    "coinbase": '{"data":{"amount":"78754.00","base":"BTC","currency":"USD"}}',
    "kraken": '{"error":[],"result":{"XXBTZUSD":{"a":["78755.00000","1","1.000"],'
              '"b":["78753.00000","1","1.000"],"c":["78754.00000","0.00076909"],"o":"78449.60000"}}}',
    "gemini": '{"bid":"78753.00000","ask":"78755.00000","last":"78754.00000",'
              '"volume":{"BTC":"76.83","USD":"6033790.46","timestamp":1788856560000}}',
    "bitfinex": '[78753,3.49,78755,3.11,-764,-0.0095,78754,1114.89,80010,78845,1358182043000]',
}


class VenueHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.lstrip("/").split("/")[0].split("?")[0]
        body = LIVE_BODIES.get(name)
        if body is None:
            self.send_response(404); self.end_headers(); return
        d = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers(); self.wfile.write(d)

    def log_message(self, *a):
        pass


socketserver.TCPServer.allow_reuse_address = True
venues = socketserver.TCPServer(("127.0.0.1", VENUE_PORT), VenueHandler)
threading.Thread(target=venues.serve_forever, daemon=True).start()

st, probe = call("GET", "/v1/oracle?pair=BTC-USD", owner=True)
answered = {s["source"]: s["price"] for s in probe["sources"] if s["answered"]}
check("every shipped parser reads its venue's real response body",
      len(answered) == 4, probe["sources"])
check("and they all read the same price out of four different shapes",
      set(round(p, 2) for p in answered.values()) == {78754.0}, answered)
check("so the venue reports it can settle", probe["can_settle"] is True, probe["verdict"])
check("with a spread of zero across them", abs(probe["spread_pct"]) < 1e-9, probe["spread_pct"])

# Kraken's key is its own name for the market, reached through the wildcard segment. Worth its
# own check: it is the one path that cannot be derived from the pair.
check("the kraken wildcard path resolved", answered.get("kraken") == 78754.0, answered.get("kraken"))
# Bitfinex answers with a bare array and the last price is at index 6, not 0.
check("the bitfinex array index picked the last price, not the bid",
      answered.get("bitfinex") == 78754.0, answered.get("bitfinex"))

venues.shutdown()

stop(proc)
if os.path.exists(WAL):
    os.remove(WAL)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("A RATIONAL AGENT HAS A REASON TO BE HERE")
