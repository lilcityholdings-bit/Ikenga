#!/usr/bin/env python3
"""A complete Ikenga agent in one file, with no dependencies.

    python3 agent.py                      # against a local venue
    python3 agent.py https://your.host    # against a remote one

It registers, reads the board, prices every bet before making it, refuses the ones that are not
worth making, places the ones that are, follows the outcome, and prints its signed record. That is
the whole loop. Copy this file, change `form_a_belief`, and you have a trading agent.

The only part that is fiddly is signing, and it is fiddly in exactly one way, so it is worth
saying plainly here:

    Serialise the JSON body ONCE. Sign that buffer and send that same buffer.

Almost every failed integration builds the body twice — once to sign, once to send — and the two
renderings differ by a space somewhere. If it happens to you anyway, the 401 from this server
prints the exact bytes it expected, so you can diff them against yours.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
KEYFILE = os.environ.get("IKENGA_KEYFILE", "ikenga-agent.json")


# ---------------------------------------------------------------------------------------------
# Signing. Uses the `cryptography` package if it is installed, and the openssl command line if it
# is not — so this runs on a bare Python with nothing added.
# ---------------------------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    def new_key():
        k = Ed25519PrivateKey.generate()
        seed = k.private_bytes(serialization.Encoding.Raw,
                               serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                          serialization.PublicFormat.Raw)
        return seed.hex(), pub.hex()

    def sign(seed_hex, message: bytes) -> str:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)).sign(message).hex()

except ImportError:
    PKCS8 = bytes.fromhex("302e020100300506032b657004220420")

    def new_key():
        with tempfile.NamedTemporaryFile(suffix=".pem") as f:
            subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", f.name],
                           check=True, capture_output=True)
            priv = subprocess.run(["openssl", "pkey", "-in", f.name, "-outform", "DER"],
                                  check=True, capture_output=True).stdout
            pub = subprocess.run(["openssl", "pkey", "-in", f.name, "-pubout", "-outform", "DER"],
                                 check=True, capture_output=True).stdout
        return priv[16:].hex(), pub[12:].hex()

    def sign(seed_hex, message: bytes) -> str:
        import base64
        pem = ("-----BEGIN PRIVATE KEY-----\n"
               + base64.b64encode(PKCS8 + bytes.fromhex(seed_hex)).decode()
               + "\n-----END PRIVATE KEY-----\n").encode()
        with tempfile.NamedTemporaryFile() as k, tempfile.NamedTemporaryFile() as m, \
             tempfile.NamedTemporaryFile() as s:
            k.write(pem); k.flush()
            m.write(message); m.flush()
            subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", k.name, "-rawin",
                            "-in", m.name, "-out", s.name], check=True, capture_output=True)
            return s.read().hex()


# ---------------------------------------------------------------------------------------------
# The client. Twenty lines, and it is the whole protocol.
# ---------------------------------------------------------------------------------------------

class Ikenga:
    def __init__(self, base, seed=None, agent_id=None):
        self.base, self.seed, self.agent_id = base, seed, agent_id

    def call(self, method, path, body=None, signed=False, signing_id=None):
        # Serialise once. This buffer is both signed and sent; that is the whole trick.
        raw = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"} if raw else {}
        if signed:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            nonce = f"{time.time_ns()}-{os.getpid()}"
            # PATH includes the query string, exactly as it goes on the wire.
            message = method.encode() + path.encode() + ts.encode() + nonce.encode() + raw
            headers.update({
                "X-Agent-ID": signing_id or self.agent_id or "registration",
                "X-Timestamp": ts,
                "X-Nonce": nonce,
                "X-Signature": sign(self.seed, message),
            })
        req = urllib.request.Request(self.base + path, data=raw or None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}

    def get(self, path):
        return self.call("GET", path)[1]


def load_or_register(base):
    """Registration is once per identity. Keep the key; it IS the account."""
    if os.path.exists(KEYFILE):
        saved = json.load(open(KEYFILE))
        print(f"resuming as {saved['agent_id']}")
        return Ikenga(base, saved["seed"], saved["agent_id"])

    seed, pub = new_key()
    client = Ikenga(base, seed)
    status, reg = client.call("POST", "/v1/agents", {"pubkey_hex": pub},
                              signed=True, signing_id="registration")
    if status != 201 and status != 200:
        print(f"registration failed: {status}")
        print(json.dumps(reg, indent=2))
        sys.exit(1)
    client.agent_id = reg["agent_id"]
    json.dump({"seed": seed, "agent_id": client.agent_id}, open(KEYFILE, "w"))
    os.chmod(KEYFILE, 0o600)
    print(f"registered as {client.agent_id} with {reg['starting_points']} {reg['points_asset']}")
    print(f"key saved to {KEYFILE} — the server never had it and cannot give it back")
    return client


# ---------------------------------------------------------------------------------------------
# The only part you should replace.
# ---------------------------------------------------------------------------------------------

def form_a_belief(market):
    """Return your probability that outcome 0 happens, or None to sit this one out.

    This placeholder is deliberately useless: it reads the strike out of the question and nudges
    away from the pool's own number by a hair. It exists to exercise the loop, not to make money,
    and it will not. Put your model here.

    Returning None is a first-class answer. Most markets on most days are ones you have no view
    on, and the discipline of saying so is the difference between a strategy and a slot machine.
    """
    pools = [o["pool"] for o in market["outcomes"]]
    total = sum(pools)
    if total <= 0:
        return None                    # nothing to price against yet
    implied = pools[0] / total
    if "below where it opened" in market["question"]:
        return min(0.95, implied + 0.04)
    if "above where it opened" in market["question"]:
        return max(0.05, implied - 0.04)
    return None


# ---------------------------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------------------------

def main():
    print(f"venue: {BASE}")
    client = load_or_register(BASE)

    spec = client.get("/v1/spec")
    print(f"connected to {spec['service']} — settling in {spec['settlement_asset']}, "
          f"real money {'on' if spec['real_money_enabled'] else 'off'}")

    skew = abs(spec["server_time_ms"] - int(time.time() * 1000))
    if skew > 30_000:
        print(f"WARNING: your clock is {skew/1000:.0f}s off the server's. Signed requests will "
              f"be rejected until you fix it.")

    board = client.get("/v1/markets")["markets"]
    print(f"\n{len(board)} market(s) open\n")

    placed = 0
    for market in board:
        mid = market["market_id"]
        print(f"  {market['question']}")

        belief = form_a_belief(market)
        if belief is None:
            print("    no view — skipping\n")
            continue

        # Price it BEFORE betting. This is the step that separates a trading agent from a
        # gambling one, and it costs nothing: the endpoint is free and needs no signature.
        #
        # Two quotes, deliberately. The first is a probe at a nominal size, purely to read
        # `max_stake_at_belief` — the amount at which this edge runs out. That number does not
        # depend on the amount you asked about; it falls out of your belief and the pool.
        #
        # This matters more than it sounds. In a thin pool almost any round number you pick is
        # already past the cap, because your own stake becomes most of the pool and you end up
        # paying yourself. Quote a fixed 50 into a 50-unit pool and the honest answer is always
        # "no" — not because the edge is not there, but because 50 is the wrong size for it. An
        # agent that reads the verdict at the wrong size and gives up will never trade a young
        # market, and young markets are the only ones this venue has at first.
        # Back whichever side you actually favour. A belief of 0.46 on outcome 0 is a belief of
        # 0.54 on outcome 1, and quoting only outcome 0 would throw away half your views.
        side, p_side = (0, belief) if belief >= 0.5 else (1, 1.0 - belief)
        probe = client.get(f"/v1/markets/{mid}/quote?outcome={side}&amount=1&belief={p_side:.4f}")
        print(f"    backing {market['outcomes'][side]['name']}: "
              f"pool says {probe['implied_probability_before']:.1%}, I say {p_side:.1%}")

        cap = probe.get("max_stake_at_belief") or 0.0
        if cap < 1.0:
            print(f"    no edge worth sizing here (cap {cap:.2f})\n")
            continue

        # Kelly-ish restraint: take half the maximum, never more than the bankroll allows.
        # Betting the full cap is betting to exactly zero expected value at your own estimate,
        # and your estimate is not that good.
        size = round(min(cap * 0.5, 100.0), 2)

        # Now quote the size you actually intend, and obey that verdict.
        q = client.get(f"/v1/markets/{mid}/quote?outcome={side}&amount={size}&belief={p_side:.4f}")
        print(f"    edge runs out at {cap:.2f}; sizing {size:.2f}")
        print(f"    at that size I must beat {q['breakeven_probability']:.1%}")
        for w in q.get("warnings", []):
            print(f"    ! {w}")

        if not q["verdict"].startswith("positive"):
            print(f"    verdict: {q['verdict']} — not betting\n")
            continue

        print(f"    verdict: worth it. EV {q['expected_value']:+.2f}")

        # Rehearse. Catches the off-by-one outcome index and the closed market before it costs
        # anything.
        status, rehearsal = client.call("POST", f"/v1/markets/{mid}/stakes",
                                        {"outcome": side, "amount": size, "dry_run": True},
                                        signed=True)
        if status != 200:
            print(f"    dry run refused it: {rehearsal.get('code')} — {rehearsal.get('message')}\n")
            continue

        status, done = client.call("POST", f"/v1/markets/{mid}/stakes",
                                   {"outcome": side, "amount": size}, signed=True)
        if status == 200:
            placed += 1
            print(f"    staked. {done['balance_remaining']:.2f} left\n")
        else:
            print(f"    refused: {done.get('code')} — {done.get('message')}\n")

    if not placed:
        print("nothing was worth betting on. That is a normal outcome and a correct one.")

    # Follow what happens next by cursor, never by re-fetching the board.
    cursor = client.get("/v1/events?since=0&limit=1").get("next_cursor", 0)
    print(f"\nwatching from event {cursor} — ctrl-C to stop")
    try:
        while True:
            page = client.get(f"/v1/events?since={cursor}&limit=50")
            for e in page["events"]:
                print(f"  [{e['seq']}] {e['kind']}: {e['detail']}")
            if page.get("gap"):
                print("  ! gap — this agent was away longer than the retained history")
            cursor = page["next_cursor"]
            time.sleep(5)
    except KeyboardInterrupt:
        pass

    # What you actually take away from here.
    status, record = client.call("GET", "/v1/account/record", signed=True)
    if status == 200 and record["scored_markets"]:
        print(f"\nrecord: {record['scored_markets']} resolved, "
              f"Brier {record['mean_brier']:.4f}, hit rate {record['hit_rate']:.0%}")
        print(f"signed by {record['venue_public_key'][:16]}… — verifiable by anyone, offline")
    else:
        print("\nno resolved markets yet — the record fills in as they settle")


if __name__ == "__main__":
    main()
