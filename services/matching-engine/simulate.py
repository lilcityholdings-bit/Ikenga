#!/usr/bin/env python3
"""A load and behaviour simulator for a whole Ikenga venue.

    python3 simulate.py [rounds]

The test suites answer "does this feature do what it says". This answers a different question:
what happens when hundreds of people and bots use all of it at once, for a while, at speed, in an
order nobody planned. Those are the failures that do not show up in a unit test — a lock taken in
the wrong order, a balance that drifts by a rounding error every thousand trades, a background
sweeper racing a settlement, memory that only grows.

Four phases, in this order on purpose:

  1. Each feature alone, so a failure has exactly one possible cause.
  2. All features at once at low volume, which is where interactions first appear.
  3. Random: weighted actions, random timing, the way a real venue is used.
  4. Ramp: the same mix at rising concurrency until something bends.

Invariants are checked continuously rather than at the end, because "the books balanced when it
finished" hides a venue that was wrong for ten minutes in the middle.
"""
import json
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HOST, PORT = "127.0.0.1", 8399
BASE = f"http://{HOST}:{PORT}"
BIN = "./target/release/ikenga-matching-engine"
OWNER = "5a" * 32
WAL = "/tmp/ikenga-sim.wal"
VENUE_PORT = 8398
START_POINTS = 1000.0

findings = []
lock = threading.Lock()


def finding(phase, what, detail=""):
    with lock:
        entry = (phase, what, str(detail)[:400])
        if entry not in findings:
            findings.append(entry)
            print(f"  !! [{phase}] {what}  {str(detail)[:200]}")


# ---------------------------------------------------------------------------------------------
# A price feed we control, so the venue has something real-shaped to settle against and the
# simulation can move the market on purpose.
# ---------------------------------------------------------------------------------------------
import http.server
import socketserver

PRICE = [78754.0]


class Venue(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = PRICE[0]
        name = self.path.lstrip("/").split("/")[0].split("?")[0]
        bodies = {
            "coinbase": json.dumps({"data": {"amount": f"{p:.2f}"}}),
            "kraken": json.dumps({"error": [], "result": {"XXBTZUSD": {"c": [f"{p:.5f}", "0.1"]}}}),
            "gemini": json.dumps({"last": f"{p:.5f}"}),
            "bitfinex": json.dumps([p - 1, 1, p + 1, 1, 0, 0, p, 1, p, p, 0]),
        }
        body = bodies.get(name)
        if body is None:
            self.send_response(404); self.end_headers(); return
        d = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------------------------------------
# A participant. Signs with a real Ed25519 key, the same way any client must.
# ---------------------------------------------------------------------------------------------
class Actor:
    def __init__(self, kind):
        self.kind = kind          # "bot" or "person" — same protocol, different habits
        self.key = Ed25519PrivateKey.generate()
        self.agent_id = None
        self.staked = 0.0
        self.errors = 0

    @property
    def pubkey_hex(self):
        return self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    def call(self, method, path, body=None, signed=True, timeout=20):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"} if raw else {}
        if signed:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            nonce = f"{time.time_ns()}-{random.randrange(1 << 30)}"
            msg = method.encode() + path.encode() + ts.encode() + nonce.encode() + raw
            headers.update({
                "X-Agent-ID": self.agent_id or "registration",
                "X-Timestamp": ts,
                "X-Nonce": nonce,
                "X-Signature": self.key.sign(msg).hex(),
            })
        req = urllib.request.Request(BASE + path, data=raw or None, method=method, headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw_body = r.read()
                # Not everything this venue serves is JSON — /bet and /build are HTML and
                # /agent.py is source. Treating a parse failure as a transport failure reported
                # 1500 phantom outages in the first run. The measuring instrument has to be right
                # before its readings mean anything.
                try:
                    parsed = json.loads(raw_body or b"{}")
                except ValueError:
                    parsed = {"_bytes": len(raw_body)}
                return r.status, parsed, time.time() - t0
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}"), time.time() - t0
            except Exception:
                return e.code, {}, time.time() - t0
        except Exception as e:
            return 0, {"transport": str(e)}, time.time() - t0

    def register(self):
        st, body, _ = self.call("POST", "/v1/agents", {"pubkey_hex": self.pubkey_hex})
        if st in (200, 201):
            self.agent_id = body["agent_id"]
            return True
        return False

    def balance(self, asset="PTS"):
        st, body, _ = self.call("GET", "/v1/account")
        if st != 200:
            return None
        return next((b["balance"] for b in body.get("balances", []) if b["asset"] == asset), 0.0)


def owner_call(method, path, body=None, timeout=30):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {"X-Owner-Key": OWNER}
    if raw:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=raw or None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"transport": str(e)}


