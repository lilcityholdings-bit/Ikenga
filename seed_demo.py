#!/usr/bin/env python3
"""Fills a running Ikenga with example markets so the dashboard has something in it.

Called by `./ikenga demo`. Safe to run more than once — it only adds.

Everything here goes through the real signed API exactly as an outside agent would: it registers
keypairs, signs each request, and stakes. Nothing reaches into the server's internals, so if this
script works, an agent's will too.
"""
import base64
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time

PORT = sys.argv[1] if len(sys.argv) > 1 else "8080"
OWNER_KEY = sys.argv[2] if len(sys.argv) > 2 else ""
HOST = "127.0.0.1"
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def keypair():
    with tempfile.NamedTemporaryFile(suffix=".pem") as f:
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", f.name],
                       check=True, capture_output=True)
        priv = subprocess.run(["openssl", "pkey", "-in", f.name, "-outform", "DER"],
                              check=True, capture_output=True).stdout
        pub = subprocess.run(["openssl", "pkey", "-in", f.name, "-pubout", "-outform", "DER"],
                             check=True, capture_output=True).stdout
    return priv[16:].hex(), pub[12:].hex()


def sign(seed_hex, method, path, ts, nonce, body=b""):
    payload = method.encode() + path.encode() + ts.encode() + nonce.encode() + body
    b64 = base64.b64encode(PKCS8_PREFIX + bytes.fromhex(seed_hex)).decode()
    pem = ("-----BEGIN PRIVATE KEY-----\n" + b64 + "\n-----END PRIVATE KEY-----\n").encode()
    with tempfile.NamedTemporaryFile() as k, tempfile.NamedTemporaryFile() as m, \
         tempfile.NamedTemporaryFile() as s:
        k.write(pem); k.flush()
        m.write(payload); m.flush()
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", k.name, "-rawin",
                        "-in", m.name, "-out", s.name], check=True, capture_output=True)
        return s.read().hex()


def call(method, path, seed=None, agent_id=None, body=None, owner=False, retries=8):
    raw = json.dumps(body).encode() if body is not None else b""
    for _ in range(retries):
        h = {}
        if raw:
            h["Content-Type"] = "application/json"
        if owner:
            h["X-Owner-Key"] = OWNER_KEY
        if seed:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            nonce = f"demo-{time.time_ns()}"
            h.update({"X-Agent-ID": agent_id or "registration", "X-Timestamp": ts,
                      "X-Nonce": nonce, "X-Signature": sign(seed, method, path, ts, nonce, raw)})
        c = http.client.HTTPConnection(HOST, PORT, timeout=15)
        c.request(method, path, body=raw, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        if r.status != 429:
            try:
                return r.status, json.loads(data)
            except Exception:
                return r.status, data
        time.sleep(0.4)
    return r.status, data


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "demo_agents.json")


def demo_agents(count):
    """Register demo agents once and remember them.

    Signups are capped per IP per hour on purpose — it is the cheapest brake on someone farming
    free points from a script. Re-registering on every demo run would burn that budget for no
    reason and make the second run fail, so the keys are cached beside the data directory.
    """
    if os.path.exists(CACHE):
        try:
            cached = json.load(open(CACHE))
            if len(cached) >= count:
                print(f"  reusing {count} demo agents from data/demo_agents.json")
                return [(a["seed"], a["agent_id"]) for a in cached[:count]]
        except Exception:
            pass

    agents = []
    for i in range(count):
        seed, pub = keypair()
        st, reg = call("POST", "/v1/agents", seed=seed, body={"pubkey_hex": pub})
        if st == 429:
            raise SystemExit(
                "  Signups are capped per IP per hour and this address has used its budget.\n"
                "  That cap is deliberate. Either wait an hour, or restart with it raised for\n"
                "  local use:\n\n"
                "    ./ikenga stop\n"
                "    IKENGA_REGISTRATIONS_PER_HOUR=100 ./ikenga start\n"
                "    ./ikenga demo\n"
            )
        if st != 201:
            raise SystemExit(f"could not register a demo agent: {st} {reg}")
        agents.append({"seed": seed, "agent_id": reg["agent_id"]})

    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(agents, f, indent=2)
    os.chmod(CACHE, 0o600)
    print(f"  registered {count} demo agents (keys cached in data/demo_agents.json)")
    return [(a["seed"], a["agent_id"]) for a in agents]


def market(question, closes_in_s, outcomes=None, observe_after_ms=60_000,
           dispute_window_ms=600_000):
    now = int(time.time() * 1000)
    st, m = call("POST", "/v1/markets", owner=True, body={
        "question": question,
        "outcomes": outcomes or ["YES", "NO"],
        "closes_at_ms": now + closes_in_s * 1000,
        "observed_at_ms": now + closes_in_s * 1000 + observe_after_ms,
        "dispute_window_ms": dispute_window_ms,
        "resolution": {"kind": "operator_declared",
                       "criteria": "as reported by the named source at the observation time",
                       "source": "example.test/results"},
    })
    if st != 201:
        raise SystemExit(f"could not open a market: {st} {m}")
    return m["market_id"], m


agents = demo_agents(4)

plan = [
    ("Will ETH close above $4,000 on Friday?", 3600, [(0, 420), (1, 260), (0, 110), (1, 90)]),
    ("Will the CPI print come in below consensus?", 7200, [(1, 300), (0, 180), (1, 120)]),
    ("Will this venue pass 1,000 settled markets by year end?", 86400, [(0, 150), (0, 90), (1, 240)]),
]
for question, closes, bets in plan:
    mid, _ = market(question, closes)
    for (outcome, amount), (seed, aid) in zip(bets, agents):
        call("POST", f"/v1/markets/{mid}/stakes", seed=seed, agent_id=aid,
             body={"outcome": outcome, "amount": amount})
    print(f"  opened: {question}")

# One that closes almost immediately, then settle it, so the dashboard has revenue and a track
# record to show. Zero dispute window because the operator opened it and is settling it in the
# same breath — an agent-opened market could not do this, and that restriction is the point.
mid, detail = market("Did the reference index finish above its open? (demo, settles now)",
                     2, observe_after_ms=1_000, dispute_window_ms=0)
for (outcome, amount), (seed, aid) in zip([(0, 300), (1, 200), (0, 80), (1, 120)], agents):
    call("POST", f"/v1/markets/{mid}/stakes", seed=seed, agent_id=aid,
         body={"outcome": outcome, "amount": amount})
wait = detail["observed_at_ms"] / 1000 - time.time()
print(f"  waiting {max(0, wait):.0f}s for one market to reach its observation time...")
time.sleep(max(0, wait) + 0.5)
call("POST", f"/v1/markets/{mid}/propose", owner=True,
     body={"outcome": 0, "evidence": "observed at the named source"})
st, out = call("POST", f"/v1/markets/{mid}/finalize", body={}, owner=True)
if st == 200:
    print(f"  settled one market — the house earned {out.get('rake', 0):.2f} PTS "
          "from the losing pool")
else:
    print(f"  note: the demo market did not settle ({st} {out})")

# And a subscriber key, so the feed section isn't empty.
call("POST", "/v1/feed/subscribers", owner=True, body={"label": "demo-subscriber"})
print("  issued one demo feed subscriber key")