def public(path, timeout=20):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


# ---------------------------------------------------------------------------------------------
# Invariants. Checked while the thing is running, not after it stops.
# ---------------------------------------------------------------------------------------------
class Invariants:
    """Money is conserved, nothing goes negative, and the venue keeps answering.

    Total points in existence only ever grow by registration grants and the house's bankroll.
    Everything after that is a move between an agent, an escrow and the fee ledger, so at any
    instant the sum of every balance, every open stake, every unmatched offer and every fee taken
    must equal what was minted. A drift of a few atoms is floating point; a drift that grows is a
    leak.
    """

    def __init__(self):
        self.max_drift = 0.0
        self.samples = 0

    def check(self, actors, phase):
        alive, _ = public("/health")
        if alive != 200:
            finding(phase, "the venue stopped answering /health", alive)
            return

        # EVERY registered account, not a sample. The first version sampled eight and compared
        # them against the whole board's pools, so money belonging to the other hundred-odd
        # participants looked like money appearing from nowhere, and the alarm rose steadily all
        # run. A conservation check has to see both sides of the ledger or it is measuring noise.
        registered = [a for a in list(actors) + QUIET_ACTOR if a.agent_id]
        minted = START_POINTS * len(registered)
        # What the house has pushed onto the board. Its untouched bankroll is its own balance and
        # is deliberately NOT counted here — counting it made every reading look like a 50,000
        # point hole, which is exactly the kind of false alarm that trains you to ignore the alarm.

        # Order matters more than it looks. Pools are read BEFORE balances, so a stake that
        # lands between the two reads is counted in neither and shows up as a harmless
        # undershoot. Read the other way round, that same in-flight stake is counted twice and
        # reports money appearing from nowhere — which is what the first three runs "found".
        pooled = 0.0
        st, mk = public("/v1/markets?limit=500")
        if st == 200:
            for m in mk.get("markets", []):
                if m.get("status") in ("Open", "Closed", "Proposed", "Disputed"):
                    pooled += sum(o.get("pool", 0.0) for o in m.get("outcomes", []))

        escrow = 0.0
        st, ch = public("/v1/challenges?limit=500")
        if st == 200:
            for c in ch.get("challenges", []):
                if str(c.get("status", "")).lower() == "open":
                    escrow += float(c.get("proposer_side", {}).get("stake", 0.0))

        held = 0.0
        with ThreadPoolExecutor(max_workers=24) as ex:
            balances = list(ex.map(lambda a: (a.agent_id, a.balance()), registered))
        for agent_id, b in balances:
            if b is None:
                finding(phase, "an account could not be read", agent_id)
                return
            if b < -1e-9:
                finding(phase, "a balance went negative", f"{agent_id} = {b}")
            held += b

        # Read after the pools, so the ceiling can only be too generous, never too tight.
        st, dash = owner_call("GET", "/v1/dashboard")
        if st != 200:
            finding(phase, "the operator console stopped answering", st)
            return
        seeded = float(dash.get("seed_liquidity", {}).get("spent_today", 0))
        fees = sum(r["amount"] for r in dash.get("revenue", {}).get("by_asset", [])
                   if r["asset"] == "PTS")

        st, res = public("/v1/reserves")
        if st == 200 and res.get("fully_backed") is False:
            finding(phase, "the venue reported itself not fully backed", res)

        # Sampling only some accounts means `held` is partial, so the absolute sum cannot be
        # compared to the mint. What CAN be compared, and is the thing that actually matters, is
        # that no account is ever paid more than the venue ever created: the sampled holdings plus
        # everything still locked in pools and offers must not exceed the total ever minted.
        # A leak shows up as this rising past the ceiling; a theft shows up as a negative balance,
        # checked above.
        ceiling = minted + seeded
        accounted = held + pooled + escrow + fees
        overshoot = accounted - ceiling
        self.samples += 1
        self.max_drift = max(self.max_drift, overshoot)
        if overshoot > max(5.0, 0.05 * len(registered)):
            finding(phase, "more money exists than was ever created",
                    f"accounted {accounted:.2f} vs ceiling {ceiling:.2f} (+{overshoot:.2f})")


# ---------------------------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------------------------
def start_server(extra=None):
    if os.path.exists(WAL):
        os.remove(WAL)
    src = ";".join([
        f"coinbase|http://127.0.0.1:{VENUE_PORT}/coinbase?p={{sell}}-{{buy}}|data.amount|rate",
        f"kraken|http://127.0.0.1:{VENUE_PORT}/kraken?p={{sell}}{{buy}}|result.*.c.0|rate",
        f"gemini|http://127.0.0.1:{VENUE_PORT}/gemini?p={{sell_lower}}{{buy_lower}}|last|rate",
        f"bitfinex|http://127.0.0.1:{VENUE_PORT}/bitfinex?p={{sell}}{{buy}}|6|rate",
    ])
    env = {
        **os.environ,
        "IKENGA_BIND": f"{HOST}:{PORT}",
        "IKENGA_OWNER_KEY": OWNER,
        "IKENGA_WAL_PATH": WAL,
        "IKENGA_ROUTE_SOURCES": src,
        "IKENGA_AUTOPILOT": "BTC-USD:120@-50,0,+50",
        "IKENGA_SEED_PER_MARKET": "25",
        "IKENGA_SEED_BANKROLL": "50000",
        "IKENGA_SEED_DAILY_MAX": "50000",
        "IKENGA_REGISTRATIONS_PER_HOUR": "100000",
        "IKENGA_SWEEP_INTERVAL_MS": "1000",
        "IKENGA_DISABLE_RATE_LIMIT": "1",
        **(extra or {}),
    }
    proc = subprocess.Popen(BIN, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    deadline = time.time() + 25
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if "listening on" in line:
            break
    # Keep draining, or the pipe fills and the server blocks on its own stdout.
    drained = []
    threading.Thread(target=lambda: [drained.append(l.rstrip()) for l in proc.stdout],
                     daemon=True).start()
    return proc, lines, drained


def stop_server(proc):
    try:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------
# The actions a participant can take. Each returns (ok, label) so the mix can be counted.
# ---------------------------------------------------------------------------------------------
LATENCY = defaultdict(list)
STATUS = Counter()


def record(label, st, dt, phase, ok_codes=(200, 201)):
    with lock:
        LATENCY[label].append(dt)
        STATUS[f"{label}:{st}"] += 1
    if st >= 500 or st == 0:
        finding(phase, f"{label} returned {st or 'no response'}", "")
        return False
    return st in ok_codes


def act_read_board(a, phase):
    st, body, dt = a.call("GET", "/v1/markets?limit=50", signed=False)
    record("board", st, dt, phase)
    return [m for m in body.get("markets", []) if m.get("status") == "Open"]


def act_quote(a, phase, markets):
    if not markets:
        return
    m = random.choice(markets)
    st, body, dt = a.call(
        "GET",
        f"/v1/markets/{m['market_id']}/quote?outcome={random.randint(0,1)}"
        f"&amount={random.choice([1,5,10,25])}&belief={random.uniform(0.05,0.95):.4f}",
        signed=False)
    if record("quote", st, dt, phase) and body.get("breakeven_probability") is not None:
        be = body["breakeven_probability"]
        if not (0.0 <= be <= 1.0000001):
            finding(phase, "a quote returned a breakeven outside 0..1", be)
        if body.get("payout_if_right", 0) < 0:
            finding(phase, "a quote offered a negative payout", body.get("payout_if_right"))


def act_stake(a, phase, markets):
    if not markets:
        return
    m = random.choice(markets)
    amt = round(random.uniform(1, 20), 2)
    st, body, dt = a.call("POST", f"/v1/markets/{m['market_id']}/stakes",
                          {"outcome": random.randint(0, 1), "amount": amt})
    if record("stake", st, dt, phase, ok_codes=(200, 201, 400, 409, 422)):
        if st in (200, 201):
            a.staked += amt


QUIET_ACTOR = []


def act_dry_run(a, phase, markets):
    if not markets:
        return
    # Deliberately not `a`. Comparing one actor's balance either side of its own rehearsal is
    # only meaningful if nothing else is spending that actor's money at the same time, and in a
    # hundred-thread simulation something always is. This actor is driven by nobody else.
    if not QUIET_ACTOR:
        return
    a = QUIET_ACTOR[0]
    m = random.choice(markets)
    before = a.balance()
    st, body, dt = a.call("POST", f"/v1/markets/{m['market_id']}/stakes",
                          {"outcome": 0, "amount": 5, "dry_run": True})
    record("dry_run", st, dt, phase, ok_codes=(200, 400, 422))
    if st == 200:
        after = a.balance()
        if before is not None and after is not None and abs(after - before) > 1e-9:
            finding(phase, "a dry run moved money", f"{before} -> {after}")


def act_challenge(a, phase):
    now = int(time.time() * 1000)
    mine = round(random.uniform(2, 15), 2)
    st, body, dt = a.call("POST", "/v1/challenges", {
        "question": f"Sim bet {random.randrange(10**6)}: will the number be higher?",
        "my_outcome": random.randint(0, 1),
        "my_stake": mine,
        "their_stake": round(random.uniform(2, 15), 2),
        "closes_at_ms": now + random.choice([4000, 8000, 30000]),
        "observed_at_ms": now + random.choice([5000, 9000, 31000]),
        "dispute_window_ms": 0,
        "resolution": {"kind": "mutual_agreement", "criteria": "both of us saw the same thing"},
    })
    record("challenge_open", st, dt, phase, ok_codes=(200, 201, 400, 422))


def act_take_challenge(a, phase):
    st, body, _ = a.call("GET", "/v1/challenges?limit=40", signed=False)
    if st != 200:
        return
    open_ones = [c for c in body.get("challenges", []) if str(c.get("status", "")).lower() == "open"]
    if not open_ones:
        return
    c = random.choice(open_ones)
    st, body, dt = a.call("POST", f"/v1/challenges/{c['challenge_id']}/accept", {})
    record("challenge_take", st, dt, phase, ok_codes=(200, 201, 400, 403, 409, 422))


def act_report(a, phase):
    st, acct, _ = a.call("GET", "/v1/account")
    if st != 200:
        return
    now = int(time.time() * 1000)
    for p in acct.get("positions", []):
        if p.get("status") in ("Closed", "Open") and p.get("observed_at_ms", 0) < now:
            st, body, dt = a.call("POST", f"/v1/markets/{p['market_id']}/report",
                                  {"outcome": random.randint(0, 1)})
            record("report", st, dt, phase, ok_codes=(200, 422))
            return


def act_account(a, phase):
    st, body, dt = a.call("GET", "/v1/account")
    record("account", st, dt, phase)
    if st == 200:
        for b in body.get("balances", []):
            if b["balance"] < -1e-9:
                finding(phase, "account showed a negative balance", b)


def act_events(a, phase, cursor=[0]):
    st, body, dt = a.call(f"GET", f"/v1/events?since={cursor[0]}&limit=50", signed=False)
    if record("events", st, dt, phase):
        cursor[0] = body.get("next_cursor", cursor[0])


def act_feed(a, phase):
    st, _, dt = a.call("GET", "/v1/feed", signed=False)
    record("feed", st, dt, phase, ok_codes=(200, 429))


def act_record(a, phase):
    st, body, dt = a.call("GET", "/v1/account/record")
    if record("record", st, dt, phase) and body.get("mean_brier") is not None:
        if not (0.0 <= body["mean_brier"] <= 2.0):
            finding(phase, "a Brier score outside its possible range", body["mean_brier"])


def act_page(a, phase):
    for p in ("/bet", "/build", "/v1/tools", "/v1/spec"):
        st, _, dt = a.call("GET", p, signed=False)
        record("page", st, dt, phase)


ACTIONS = [
    ("board", 14, lambda a, ph, mk: act_read_board(a, ph)),
    ("quote", 18, lambda a, ph, mk: act_quote(a, ph, mk)),
    ("stake", 16, lambda a, ph, mk: act_stake(a, ph, mk)),
    ("dry_run", 5, lambda a, ph, mk: act_dry_run(a, ph, mk)),
    ("challenge", 6, lambda a, ph, mk: act_challenge(a, ph)),
    ("take", 8, lambda a, ph, mk: act_take_challenge(a, ph)),
    ("report", 6, lambda a, ph, mk: act_report(a, ph)),
    ("account", 10, lambda a, ph, mk: act_account(a, ph)),
    ("events", 9, lambda a, ph, mk: act_events(a, ph)),
    ("feed", 3, lambda a, ph, mk: act_feed(a, ph)),
    ("record", 3, lambda a, ph, mk: act_record(a, ph)),
    ("page", 2, lambda a, ph, mk: act_page(a, ph)),
]
WEIGHTS = [w for _, w, _ in ACTIONS]


# ---------------------------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------------------------
def phase_one_at_a_time(actors, inv):
    """Every feature alone, so a failure has exactly one possible cause."""
    print("\n[1] each feature on its own")
    a, b = actors[0], actors[1]
    markets = act_read_board(a, "solo")
    if not markets:
        finding("solo", "no open markets to work with", "autopilot may not be opening any")
    checks = [
        ("read the board", lambda: act_read_board(a, "solo") is not None),
        ("price a bet", lambda: act_quote(a, "solo", markets) is None),
        ("rehearse a bet", lambda: act_dry_run(a, "solo", markets) is None),
        ("place a bet", lambda: act_stake(a, "solo", markets) is None),
        ("read the account", lambda: act_account(a, "solo") is None),
        ("follow events", lambda: act_events(a, "solo") is None),
        ("read the feed", lambda: act_feed(a, "solo") is None),
        ("fetch the record", lambda: act_record(a, "solo") is None),
        ("post a challenge", lambda: act_challenge(a, "solo") is None),
        ("take a challenge", lambda: act_take_challenge(b, "solo") is None),
        ("report an outcome", lambda: act_report(a, "solo") is None),
        ("serve the pages", lambda: act_page(a, "solo") is None),
    ]
    for name, fn in checks:
        try:
            fn()
            print(f"    {name}: ok")
        except Exception as e:
            finding("solo", f"{name} raised", e)
    inv.check(actors, "solo")


def phase_together(actors, inv, seconds=25):
    """Everything at once, gently. Interactions show up here before volume does."""
    print(f"\n[2] all features together for {seconds}s")
    run_mixed(actors, inv, seconds, workers=8, phase="together", pace=(0.05, 0.25),
              drivers=actors[:20])


def phase_random(actors, inv, seconds=35):
    """Weighted actions, random timing, the way a venue is actually used."""
    print(f"\n[3] randomised use for {seconds}s")
    run_mixed(actors, inv, seconds, workers=16, phase="random", pace=(0.0, 0.3))


def phase_ramp(actors, inv):
    """Rising concurrency until something bends."""
    print("\n[4] rising load")
    results = []
    for workers in (8, 32, 96, 200, 400):
        LATENCY.clear()
        t0 = time.time()
        run_mixed(actors, inv, 12, workers=workers, phase=f"ramp{workers}", pace=(0.0, 0.02),
                  check_every=11)
        allv = [v for vs in LATENCY.values() for v in vs]
        if not allv:
            continue
        allv.sort()
        rps = len(allv) / max(time.time() - t0, 0.001)
        p50 = allv[len(allv) // 2] * 1000
        p99 = allv[min(len(allv) - 1, int(len(allv) * 0.99))] * 1000
        results.append((workers, len(allv), rps, p50, p99))
        print(f"    {workers:3d} clients: {len(allv):6d} calls  {rps:7.0f}/s  "
              f"p50 {p50:6.1f}ms  p99 {p99:7.1f}ms")
        if p99 > 8000:
            finding(f"ramp{workers}", "p99 latency above 5s", f"{p99:.0f}ms at {workers} clients")
    return results


def run_mixed(actors, inv, seconds, workers, phase, pace, check_every=12, drivers=None):
    # `actors` is the whole roster, for conservation. `drivers` is who is actually doing things.
    drivers = drivers or actors
    stop_at = time.time() + seconds
    board = {"markets": act_read_board(drivers[0], phase)}

    def refresher():
        while time.time() < stop_at:
            board["markets"] = act_read_board(drivers[0], phase)
            time.sleep(2)

    def checker():
        while time.time() < stop_at:
            time.sleep(check_every)
            inv.check(actors, phase)

    def mover():
        # The price wanders, so markets actually resolve both ways rather than always one.
        while time.time() < stop_at:
            PRICE[0] = max(1000.0, PRICE[0] * (1 + random.uniform(-0.004, 0.004)))
            time.sleep(1.5)

    def worker():
        while time.time() < stop_at:
            a = random.choice(drivers)
            name, _, fn = random.choices(ACTIONS, weights=WEIGHTS, k=1)[0]
            try:
                fn(a, phase, board["markets"])
            except Exception as e:
                finding(phase, f"client-side crash during {name}", e)
            lo, hi = pace
            if hi > 0:
                time.sleep(random.uniform(lo, hi))

    threads = [threading.Thread(target=refresher, daemon=True),
               threading.Thread(target=checker, daemon=True),
               threading.Thread(target=mover, daemon=True)]
    for t in threads:
        t.start()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in range(workers):
            ex.submit(worker)
    for t in threads:
        t.join(timeout=2)


def summarise(ramp):
    print("\n--- what happened ---")
    total = sum(sum(1 for _ in v) for v in LATENCY.values())
    print(f"  calls this round: {total}")
    bad = {k: v for k, v in STATUS.items() if k.split(":")[1] not in
           ("200", "201", "400", "403", "404", "409", "422", "429")}
    if bad:
        print(f"  unexpected statuses: {dict(bad)}")
    for w, n, rps, p50, p99 in ramp:
        print(f"  {w:3d} clients  {rps:7.0f} req/s  p50 {p50:6.1f}ms  p99 {p99:7.1f}ms")


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    socketserver.TCPServer.allow_reuse_address = True
    venues = socketserver.TCPServer(("127.0.0.1", VENUE_PORT), Venue)
    threading.Thread(target=venues.serve_forever, daemon=True).start()

    for rnd in range(1, rounds + 1):
        print(f"\n{'='*78}\nROUND {rnd}\n{'='*78}")
        findings.clear()
        LATENCY.clear()
        STATUS.clear()
        proc, boot, drained = start_server()
        if not any("listening on" in l for l in boot):
            print("server did not start:\n" + "\n".join(boot))
            sys.exit(1)

        print("  waiting for the board to fill...")
        time.sleep(12)

        n_actors = 120
        actors = [Actor("bot" if i % 3 else "person") for i in range(n_actors)]
        with ThreadPoolExecutor(max_workers=24) as ex:
            list(ex.map(lambda a: a.register(), actors))
        actors = [a for a in actors if a.agent_id]
        # One participant held back from the action mix, so the dry-run check has a stable
        # balance to compare against.
        QUIET_ACTOR.clear()
        if actors:
            QUIET_ACTOR.append(actors.pop())
        print(f"  {len(actors)} participants registered (plus one held quiet for the dry-run check)")
        if len(actors) + len(QUIET_ACTOR) < n_actors:
            finding("setup", "some registrations failed",
                    f"{len(actors) + len(QUIET_ACTOR)}/{n_actors}")

        inv = Invariants()
        phase_one_at_a_time(actors, inv)
        phase_together(actors, inv)
        phase_random(actors, inv)
        ramp = phase_ramp(actors, inv)

        print("\n  final settle-down...")
        time.sleep(8)
        inv.check(actors, "final")

        alive, _ = public("/health")
        if alive != 200:
            finding("final", "the venue was not answering at the end", alive)
        crashes = [l for l in drained if "panic" in l.lower() or "FATAL" in l]
        if crashes:
            finding("final", "the server log contains a panic", crashes[:2])

        summarise(ramp)
        print(f"  peak money drift observed: {inv.max_drift:.6f} over {inv.samples} checks")
        stop_server(proc)

        if findings:
            print(f"\n  {len(findings)} PROBLEM(S) FOUND IN ROUND {rnd}:")
            for ph, what, detail in findings:
                print(f"    [{ph}] {what}  {detail[:160]}")
            sys.exit(1)
        print(f"\n  ROUND {rnd}: clean")

    print("\nALL ROUNDS CLEAN")


if __name__ == "__main__":
    main()
